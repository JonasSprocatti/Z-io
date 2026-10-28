"""Placa ESPHome simulada.

A criptografia do lado da placa usa a biblioteca `noise` (a mesma do
aioesphomeapi), e não o código do Zóio. Assim os testes conferem a
implementação do Zóio contra uma implementação independente do Noise.
"""

from __future__ import annotations

import asyncio
import struct

from noise.connection import NoiseConnection

from zoio import esphome as eh

PORTEIRO_BUTTONS = {"abrir_portao": (0x1234ABCD, "Abrir portao"), "garagem_1": (0x2001, "Garagem 1"), "garagem_2": (0x2002, "Garagem 2")}
PORTEIRO_SENSORS = {"campainha": (0x3000, "Campainha"), "garagem_1_aberta": (0x3001, "Garagem 1 aberta")}
# Ações (`api: actions:`): nome -> (chave, argumentos de texto)
PORTEIRO_SERVICES = {"conversa_iniciar": (0x4001, ["chave"]), "conversa_parar": (0x4002, [])}
STRING_ARG = 3  # ServiceArgType.STRING


class FakeEspHome:
    def __init__(self, key: str, name: str = "zoio-porteiro", buttons: dict | None = None, sensors: dict | None = None, services: dict | None = None, encrypted: bool = True):
        self.psk = eh.decode_key(key)
        self.name = name
        self.buttons = dict(PORTEIRO_BUTTONS if buttons is None else buttons)
        self.sensors = dict(PORTEIRO_SENSORS if sensors is None else sensors)
        self.services = dict(PORTEIRO_SERVICES if services is None else services)
        self.executed: list[tuple[str, list[str]]] = []  # ações rodadas: (nome, argumentos)
        self.on_execute = None  # chamado com (nome, argumentos), como o código da ação na placa
        self.states = {obj: False for obj in self.sensors}
        self.encrypted = encrypted
        self.pressed: list[int] = []
        self.hello: dict | None = None
        self.pongs = 0
        self.disconnects = 0
        self.connections = 0
        self.silent = False  # para de responder (simula a placa travada)
        self.wifi_dbm = -61.0
        self.logs: list[str] = []  # linhas mandadas a quem assinar o log (ex.: códigos de rádio)
        self.log_level: int | None = None
        self._subscribers: list = []
        self._writers: set[asyncio.StreamWriter] = set()
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self, port: int = 0) -> "FakeEspHome":
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port or self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.server.close()
        for w in list(self._writers):  # derruba quem estiver conectado
            w.close()
        await self.server.wait_closed()

    def press_key(self, key: int) -> str | None:
        return next((obj for obj, (k, _) in self.buttons.items() if k == key), None)

    async def set_state(self, object_id: str, state: bool) -> None:
        """Muda um sensor (campainha apertada, garagem abriu) e avisa quem assina."""
        self.states[object_id] = state
        for send in list(self._subscribers):
            send(object_id, state)
        await asyncio.sleep(0.05)

    async def _client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self._writers.add(w)
        self.connections += 1

        async def frame() -> bytes:
            head = await r.readexactly(3)
            assert head[0] == 0x01, head
            return await r.readexactly(struct.unpack(">H", head[1:])[0])

        def send(payload: bytes) -> None:
            w.write(b"\x01" + struct.pack(">H", len(payload)) + payload)

        subscriber = None
        try:
            if not self.encrypted:
                await r.read(3)
                w.write(b"\x00\x00\x00")  # placa sem chave responde no protocolo aberto
                await w.drain()
                return
            assert await frame() == b""  # client hello
            handshake = await frame()
            assert handshake[0] == 0x00
            noise = NoiseConnection.from_name(eh.PROTOCOL_NAME)
            noise.set_as_responder()
            noise.set_psks(self.psk)
            noise.set_prologue(eh.PROLOGUE)
            noise.start_handshake()
            send(b"\x01" + self.name.encode() + b"\x00" + b"AA:BB:CC:DD:EE:FF\x00")
            try:
                noise.read_message(handshake[1:])
            except Exception:
                send(b"\x01Handshake MAC failure")  # o que o ESPHome responde com a chave errada
                await w.drain()
                return
            send(b"\x00" + noise.write_message())
            await w.drain()

            def send_msg(msg_type: int, data: bytes = b"") -> None:
                send(noise.encrypt(struct.pack(">HH", msg_type, len(data)) + data))

            def send_state(object_id: str, state: bool) -> None:
                key = self.sensors[object_id][0]
                send_msg(eh.BINARY_SENSOR_STATE, eh.pb_fixed32(1, key) + eh.pb_uint(2, int(state)))

            while True:
                plain = noise.decrypt(await frame())
                if self.silent:
                    continue
                msg_type, data = struct.unpack(">H", plain[:2])[0], plain[4:]
                if msg_type == eh.HELLO_REQUEST:
                    self.hello = eh.pb_parse(data)
                    send_msg(eh.HELLO_RESPONSE, eh.pb_uint(1, 1) + eh.pb_uint(2, 10) + eh.pb_string(3, "fake 2026.10") + eh.pb_string(4, self.name))
                    send_msg(eh.PING_REQUEST)  # a placa real manda ping de tempos em tempos
                elif msg_type == eh.LIST_ENTITIES_REQUEST:
                    send_msg(eh.LIST_ENTITIES_SENSOR, eh.pb_string(1, "sinal_wifi") + eh.pb_fixed32(2, 7) + eh.pb_string(3, "Sinal Wi-Fi"))
                    for object_id, (key, name) in self.sensors.items():
                        send_msg(eh.LIST_ENTITIES_BINARY_SENSOR, eh.pb_string(1, object_id) + eh.pb_fixed32(2, key) + eh.pb_string(3, name))
                    for object_id, (key, name) in self.buttons.items():
                        send_msg(eh.LIST_ENTITIES_BUTTON, eh.pb_string(1, object_id) + eh.pb_fixed32(2, key) + eh.pb_string(3, name))
                    for name, (key, args) in self.services.items():
                        arg_fields = b"".join(eh.pb_bytes(3, eh.pb_string(1, arg) + eh.pb_uint(2, STRING_ARG)) for arg in args)
                        send_msg(eh.LIST_ENTITIES_SERVICES, eh.pb_string(1, name) + eh.pb_fixed32(2, key) + arg_fields)
                    send_msg(eh.LIST_ENTITIES_DONE)
                elif msg_type == eh.SUBSCRIBE_STATES:
                    for object_id, state in self.states.items():
                        send_state(object_id, state)
                    send_msg(eh.BINARY_SENSOR_STATE, eh.pb_fixed32(1, 0x9999) + eh.pb_uint(3, 1))  # sensor sem leitura
                    send_msg(eh.SENSOR_STATE, eh.pb_fixed32(1, 7) + eh.pb_float(2, self.wifi_dbm))
                    subscriber = send_state
                    self._subscribers.append(subscriber)
                elif msg_type == eh.SUBSCRIBE_LOGS:
                    self.log_level = eh.pb_parse(data).get(1)
                    for line in self.logs:
                        send_msg(eh.SUBSCRIBE_LOGS_RESPONSE, eh.pb_uint(1, 3) + eh.pb_string(3, line))
                elif msg_type == eh.BUTTON_COMMAND:
                    self.pressed.append(eh.pb_parse(data)[1])
                elif msg_type == eh.EXECUTE_SERVICE:
                    fields = eh.pb_parse(data)
                    name = next(n for n, (k, _) in self.services.items() if k == fields[1])
                    # um argumento só (o pb_parse guarda o último de cada campo)
                    args = [eh.pb_parse(fields[2])[4].decode()] if 2 in fields else []
                    self.executed.append((name, args))
                    if self.on_execute:
                        self.on_execute(name, args)
                elif msg_type == eh.PING_REQUEST:
                    send_msg(eh.PING_RESPONSE)
                elif msg_type == eh.PING_RESPONSE:
                    self.pongs += 1
                elif msg_type == eh.DISCONNECT_REQUEST:
                    self.disconnects += 1
                    send_msg(eh.DISCONNECT_RESPONSE)
                    await w.drain()
                    return
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
            self._writers.discard(w)
            w.close()
