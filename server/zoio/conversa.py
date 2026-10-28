"""Conversa com o portão: áudio entre o Pi e a placa do porteiro, por UDP cifrado.

A placa (componente `zoio_conversa` em deploy/esphome/components) só abre a
porta UDP depois que o Zóio roda a ação `conversa_iniciar(chave)` pela API
cifrada. A chave é nova a cada conversa (16 bytes aleatórios), então quem só
escuta a rede não entende o áudio, nem consegue falar no portão.

Pacote: 'Z' | tipo | contador (8 bytes, big-endian) | conteúdo cifrado | etiqueta (16).
AES-128-GCM com o cabeçalho como dado autenticado, e nonce
direção (1 byte) | 3 zeros | contador. Cada lado conta os pacotes que manda
e descarta o que chegar com contador repetido ou mais velho.

Áudio: PCM 16 bits little-endian, 16 kHz, mono, em quadros de até 20 ms.
O Zóio manda um "estou aqui" a cada segundo; sem ele a placa encerra sozinha.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import struct
import time
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

MAGIC = ord("Z")
AUDIO, ALIVE, END = 1, 2, 3
TO_BOARD, FROM_BOARD = 1, 2
HEADER = 10
TAG = 16
KEY_LEN = 16
FRAME = 640  # 20 ms de áudio
RATE = 32000  # bytes por segundo (16 kHz, 16 bits)
DEFAULT_PORT = 6054

ALIVE_EVERY = 1.0
ANSWER_TIMEOUT = 4.0  # a placa responde o primeiro pacote na hora
SILENT_AFTER = 5.0  # a placa manda "estou aqui" a cada segundo
MAX_SECONDS = 600  # a placa também para sozinha em 10 minutos
SEND_BURST = 16000  # quanto o app pode mandar de uma vez (0,5 s); depois, até 2x o tempo real


def nonce(direction: int, counter: int) -> bytes:
    return bytes([direction, 0, 0, 0]) + struct.pack(">Q", counter)


class Channel:
    """Cifra e confere os pacotes de uma conversa, do lado do Zóio."""

    def __init__(self, key: bytes):
        if len(key) != KEY_LEN:
            raise ValueError("a chave da conversa tem 16 bytes")
        self._aead = AESGCM(key)
        self.sent = 0
        self.received = 0

    def seal(self, kind: int, payload: bytes = b"") -> bytes:
        self.sent += 1
        header = struct.pack(">BBQ", MAGIC, kind, self.sent)
        return header + self._aead.encrypt(nonce(TO_BOARD, self.sent), payload, header)

    def open(self, packet: bytes) -> tuple[int, bytes] | None:
        """(tipo, conteúdo) de um pacote da placa, ou None se não for dela ou se for repetido."""
        if len(packet) < HEADER + TAG or packet[0] != MAGIC:
            return None
        counter = struct.unpack_from(">Q", packet, 2)[0]
        if counter <= self.received:
            return None
        try:
            payload = self._aead.decrypt(nonce(FROM_BOARD, counter), packet[HEADER:], packet[:HEADER])
        except InvalidTag:
            return None
        self.received = counter
        return packet[1], payload


class Call(asyncio.DatagramProtocol):
    """Uma conversa com a placa.

    `open()` abre o socket (antes de pedir a conversa para a placa), `begin()`
    manda o primeiro pacote e passa a vigiar a conversa, e `ready()` espera a
    placa responder. O áudio da placa chega em `on_audio`, e `wait()` devolve
    o motivo do fim.
    """

    def __init__(self, host: str, port: int, on_audio: Callable[[bytes], None] | None = None, max_seconds: float = MAX_SECONDS):
        self.host, self.port = host, port
        self.key = secrets.token_bytes(KEY_LEN)
        self.channel = Channel(self.key)
        self.on_audio = on_audio
        self.max_seconds = max_seconds
        self.started = time.monotonic()
        self._board_closed = False  # a placa mandou o fim: não precisa avisar de volta
        self.ended: asyncio.Future[str] | None = None
        self._ready = asyncio.Event()
        self._transport: asyncio.DatagramTransport | None = None
        self._watch: asyncio.Task | None = None
        self._last_rx = self._last_tx = 0.0
        self._budget, self._budget_at = float(SEND_BURST), time.monotonic()

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self.ended = loop.create_future()
        self._transport, _ = await loop.create_datagram_endpoint(lambda: self, remote_addr=(self.host, self.port))

    def begin(self) -> None:
        self.started = self._last_rx = time.monotonic()
        self._send(ALIVE)
        self._watch = asyncio.create_task(self._watchdog())

    async def ready(self) -> str | None:
        """Espera o primeiro pacote da placa. None se ela respondeu; senão, o motivo do fim."""
        waiter = asyncio.ensure_future(self._ready.wait())
        try:
            await asyncio.wait({waiter, self.ended}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        return self.ended.result() if self.ended.done() else None

    @property
    def answered(self) -> bool:
        """A placa já mandou algum pacote válido."""
        return self._ready.is_set()

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started

    async def wait(self) -> str:
        return await asyncio.shield(self.ended)

    def finish(self, reason: str) -> None:
        if self.ended is not None and not self.ended.done():
            self.ended.set_result(reason)

    async def close(self) -> None:
        """Avisa a placa (se ela já respondeu) e fecha o socket."""
        self.finish("encerrada")
        if self._watch is not None:
            self._watch.cancel()
        if self._transport is not None:
            if self.answered and not self._board_closed:
                self._send(END)
            self._transport.close()
            self._transport = None
        self.key = b""

    # ---- envio ----

    def send_audio(self, pcm: bytes) -> bool:
        """Manda áudio do app para a placa. Descarta (False) o que passar do ritmo da fala."""
        if self._transport is None or not self.answered:
            return False
        pcm = pcm[: len(pcm) & ~1]
        now = time.monotonic()
        self._budget = min(SEND_BURST, self._budget + (now - self._budget_at) * RATE * 2)
        self._budget_at = now
        if not pcm or len(pcm) > self._budget:
            return False
        self._budget -= len(pcm)
        for i in range(0, len(pcm), FRAME):
            self._send(AUDIO, pcm[i : i + FRAME])
        return True

    def _send(self, kind: int, payload: bytes = b"") -> None:
        if self._transport is None:
            return
        self._transport.sendto(self.channel.seal(kind, payload))
        self._last_tx = time.monotonic()

    async def _watchdog(self) -> None:
        while not self.ended.done():
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if not self.answered and now - self.started > ANSWER_TIMEOUT:
                self.finish("a placa não respondeu (o firmware tem a conversa?)")
            elif now - self._last_rx > SILENT_AFTER:
                self.finish("a placa parou de responder")
            elif now - self.started > self.max_seconds:
                self.finish("tempo máximo da conversa")
            elif now - self._last_tx >= ALIVE_EVERY:
                self._send(ALIVE)

    # ---- recebimento (asyncio.DatagramProtocol) ----

    def datagram_received(self, data: bytes, addr) -> None:
        opened = self.channel.open(data)
        if opened is None or self.ended.done():
            return
        kind, payload = opened
        self._ready.set()
        self._last_rx = time.monotonic()
        if kind == END:
            self._board_closed = True
            self.finish("a placa encerrou a conversa")
        elif kind == AUDIO and payload and self.on_audio:
            self.on_audio(payload)

    def error_received(self, exc: Exception) -> None:
        # ICMP "porta fechada": a placa ainda não abriu ou já fechou. O vigia resolve.
        log.debug("conversa: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.finish("conexão perdida")
