"""Lado da placa na conversa (o componente zoio_conversa), simulado por UDP.

Segue o que o firmware faz em C++: abre a porta só depois de `start(chave)`,
aprende o endereço do Zóio no primeiro pacote autenticado, responde com
"estou aqui" e descarta pacotes repetidos ou de outra chave. Os pacotes são
montados aqui de novo, sem usar o conversa.py, para conferir o formato.
"""

from __future__ import annotations

import asyncio
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AUDIO, ALIVE, END = 1, 2, 3


def board_nonce(direction: int, counter: int) -> bytes:
    return bytes([direction]) + b"\x00\x00\x00" + counter.to_bytes(8, "big")


class FakeIntercom(asyncio.DatagramProtocol):
    def __init__(self):
        self.port = 0
        self.transport: asyncio.DatagramTransport | None = None
        self.aead: AESGCM | None = None
        self.peer = None
        self.tx = 0
        self.rx = 0
        self.received: list[tuple[int, bytes]] = []  # (tipo, conteúdo) de cada pacote aceito
        self.rejected = 0
        self.raw: list[bytes] = []  # pacotes como chegaram (para testar repetição)
        self.silent = False  # não responde (firmware sem a conversa)
        self.starts = 0

    async def open(self) -> "FakeIntercom":
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=("127.0.0.1", 0))
        self.port = self.transport.get_extra_info("sockname")[1]
        return self

    def close(self) -> None:
        if self.transport:
            self.transport.close()

    @property
    def active(self) -> bool:
        return self.aead is not None

    def start(self, key_hex: str) -> None:
        self.starts += 1
        self.aead = AESGCM(bytes.fromhex(key_hex))
        self.peer, self.tx, self.rx = None, 0, 0

    def stop(self) -> None:
        if self.active and self.peer:
            self.send(END)
        self.aead = None

    def audio(self) -> bytes:
        return b"".join(p for kind, p in self.received if kind == AUDIO)

    def send(self, kind: int, payload: bytes = b"") -> None:
        self.tx += 1
        header = b"Z" + bytes([kind]) + struct.pack(">Q", self.tx)
        sealed = self.aead.encrypt(board_nonce(2, self.tx), payload, header)
        self.transport.sendto(header + sealed, self.peer)

    def datagram_received(self, data: bytes, addr) -> None:
        self.raw.append(data)
        if not self.active or self.silent:
            return
        if len(data) < 26 or data[0:1] != b"Z":
            self.rejected += 1
            return
        counter = int.from_bytes(data[2:10], "big")
        if counter <= self.rx:
            self.rejected += 1
            return
        try:
            payload = self.aead.decrypt(board_nonce(1, counter), data[10:], data[:10])
        except InvalidTag:
            self.rejected += 1
            return
        self.rx, self.peer = counter, addr
        self.received.append((data[1], payload))
        if data[1] == END:
            self.aead = None
        elif data[1] == ALIVE:
            self.send(ALIVE)
