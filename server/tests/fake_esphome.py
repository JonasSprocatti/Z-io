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


class FakeEspHome:
    def __init__(self, key: str, name: str = "zoio-portao", buttons: dict | None = None, encrypted: bool = True):
        self.psk = eh.decode_key(key)
        self.name = name
        self.buttons = buttons if buttons is not None else {"abrir_portao": (0x1234ABCD, "Abrir portao")}
        self.encrypted = encrypted
        self.pressed: list[int] = []
        self.hello: dict | None = None
        self.pongs = 0
        self.disconnects = 0
        self.server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> "FakeEspHome":
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        async def frame() -> bytes:
            head = await r.readexactly(3)
            assert head[0] == 0x01, head
            return await r.readexactly(struct.unpack(">H", head[1:])[0])

        def send(payload: bytes) -> None:
            w.write(b"\x01" + struct.pack(">H", len(payload)) + payload)

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

            while True:
                plain = noise.decrypt(await frame())
                msg_type, data = struct.unpack(">H", plain[:2])[0], plain[4:]
                if msg_type == eh.HELLO_REQUEST:
                    self.hello = eh.pb_parse(data)
                    send_msg(eh.HELLO_RESPONSE, eh.pb_uint(1, 1) + eh.pb_uint(2, 10) + eh.pb_string(3, "fake 2026.10") + eh.pb_string(4, self.name))
                    send_msg(eh.PING_REQUEST)  # a placa real manda ping de tempos em tempos
                elif msg_type == eh.LIST_ENTITIES_REQUEST:
                    send_msg(16, eh.pb_string(1, "sinal_wifi") + eh.pb_fixed32(2, 7))  # um sensor qualquer
                    for object_id, (key, name) in self.buttons.items():
                        send_msg(eh.LIST_ENTITIES_BUTTON, eh.pb_string(1, object_id) + eh.pb_fixed32(2, key) + eh.pb_string(3, name))
                    send_msg(eh.LIST_ENTITIES_DONE)
                elif msg_type == eh.BUTTON_COMMAND:
                    self.pressed.append(eh.pb_parse(data)[1])
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
            w.close()
