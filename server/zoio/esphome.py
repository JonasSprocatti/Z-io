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
4. ButtonCommandRequest(key), seguido de um Ping para confirmar que a placa
   processou o comando (a resposta ao ping só chega depois dele).

Quadros: 0x01, tamanho (u16 big-endian) e o conteúdo. Depois do handshake,
o conteúdo cifrado traz tipo (u16), tamanho (u16) e a mensagem protobuf.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import struct

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
LIST_ENTITIES_DONE = 19
LIST_ENTITIES_BUTTON = 61
BUTTON_COMMAND = 62


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
    data = value.encode()
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def pb_uint(field: int, value: int) -> bytes:
    return _varint(field << 3 | 0) + _varint(value)


def pb_fixed32(field: int, value: int) -> bytes:
    return _varint(field << 3 | 5) + struct.pack("<I", value)


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


class EspHome:
    """Uma conexão curta com a placa: abre, faz o que precisa e fecha."""

    def __init__(self, host: str, port: int, key: str, expected_name: str | None = None, timeout: float = 5.0, client_info: str = "zoio"):
        self.host, self.port = host, port
        self.psk = decode_key(key)
        self.expected_name = expected_name
        self.timeout = timeout
        self.client_info = client_info
        self.name: str | None = None
        self.mac: str | None = None
        self.server_info: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._send: _Cipher | None = None
        self._recv: _Cipher | None = None

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
            raise EspHomeError("a placa fechou a conexão") from exc

    def _send_message(self, msg_type: int, data: bytes = b"") -> None:
        self._write_frame(self._send.encrypt(struct.pack(">HH", msg_type, len(data)) + data))

    async def _read_message(self) -> tuple[int, bytes]:
        plain = self._recv.decrypt(await self._read_frame())
        if len(plain) < 4:
            raise EspHomeError("mensagem curta demais")
        msg_type = struct.unpack(">H", plain[:2])[0]
        return msg_type, plain[4:]

    async def _wait_for(self, *types: int) -> tuple[int, bytes]:
        while True:
            msg_type, data = await self._read_message()
            if msg_type in types:
                return msg_type, data
            if msg_type == PING_REQUEST:
                self._send_message(PING_RESPONSE)
                await self._writer.drain()
            elif msg_type == DISCONNECT_REQUEST:
                self._send_message(DISCONNECT_RESPONSE)
                await self._writer.drain()
                raise EspHomeError("a placa encerrou a conexão")
            # outras mensagens (estados, logs) não interessam aqui

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

    async def buttons(self) -> dict[str, tuple[int, str]]:
        """Botões da placa: object_id → (chave numérica, nome)."""
        self._send_message(LIST_ENTITIES_REQUEST)
        await self._writer.drain()
        found: dict[str, tuple[int, str]] = {}
        while True:
            msg_type, data = await asyncio.wait_for(self._wait_for(LIST_ENTITIES_BUTTON, LIST_ENTITIES_DONE), self.timeout)
            if msg_type == LIST_ENTITIES_DONE:
                return found
            fields = pb_parse(data)
            found[_text(fields.get(1))] = (int(fields.get(2, 0)), _text(fields.get(3)))

    async def press(self, object_id: str) -> str:
        """Aperta um botão e espera a placa confirmar. Devolve o object_id usado."""
        buttons = await self.buttons()
        if object_id not in buttons:
            if len(buttons) != 1:
                raise EspHomeError(f"botão {object_id!r} não existe na placa (achei: {', '.join(buttons) or 'nenhum'})")
            object_id = next(iter(buttons))
        self._send_message(BUTTON_COMMAND, pb_fixed32(1, buttons[object_id][0]))
        self._send_message(PING_REQUEST)
        await self._writer.drain()
        await asyncio.wait_for(self._wait_for(PING_RESPONSE), self.timeout)
        return object_id

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
