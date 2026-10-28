"""Porteiro: a placa ESPHome do portão (campainha, fechadura e garagens).

O Zóio fica conectado na placa o tempo todo, pela API nativa do ESPHome,
cifrada com a chave dela. Assim a campainha e os sensores das garagens chegam
na hora, e os comandos vão pela mesma conexão.

Nomes que a placa precisa ter (deploy/esphome/porteiro.yaml):
- botão `abrir_portao`: fechadura do portão de pedestre;
- botões `garagem_1`, `garagem_2`...: botoeira da central de cada motor, que
  abre, para ou fecha conforme o momento (igual ao controle remoto);
- sensores `garagem_1_aberta`...: sensor magnético de cada garagem (opcional,
  mas sem ele a garagem só tem o comando genérico "acionar");
- sensor `campainha`: botão de campainha.

As garagens podem ser acionadas por relé ou por rádio 433 MHz (código fixo):
para o Zóio é igual, um botão por garagem. `radio()` escuta o receptor da
placa para aprender os códigos do controle e dos sensores sem fio.

Cada comando e cada toque da campainha vira um evento com foto da câmera
daquela porta, e os outros aparelhos são avisados.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .alarms import EventBus
from .config import Config
from .esphome import EspHome, EspHomeError
from .store import BELL_KIND, EVENT_RETENTION_DAYS, GARAGE_KIND, GATE_KIND, Store

log = logging.getLogger(__name__)

RC_SWITCH = re.compile(r"Received RCSwitch Raw: protocol=(\d+) data='([01]+)'")

GATE_ID = "portao"
GATE_BUTTON = "abrir_portao"
DOORBELL = "campainha"
WIFI_SIGNAL = "sinal_wifi"
GARAGE_BUTTON = re.compile(r"^garagem_(\d{1,2})$")

MIN_INTERVAL = 5  # segundos entre dois comandos na mesma porta (Parar não espera)
RING_INTERVAL = 15  # toques seguidos da campainha viram um só aviso
PHOTO_TIMEOUT = 8
RETRY_MIN, RETRY_MAX = 2, 60  # espera entre tentativas de reconectar
FAILED = "Falhou"
# Estado registrado para cada comando que deu certo
DONE = {("portao", "abrir"): "Aberto", ("garagem", "abrir"): "Abrindo", ("garagem", "fechar"): "Fechando",
        ("garagem", "parar"): "Parado", ("garagem", "acionar"): "Acionado"}
CACHE_KEY = "porteiro_portas"


class GateError(Exception):
    pass


class GateBusy(GateError):
    pass


@dataclass
class Door:
    id: str  # "portao", "garagem_1"...
    kind: str  # "portao" ou "garagem"
    name: str
    camera: int | None
    button: str  # object_id do botão na placa
    sensor: str | None = None  # object_id do sensor magnético
    open: bool | None = None  # leitura do sensor (None: sem sensor ou ainda sem leitura)
    moving_until: float = 0.0  # até quando o motor deve estar andando (time.time())
    unsure: bool = False  # depois de um Parar, o próximo toque pode abrir ou fechar
    expect_until: float = 0.0  # mudança do sensor esperada (comando do próprio Zóio)

    def actions(self, now: float | None = None) -> list[str]:
        """O que dá para fazer agora. Fechar só com o sensor dizendo "aberta"."""
        if self.kind == "portao":
            return ["abrir"]
        now = now or time.time()
        if now < self.moving_until:
            return ["parar"]
        if self.sensor is None or self.unsure or self.open is None:
            return ["acionar"]
        return ["fechar"] if self.open else ["abrir"]

    def public(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "camera": self.camera,
            "sensor": self.sensor is not None, "open": self.open, "moving_until": self.moving_until,
            "unsure": self.unsure, "actions": self.actions(),
        }


class Porteiro:
    def __init__(self, cfg: Config, store: Store, bus: EventBus, notifier=None):
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.notifier = notifier
        self.dir = cfg.data_dir / "portao"
        self.http: aiohttp.ClientSession | None = None
        self.board: EspHome | None = None
        self.online = False
        self.last_error: str | None = cfg.gate_error
        self.info: dict = {}
        self.doors: dict[str, Door] = {}
        self._keys: dict[str, int] = {}  # object_id -> chave da entidade na placa
        self._by_key: dict[int, str] = {}  # chave -> object_id dos sensores
        self._wifi_key: int | None = None
        self.wifi_dbm: float | None = None
        self._synced: set[str] = set()  # sensores que já mandaram a leitura inicial
        self._bell = False
        self._last_ring = -RING_INTERVAL
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._load_cache()

    @property
    def enabled(self) -> bool:
        return self.cfg.gate_enabled

    def _board(self) -> EspHome:
        return EspHome(self.cfg.gate_host, self.cfg.gate_port, self.cfg.gate_key, expected_name=self.cfg.gate_name or None)

    def photo_path(self, event_id: int) -> Path:
        return self.dir / f"{int(event_id)}.jpg"

    def public(self, allowed: bool = False) -> dict:
        return {
            "enabled": self.enabled,
            "online": self.online,
            "allowed": allowed,
            "camera": self.cfg.gate_camera,
            "doorbell": bool(self.info.get("doorbell")),
            "doors": [d.public() for d in self.doors.values()],
        }

    # ---------------------------------------------------------------- conexão

    async def run(self, http: aiohttp.ClientSession) -> None:
        """Fica conectado na placa enquanto o servidor estiver de pé."""
        self.http = http
        delay = RETRY_MIN
        while True:
            try:
                async with self._board() as board:
                    await self._setup(board)
                    delay = RETRY_MIN
                    await board.run()
            except EspHomeError as exc:
                if self.online or exc.args[0] != self.last_error:
                    log.warning("porteiro: %s", exc)
                self.last_error = str(exc)
            except OSError as exc:
                self.last_error = f"conexão com a placa caiu: {exc}"
            finally:
                self.board = None
                if self.online:
                    self.online = False
                    self._publish_state()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    async def _setup(self, board: EspHome) -> None:
        entities = await board.entities()
        self._keys = {obj: key for obj, (key, _) in {**entities.buttons, **entities.binary_sensors}.items()}
        self._by_key = {key: obj for obj, (key, _) in entities.binary_sensors.items()}
        self._wifi_key = entities.sensors.get(WIFI_SIGNAL, (None,))[0]
        self._synced = set()
        self.doors = self._build_doors(set(entities.buttons), set(entities.binary_sensors))
        self.info = {
            "name": board.name, "mac": board.mac, "version": board.server_info,
            "doorbell": DOORBELL in entities.binary_sensors,
            "buttons": sorted(entities.buttons), "sensors": sorted(entities.binary_sensors),
        }
        board.on_state = self._on_state
        board.on_value = self._on_value
        await board.subscribe_states()
        self.board, self.online, self.last_error = board, True, None
        self._save_cache()
        log.info("porteiro: conectado em %s (%s)", board.name, ", ".join(self.doors) or "nenhuma porta")
        self._publish_state()

    def _build_doors(self, buttons: set[str], sensors: set[str]) -> dict[str, Door]:
        old = self.doors
        doors: dict[str, Door] = {}
        if GATE_BUTTON in buttons:
            doors[GATE_ID] = Door(GATE_ID, "portao", "Portão", self.cfg.gate_camera, GATE_BUTTON)
        garages = sorted((int(m.group(1)), b) for b in buttons if (m := GARAGE_BUTTON.match(b)))
        for n, button in garages:
            sensor = f"{button}_aberta"
            door = Door(
                button, "garagem", self.cfg.garage_name(n), self.cfg.garage_camera(n), button,
                sensor if sensor in sensors else None,
            )
            if button in old:  # mantém o que se sabia antes da reconexão
                door.moving_until, door.unsure = old[button].moving_until, old[button].unsure
            doors[button] = door
        return doors

    def _load_cache(self) -> None:
        if not self.enabled:
            return
        try:
            saved = json.loads(self.store.get(CACHE_KEY) or "{}")
        except ValueError:
            return
        self.info = saved.get("info", {})
        self.doors = self._build_doors(set(saved.get("buttons", [])), set(saved.get("sensors", [])))

    def _save_cache(self) -> None:
        self.store.set(CACHE_KEY, json.dumps({"info": self.info, "buttons": self.info["buttons"], "sensors": self.info["sensors"]}))

    def _publish_state(self) -> None:
        self.bus.publish({"type": "portaria", "online": self.online, "doors": [d.public() for d in self.doors.values()]})

    # ---------------------------------------------------------------- sensores

    def _on_value(self, key: int, value: float) -> None:
        if key == self._wifi_key:
            self.wifi_dbm = round(value)

    def _on_state(self, key: int, state: bool) -> None:
        obj = self._by_key.get(key)
        if obj is None:
            return
        first = obj not in self._synced
        self._synced.add(obj)
        if obj == DOORBELL:
            if state and not self._bell and not first:
                self._spawn(self.ring())
            self._bell = state
            return
        door = next((d for d in self.doors.values() if d.sensor == obj), None)
        if door is None or door.open == state:
            return
        door.open = state
        now = time.time()
        if not first:
            door.unsure = False
            if state:  # abriu: o motor está andando
                door.moving_until = max(door.moving_until, now + self.cfg.garage_travel)
            else:  # fechou de vez
                door.moving_until = 0.0
            if now >= door.expect_until:  # mexeram pelo controle ou na mão: registra
                self._spawn(self._log_change(door, state))
        self._publish_state()

    async def ring(self) -> dict | None:
        """Alguém tocou a campainha: avisa na hora e guarda a foto de quem tocou."""
        now = time.monotonic()
        if now - self._last_ring < RING_INTERVAL:
            return None
        self._last_ring = now
        log.info("porteiro: campainha")
        if self.notifier:
            self._spawn(self.notifier.doorbell())
        image = await self._photo(self.cfg.gate_camera)
        event = self._add_event(self.cfg.gate_camera, BELL_KIND, "Tocou", {"door": GATE_ID, "photo": bool(image)}, image)
        self.bus.publish({"type": "campainha", **event})
        return event

    async def _log_change(self, door: Door, opened: bool) -> None:
        image = await self._photo(door.camera)
        event = self._add_event(door.camera, GARAGE_KIND, "Abriu" if opened else "Fechou", {"door": door.id, "name": door.name, "photo": bool(image)}, image)
        self.bus.publish({"type": "porta", **event})

    # ---------------------------------------------------------------- comandos

    async def act(self, door_id: str, action: str, by: str, device_id: str | None) -> dict:
        """Manda o comando e devolve o evento registrado (estado `Falhou` se não chegou na placa)."""
        if not self.enabled:
            raise GateError(self.cfg.gate_error or "portaria não configurada")
        door = self.doors.get(door_id)
        if door is None:
            raise GateError(f"porta desconhecida: {door_id}")
        allowed = door.actions()
        if action not in allowed:
            raise GateBusy(_refusal(door, action, allowed))
        now = time.monotonic()
        if action != "parar" and (self._lock.locked() or now - self._last.get(door.id, -MIN_INTERVAL) < MIN_INTERVAL):
            raise GateBusy(f"{door.name}: o comando acabou de ser enviado; aguarde alguns segundos")
        self._last[door.id] = now

        # A foto sai junto com o comando: mostra quem estava ali naquela hora.
        photo = asyncio.create_task(self._photo(door.camera))
        error = None
        try:
            async with self._lock:
                board = self.board
                if board is None:
                    raise EspHomeError(f"placa do porteiro desconectada ({self.last_error or 'sem resposta'})")
                await board.press(self._keys[door.button])
        except (EspHomeError, KeyError) as exc:
            error = str(exc) if isinstance(exc, EspHomeError) else f"botão {door.button} sumiu da placa"
        if error is None:
            self._after_press(door, action)
        image = await photo
        status = FAILED if error else DONE[(door.kind, action)]
        detail = {"door": door.id, "name": door.name, "action": action, "by": by, "device_id": device_id, "error": error, "photo": bool(image)}
        event = self._add_event(door.camera, GATE_KIND if door.kind == "portao" else GARAGE_KIND, status, detail, image)
        if error:
            log.warning("porteiro: %s (%s) falhou: %s", door.name, action, error)
        else:
            log.info("porteiro: %s: %s por %s", door.name, action, by)
        self.bus.publish({"type": "porta", **event})
        self._publish_state()
        if error is None and self.notifier:
            self._spawn(self.notifier.door_action(door.name, status, by, device_id))
        return event

    def _after_press(self, door: Door, action: str) -> None:
        if door.kind != "garagem":
            return
        now = time.time()
        if action == "parar":
            door.moving_until, door.unsure = 0.0, True
        else:
            door.moving_until = now + self.cfg.garage_travel
            door.expect_until = now + self.cfg.garage_travel + 5
            door.unsure = False

    @contextlib.asynccontextmanager
    async def session(self):
        """Conexão curta, para a linha de comando: lista as portas e deixa mandar comandos."""
        async with self._board() as board:
            entities = await board.entities()
            self._keys = {obj: key for obj, (key, _) in {**entities.buttons, **entities.binary_sensors}.items()}
            self.doors = self._build_doors(set(entities.buttons), set(entities.binary_sensors))
            self.info = {
                "name": board.name, "mac": board.mac, "version": board.server_info,
                "doorbell": DOORBELL in entities.binary_sensors,
                "buttons": sorted(entities.buttons), "sensors": sorted(entities.binary_sensors),
            }
            self.board, self.online = board, True
            try:
                yield self
            finally:
                self.board, self.online = None, False

    async def radio(self, seconds: float, on_code) -> None:
        """Escuta o rádio da placa por `seconds` segundos e chama
        `on_code(protocolo, código)` uma vez para cada código novo."""
        seen: set[tuple[int, str]] = set()

        def on_log(line: str) -> None:
            m = RC_SWITCH.search(line)
            if m and (code := (int(m[1]), m[2])) not in seen:
                seen.add(code)
                on_code(*code)

        async with self._board() as board:
            board.on_log = on_log
            await board.subscribe_logs()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(board.run(), seconds)

    async def check(self) -> dict:
        """Estado da placa para o botão "Testar" (conecta de novo se estiver fora)."""
        if not self.enabled:
            raise GateError(self.cfg.gate_error or "portaria não configurada")
        if not self.online:
            async with self._board() as board:
                await board.entities()
        return {**self.info, "online": self.online, "wifi_dbm": self.wifi_dbm, "doors": [d.public() for d in self.doors.values()]}

    # ---------------------------------------------------------------- apoio

    def _add_event(self, camera: int | None, kind: str, status: str, detail: dict, image: bytes | None) -> dict:
        event = self.store.add_event(camera or 0, kind, status, None, detail)
        if image:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.photo_path(event["id"]).write_bytes(image)
        return event

    async def _photo(self, camera: int | None) -> bytes | None:
        cam = self.cfg.camera(camera) if camera else None
        if cam is None:
            return None
        url = f"{self.cfg.go2rtc_url}/api/frame.jpeg"
        try:
            http = self.http or aiohttp.ClientSession()
            try:
                async with http.get(url, params={"src": cam.main}, timeout=aiohttp.ClientTimeout(total=PHOTO_TIMEOUT)) as resp:
                    data = await resp.read()
                    if resp.status == 200 and data[:2] == b"\xff\xd8":
                        return data
                    log.debug("porteiro: go2rtc não devolveu foto (%s)", resp.status)
            finally:
                if http is not self.http:
                    await http.close()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug("porteiro: foto falhou: %s", exc)
        return None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def prune(self) -> None:
        if not self.dir.is_dir():
            return
        limit = time.time() - EVENT_RETENTION_DAYS * 86400
        for p in self.dir.glob("*.jpg"):
            if p.stat().st_mtime < limit:
                p.unlink(missing_ok=True)


def _refusal(door: Door, action: str, allowed: list[str]) -> str:
    if allowed == ["parar"]:
        return f"{door.name} está em movimento; use Parar ou espere"
    if action == "fechar" and door.open is False:
        return f"{door.name} já está fechada"
    if action == "abrir" and door.open:
        return f"{door.name} já está aberta"
    if action in ("abrir", "fechar") and allowed == ["acionar"]:
        return f"{door.name}: sem leitura do sensor agora; só dá para acionar"
    return f"{door.name}: comando {action!r} não vale agora (dá para: {', '.join(allowed)})"
