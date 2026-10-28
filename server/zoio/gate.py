"""Portão: aciona a placa ESPHome, registra quem abriu e guarda uma foto.

A placa (ESP32 + relé) fica perto do porteiro da rua e fecha o contato da
botoeira por um tempo fixo, gravado nela. O Zóio só manda "apertar o botão"
pela API nativa do ESPHome, cifrada com a chave da placa.

Cada abertura vira um evento (com quem abriu, o resultado e uma foto da
câmera do portão tirada no mesmo instante) e avisa os outros administradores.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp

from .alarms import EventBus
from .config import Config
from .esphome import EspHome, EspHomeError
from .store import EVENT_RETENTION_DAYS, GATE_KIND, Store

log = logging.getLogger(__name__)

MIN_INTERVAL = 5  # segundos entre dois acionamentos
PHOTO_TIMEOUT = 8
OPENED, FAILED = "Aberto", "Falhou"


class GateError(Exception):
    pass


class GateBusy(GateError):
    pass


class Gate:
    def __init__(self, cfg: Config, store: Store, bus: EventBus, notifier=None):
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.notifier = notifier
        self.dir = cfg.data_dir / "portao"
        self._lock = asyncio.Lock()
        self._last = -MIN_INTERVAL
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self.cfg.gate_enabled

    def _board(self) -> EspHome:
        return EspHome(self.cfg.gate_host, self.cfg.gate_port, self.cfg.gate_key, expected_name=self.cfg.gate_name or None)

    def photo_path(self, event_id: int) -> Path:
        return self.dir / f"{int(event_id)}.jpg"

    async def check(self) -> dict:
        """Conecta na placa e lista os botões, sem acionar nada."""
        if not self.enabled:
            raise GateError(self.cfg.gate_error or "portão não configurado")
        async with self._board() as board:
            buttons = await board.buttons()
        return {
            "name": board.name,
            "mac": board.mac,
            "version": board.server_info,
            "buttons": sorted(buttons),
            "button_ok": self.cfg.gate_button in buttons or len(buttons) == 1,
        }

    async def open(self, by: str, device_id: str | None, http: aiohttp.ClientSession) -> dict:
        """Abre o portão. Devolve o evento registrado (com `status` Aberto ou Falhou)."""
        if not self.enabled:
            raise GateError(self.cfg.gate_error or "portão não configurado")
        if self._lock.locked() or time.monotonic() - self._last < MIN_INTERVAL:
            raise GateBusy("o portão acabou de ser acionado; aguarde alguns segundos")
        async with self._lock:
            self._last = time.monotonic()
            # A foto sai junto com o comando: mostra quem estava no portão naquela hora.
            photo = asyncio.create_task(self._photo(http)) if self.cfg.gate_camera else None
            error = None
            try:
                async with self._board() as board:
                    await board.press(self.cfg.gate_button)
            except EspHomeError as exc:
                error = str(exc)
            image = await photo if photo else None
            event = self.store.add_event(
                self.cfg.gate_camera or 0,
                GATE_KIND,
                OPENED if error is None else FAILED,
                None,
                {"by": by, "device_id": device_id, "error": error, "photo": bool(image)},
            )
            if image:
                self.dir.mkdir(parents=True, exist_ok=True)
                self.photo_path(event["id"]).write_bytes(image)
            self._last = time.monotonic()
        if error:
            log.warning("portão: falha ao abrir (%s): %s", by, error)
        else:
            log.info("portão aberto por %s", by)
        self.bus.publish({"type": "gate", **event})
        if error is None and self.notifier:
            task = asyncio.create_task(self.notifier.gate_opened(by, device_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return event

    async def _photo(self, http: aiohttp.ClientSession) -> bytes | None:
        cam = self.cfg.camera(self.cfg.gate_camera or 0)
        if cam is None:
            return None
        url = f"{self.cfg.go2rtc_url}/api/frame.jpeg"
        try:
            async with http.get(url, params={"src": cam.main}, timeout=aiohttp.ClientTimeout(total=PHOTO_TIMEOUT)) as resp:
                data = await resp.read()
                if resp.status == 200 and data[:2] == b"\xff\xd8":
                    return data
                log.debug("portão: go2rtc não devolveu foto (%s)", resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug("portão: foto falhou: %s", exc)
        return None

    def prune(self) -> None:
        if not self.dir.is_dir():
            return
        limit = time.time() - EVENT_RETENTION_DAYS * 86400
        for p in self.dir.glob("*.jpg"):
            if p.stat().st_mtime < limit:
                p.unlink(missing_ok=True)
