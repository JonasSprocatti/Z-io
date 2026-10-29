"""Cliente mínimo da API nativa do ESPHome, só com criptografia (Noise).

É o protocolo que o Home Assistant usa para falar com placas ESPHome, na
porta 6053. A conexão é cifrada e autenticada por uma chave de 32 bytes
(`api: encryption: key:` na placa): sem a chave certa, ninguém da rede
consegue mandar comandos, nem ler o que passa.

Implementado direto com `cryptography`, como o Web Push, porque o
`aioesphomeapi` puxa dependências que não existem no apt do Pi.

Fluxo:
1. Handshake Noise_NNpsk0_25519_ChaChaPoly_SHA256 (prólogo "NoiseAPIInit\\0\\0").
2. HelloRequest → HelloResponse (nome da placa e versão da API).
3. ListEntitiesRequest → um ListEntities*Response por entidade, até o Done.
4. SubscribeStatesRequest: a placa manda o estado atual de cada sensor e,
   depois, cada mudança (campainha, sensores das garagens).
5. ButtonCommandRequest(key), seguido de um Ping para confirmar que a placa
   processou o comando (a resposta ao ping só chega depois dele).
6. SubscribeLogsRequest, só para aprender códigos de rádio: o receptor da
   placa escreve no log cada código que escuta.
7. ExecuteServiceRequest(key, args): roda uma ação da placa (`api: actions:`),
   como `conversa_iniciar(chave)`, também confirmada por um Ping.

Quadros: 0x01, tamanho (u16 big-endian) e o conteúdo. Depois do handshake,
o conteúdo cifrado traz tipo (u16), tamanho (u16) e a mensagem protobuf.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import hmac
import re
import secrets
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PROTOCOL_NAME = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
PROLOGUE = b"NoiseAPIInit\x00\x00"
DEFAULT_PORT = 6053

# Tipos de mensagem (api.proto do ESPHome)
HELLO_REQUEST = 1
HELLO_RESPONSE = 2
AUTH_REQUEST = 3
DISCONNECT_REQUEST = 5
DISCONNECT_RESPONSE = 6
PING_REQUEST = 7
PING_RESPONSE = 8
LIST_ENTITIES_REQUEST = 11
LIST_ENTITIES_BINARY_SENSOR = 12
LIST_ENTITIES_SENSOR = 16
LIST_ENTITIES_DONE = 19
SUBSCRIBE_STATES = 20
BINARY_SENSOR_STATE = 21
SENSOR_STATE = 25
SUBSCRIBE_LOGS = 28
SUBSCRIBE_LOGS_RESPONSE = 29
LIST_ENTITIES_SERVICES = 41
EXECUTE_SERVICE = 42
LIST_ENTITIES_BUTTON = 61
BUTTON_COMMAND = 62

LOG_LEVEL_INFO = 3

KEEPALIVE = 20  # segundos sem ouvir a placa até mandar um ping
DEAD_AFTER = 50  # segundos sem resposta nenhuma: conexão perdida


class EspHomeError(Exception):
    pass


def decode_key(text: str) -> bytes:
    try:
        key = base64.b64decode(text.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise EspHomeError("chave da placa inválida (base64)") from exc
    if len(key) != 32:
        raise EspHomeError("chave da placa inválida (precisa ter 32 bytes)")
    return key


# ---------------------------------------------------------------- protobuf


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb_string(field: int, value: str) -> bytes:
    return pb_bytes(field, value.encode())


def pb_uint(field: int, value: int) -> bytes:
    return _varint(field << 3 | 0) + _varint(value)


def pb_bytes(field: int, data: bytes) -> bytes:
    """Campo com mensagem dentro (ou bytes crus)."""
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def pb_fixed32(field: int, value: int) -> bytes:
    return _varint(field << 3 | 5) + struct.pack("<I", value)


def pb_float(field: int, value: float) -> bytes:
    return _varint(field << 3 | 5) + struct.pack("<f", value)


def pb_parse(data: bytes) -> dict[int, object]:
    """Lê os campos de uma mensagem (só o último valor de cada campo)."""
    fields: dict[int, object] = {}
    pos = 0

    def varint() -> int:
        nonlocal pos
        shift = result = 0
        while True:
            if pos >= len(data):
                raise EspHomeError("mensagem cortada")
            b = data[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    while pos < len(data):
        key = varint()
        field, wire = key >> 3, key & 7
        if wire == 0:
            fields[field] = varint()
        elif wire == 1:
            fields[field] = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
        elif wire == 2:
            size = varint()
            fields[field] = data[pos : pos + size]
            pos += size
        elif wire == 5:
            fields[field] = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        else:
            raise EspHomeError(f"tipo de campo desconhecido: {wire}")
    return fields


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else ""


def object_id(name: str) -> str:
    """O nome interno que o ESPHome tira do nome da entidade ("Sinal Wi-Fi" -> "sinal_wi-fi").

    Desde 2026 a placa manda o object_id vazio e cada cliente calcula, com a
    mesma regra do ESPHome (snake_case e depois sanitize).
    """
    return re.sub(r"[^a-zA-Z0-9-_]", "_", name.replace(" ", "_").lower())


# ---------------------------------------------------------------- Noise


def _hkdf(chaining_key: bytes, material: bytes, outputs: int) -> tuple[bytes, ...]:
    temp = hmac.new(chaining_key, material, hashlib.sha256).digest()
    out = [hmac.new(temp, b"\x01", hashlib.sha256).digest()]
    for i in range(2, outputs + 1):
        out.append(hmac.new(temp, out[-1] + bytes([i]), hashlib.sha256).digest())
    return tuple(out)


class _Cipher:
    def __init__(self, key: bytes):
        self._aead = ChaCha20Poly1305(key)
        self.n = 0

    def _nonce(self) -> bytes:
        nonce = b"\x00\x00\x00\x00" + struct.pack("<Q", self.n)
        self.n += 1
        return nonce

    def encrypt(self, data: bytes, ad: bytes = b"") -> bytes:
        return self._aead.encrypt(self._nonce(), data, ad)

    def decrypt(self, data: bytes, ad: bytes = b"") -> bytes:
        try:
            return self._aead.decrypt(self._nonce(), data, ad)
        except InvalidTag as exc:
            raise EspHomeError("mensagem da placa não confere (chave errada?)") from exc


class _Handshake:
    """Lado de quem inicia o padrão NNpsk0: `-> psk, e` e `<- e, ee`."""

    def __init__(self, psk: bytes):
        self.h = hashlib.sha256(PROTOCOL_NAME).digest()  # nome com mais de 32 bytes vira hash
        self.ck = self.h
        self.cipher: _Cipher | None = None
        self.psk = psk
        self.e = X25519PrivateKey.generate()
        self._mix_hash(PROLOGUE)

    def _mix_hash(self, data: bytes) -> None:
        self.h = hashlib.sha256(self.h + data).digest()

    def _mix_key(self, material: bytes) -> None:
        self.ck, key = _hkdf(self.ck, material, 2)
        self.cipher = _Cipher(key)

    def _mix_key_and_hash(self, material: bytes) -> None:
        self.ck, temp_h, key = _hkdf(self.ck, material, 3)
        self._mix_hash(temp_h)
        self.cipher = _Cipher(key)

    def write_message(self) -> bytes:
        self._mix_key_and_hash(self.psk)
        e_pub = self.e.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self._mix_hash(e_pub)
        self._mix_key(e_pub)  # num handshake com psk, o "e" também entra na chave
        tag = self.cipher.encrypt(b"", self.h)  # conteúdo vazio, só a autenticação
        self._mix_hash(tag)
        return e_pub + tag

    def read_message(self, message: bytes) -> tuple[_Cipher, _Cipher]:
        if len(message) < 48:
            raise EspHomeError("resposta do handshake curta demais")
        re_pub, rest = message[:32], message[32:]
        self._mix_hash(re_pub)
        self._mix_key(re_pub)
        self._mix_key(self.e.exchange(X25519PublicKey.from_public_bytes(re_pub)))
        self.cipher.decrypt(rest, self.h)
        self._mix_hash(rest)
        send_key, recv_key = _hkdf(self.ck, b"", 2)
        return _Cipher(send_key), _Cipher(recv_key)


# ---------------------------------------------------------------- conexão


@dataclass
class Entities:
    """Entidades da placa, por object_id: (chave numérica, nome)."""

    buttons: dict[str, tuple[int, str]] = field(default_factory=dict)
    binary_sensors: dict[str, tuple[int, str]] = field(default_factory=dict)
    sensors: dict[str, tuple[int, str]] = field(default_factory=dict)  # numéricos (sinal do Wi-Fi...)
    services: dict[str, int] = field(default_factory=dict)  # ações (`api: actions:`), por nome: chave


class EspHome:
    """Conexão com a placa.

    Dá para usar de dois jeitos: curta (conecta, aperta um botão e fecha, como
    no `zoio portao testar`) ou permanente, com `run()` lendo tudo o que a placa
    manda e entregando as mudanças de estado em `on_state(chave, estado)`.
    """

    def __init__(self, host: str, port: int, key: str, expected_name: str | None = None, timeout: float = 5.0, client_info: str = "zoio"):
        self.host, self.port = host, port
        self.psk = decode_key(key)
        self.expected_name = expected_name
        self.timeout = timeout
        self.client_info = client_info
        self.name: str | None = None
        self.mac: str | None = None
        self.server_info: str | None = None
        self.on_state: Callable[[int, bool], None] | None = None  # sensores binários
        self.on_value: Callable[[int, float], None] | None = None  # sensores numéricos
        self.on_log: Callable[[str], None] | None = None  # linhas do log, depois de subscribe_logs()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._send: _Cipher | None = None
        self._recv: _Cipher | None = None
        self._pongs: collections.deque[asyncio.Future] = collections.deque()
        self._reading = False
        self._last_rx = 0.0
        self._dead = False

    async def __aenter__(self) -> "EspHome":
        try:
            await asyncio.wait_for(self._connect(), self.timeout)
        except asyncio.TimeoutError as exc:
            await self.close()
            raise EspHomeError(f"a placa ({self.host}) não respondeu a tempo") from exc
        except OSError as exc:
            await self.close()
            raise EspHomeError(f"não consegui conectar na placa ({self.host}): {exc.strerror or exc}") from exc
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---- quadros ----

    def _write_frame(self, payload: bytes) -> None:
        self._writer.write(b"\x01" + struct.pack(">H", len(payload)) + payload)

    async def _read_frame(self) -> bytes:
        try:
            header = await self._reader.readexactly(3)
            if header[0] != 0x01:
                if header[0] == 0x00:
                    raise EspHomeError("a placa está sem criptografia na API; configure a chave (api: encryption: key:)")
                raise EspHomeError(f"resposta inesperada da placa (0x{header[0]:02x})")
            return await self._reader.readexactly(struct.unpack(">H", header[1:])[0])
        except asyncio.IncompleteReadError as exc:
            raise EspHomeError("a placa parou de responder" if self._dead else "a placa fechou a conexão") from exc

    def _send_message(self, msg_type: int, data: bytes = b"") -> None:
        # Sem await no meio: cifrar e escrever acontecem juntos, na ordem do contador.
        self._write_frame(self._send.encrypt(struct.pack(">HH", msg_type, len(data)) + data))

    async def _read_message(self) -> tuple[int, bytes]:
        plain = self._recv.decrypt(await self._read_frame())
        if len(plain) < 4:
            raise EspHomeError("mensagem curta demais")
        self._last_rx = time.monotonic()
        msg_type = struct.unpack(">H", plain[:2])[0]
        return msg_type, plain[4:]

    async def _dispatch(self, msg_type: int, data: bytes) -> None:
        """Trata o que é da própria conexão (pings, desconexão, estados)."""
        if msg_type == PING_REQUEST:
            self._send_message(PING_RESPONSE)
            await self._writer.drain()
        elif msg_type == PING_RESPONSE:
            if self._pongs:
                fut = self._pongs.popleft()
                if not fut.done():
                    fut.set_result(None)
        elif msg_type == DISCONNECT_REQUEST:
            self._send_message(DISCONNECT_RESPONSE)
            await self._writer.drain()
            raise EspHomeError("a placa encerrou a conexão")
        elif msg_type == BINARY_SENSOR_STATE and self.on_state:
            fields = pb_parse(data)
            if not fields.get(3):  # missing_state: sensor ainda sem leitura
                self.on_state(int(fields.get(1, 0)), bool(fields.get(2, 0)))
        elif msg_type == SENSOR_STATE and self.on_value:
            fields = pb_parse(data)
            if not fields.get(3):
                value = struct.unpack("<f", struct.pack("<I", int(fields.get(2, 0))))[0]
                self.on_value(int(fields.get(1, 0)), value)
        elif msg_type == SUBSCRIBE_LOGS_RESPONSE and self.on_log:
            self.on_log(_text(pb_parse(data).get(3)))
        # outras mensagens (logs, outros sensores) não interessam

    async def _wait_for(self, *types: int) -> tuple[int, bytes]:
        while True:
            msg_type, data = await self._read_message()
            if msg_type in types:
                return msg_type, data
            await self._dispatch(msg_type, data)

    # ---- etapas ----

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        handshake = _Handshake(self.psk)
        self._write_frame(b"")  # "client hello"
        self._write_frame(b"\x00" + handshake.write_message())
        await self._writer.drain()

        hello = await self._read_frame()
        if not hello or hello[0] != 0x01:
            raise EspHomeError("a placa não aceitou o protocolo de criptografia")
        parts = hello[1:].split(b"\x00")
        self.name = parts[0].decode("utf-8", "replace") if parts and parts[0] else None
        self.mac = parts[1].decode("utf-8", "replace") if len(parts) > 2 and parts[1] else None
        if self.expected_name and self.name and self.name != self.expected_name:
            raise EspHomeError(f"a placa se apresentou como {self.name!r}, esperado {self.expected_name!r}")

        reply = await self._read_frame()
        if not reply:
            raise EspHomeError("resposta do handshake vazia")
        if reply[0] != 0x00:
            reason = reply[1:].decode("utf-8", "replace")
            if reason == "Handshake MAC failure":
                raise EspHomeError("a placa recusou a chave (ZOIO_GATE_KEY diferente da chave gravada na placa)")
            raise EspHomeError(f"a placa recusou a conexão: {reason}")
        self._send, self._recv = handshake.read_message(reply[1:])

        self._send_message(HELLO_REQUEST, pb_string(1, self.client_info) + pb_uint(2, 1) + pb_uint(3, 10))
        self._send_message(AUTH_REQUEST)  # sem senha; versões antigas do ESPHome esperam por ele
        await self._writer.drain()
        _, data = await self._wait_for(HELLO_RESPONSE)
        fields = pb_parse(data)
        self.server_info = _text(fields.get(3))
        self.name = _text(fields.get(4)) or self.name
        if self.expected_name and self.name != self.expected_name:
            raise EspHomeError(f"a placa se apresentou como {self.name!r}, esperado {self.expected_name!r}")

    async def entities(self) -> Entities:
        """Lista botões, sensores e ações. Use antes de `run()`."""
        self._send_message(LIST_ENTITIES_REQUEST)
        await self._writer.drain()
        found = Entities()
        kinds = {LIST_ENTITIES_BUTTON: found.buttons, LIST_ENTITIES_BINARY_SENSOR: found.binary_sensors, LIST_ENTITIES_SENSOR: found.sensors}
        while True:
            msg_type, data = await asyncio.wait_for(self._wait_for(LIST_ENTITIES_DONE, LIST_ENTITIES_SERVICES, *kinds), self.timeout)
            if msg_type == LIST_ENTITIES_DONE:
                return found
            fields = pb_parse(data)
            if msg_type == LIST_ENTITIES_SERVICES:
                found.services[_text(fields.get(1))] = int(fields.get(2, 0))
            else:
                name = _text(fields.get(3))
                kinds[msg_type][_text(fields.get(1)) or object_id(name)] = (int(fields.get(2, 0)), name)

    async def subscribe_states(self) -> None:
        self._send_message(SUBSCRIBE_STATES)
        await self._writer.drain()

    async def subscribe_logs(self, level: int = LOG_LEVEL_INFO) -> None:
        self._send_message(SUBSCRIBE_LOGS, pb_uint(1, level))
        await self._writer.drain()

    async def ping(self) -> None:
        """Espera a placa responder um ping: tudo o que foi mandado antes já foi tratado."""
        fut = asyncio.get_running_loop().create_future()
        self._pongs.append(fut)
        self._send_message(PING_REQUEST)
        await self._writer.drain()
        if self._reading:
            await asyncio.wait_for(asyncio.shield(fut), self.timeout)
            return

        async def read_until_pong() -> None:
            while not fut.done():
                await self._dispatch(*await self._read_message())

        await asyncio.wait_for(read_until_pong(), self.timeout)

    async def press(self, key: int) -> None:
        """Aperta o botão de chave `key` e espera a placa confirmar."""
        self._send_message(BUTTON_COMMAND, pb_fixed32(1, key))
        try:
            await self.ping()
        except asyncio.TimeoutError as exc:
            raise EspHomeError("a placa não confirmou o comando") from exc

    async def execute(self, key: int, *args: str) -> None:
        """Roda a ação de chave `key` (argumentos de texto, na ordem) e espera a placa confirmar."""
        data = pb_fixed32(1, key) + b"".join(pb_bytes(2, pb_string(4, arg)) for arg in args)
        self._send_message(EXECUTE_SERVICE, data)
        try:
            await self.ping()
        except asyncio.TimeoutError as exc:
            raise EspHomeError("a placa não confirmou a ação") from exc

    async def run(self) -> None:
        """Lê a conexão até ela cair (sempre termina com EspHomeError)."""
        self._reading = True
        self._last_rx = time.monotonic()
        keepalive = asyncio.create_task(self._keepalive())
        try:
            while True:
                await self._dispatch(*await self._read_message())
        finally:
            self._reading = False
            keepalive.cancel()
            while self._pongs:
                fut = self._pongs.popleft()
                if not fut.done():
                    fut.set_exception(EspHomeError("a conexão com a placa caiu"))

    async def _keepalive(self) -> None:
        """Pinga a placa quando ela fica quieta; sem resposta, derruba a conexão."""
        while True:
            await asyncio.sleep(KEEPALIVE / 2)
            idle = time.monotonic() - self._last_rx
            if idle > DEAD_AFTER:
                self._dead = True
                self._writer.close()
                return
            if idle > KEEPALIVE:
                fut = asyncio.get_running_loop().create_future()
                fut.add_done_callback(lambda f: f.exception())  # ninguém espera este
                self._pongs.append(fut)
                self._send_message(PING_REQUEST)
                try:
                    await self._writer.drain()
                except OSError:
                    return

    async def close(self) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            if self._send is not None and not writer.is_closing():
                self._send_message(DISCONNECT_REQUEST)
                await asyncio.wait_for(writer.drain(), 1)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            self._writer = None
            writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 1)
        except (OSError, asyncio.TimeoutError):
            pass


def new_key() -> str:
    """Chave nova no formato que o ESPHome usa (base64 de 32 bytes)."""
    return base64.b64encode(secrets.token_bytes(32)).decode()
