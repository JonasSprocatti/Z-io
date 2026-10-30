"""Notificações Web Push (RFC 8291 + VAPID, RFC 8292) sem dependências extras.

O conteúdo vai cifrado de ponta a ponta: o serviço de push do Google/Apple só
entrega a mensagem e não consegue lê-la. O Pi apenas faz conexões de saída.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import struct
import time
from urllib.parse import urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .store import Store

log = logging.getLogger(__name__)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _raw_public(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None, sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """Cifra no formato aes128gcm de um único registro."""
    ua_public = b64u_decode(p256dh)
    auth_secret = b64u_decode(auth)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(sender)
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack(">IB", 4096, len(as_public)) + as_public + ciphertext


class Vapid:
    def __init__(self, store: Store, contact: str):
        self.contact = contact
        pem = store.get("vapid_private_pem")
        if pem is None:
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            store.set("vapid_private_pem", pem)
        self.key = serialization.load_pem_private_key(pem.encode(), None)
        self.public_key = b64u(_raw_public(self.key))

    def header(self, endpoint: str) -> str:
        parts = urlsplit(endpoint)
        claims = {"aud": f"{parts.scheme}://{parts.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": self.contact}
        signing_input = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode()) + "." + b64u(json.dumps(claims).encode())
        r, s = decode_dss_signature(self.key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
        token = signing_input + "." + b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return f"vapid t={token}, k={self.public_key}"


class Notifier:
    """Envia alertas de movimento, com intervalo mínimo por câmera."""

    def __init__(self, store: Store, contact: str, cooldown: int, channels: set[int], approved_only: bool = False):
        self.store = store
        # Com a aprovação de aparelhos ligada, só aparelhos aprovados recebem avisos.
        self.approved_only = approved_only
        self._last_device_alert = 0.0
        self.vapid = Vapid(store, contact)
        self.cooldown = cooldown
        self.channels = channels
        self._last: dict[int, float] = {}
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    def should_notify(self, camera: int, now: float | None = None) -> bool:
        if self.channels and camera not in self.channels:
            return False
        now = now or time.time()
        if now - self._last.get(camera, 0) < self.cooldown:
            return False
        self._last[camera] = now
        return True

    async def motion(self, camera: int, name: str, event_id: int) -> int:
        """Avisa todos os aparelhos inscritos. Devolve quantos receberam."""
        payload = json.dumps(
            {"title": f"Movimento: {name}", "body": time.strftime("%H:%M:%S"), "camera": camera, "event": event_id, "tag": f"cam{camera}"}
        ).encode()
        sent = 0
        for item in self.store.subscriptions(approved_only=self.approved_only):
            if item["cameras"] and camera not in item["cameras"]:
                continue
            if await self.send(item["sub"], payload):
                sent += 1
        return sent

    async def device_request(self, name: str, code: str) -> int:
        """Avisa os aparelhos administradores que um aparelho novo pediu acesso."""
        now = time.time()
        if now - self._last_device_alert < 60:  # evita enxurrada de pedidos
            return 0
        self._last_device_alert = now
        payload = json.dumps(
            {"title": "Aparelho novo pedindo acesso", "body": f"{name} · código {code}", "tag": "aparelho", "url": "/#ajustes"},
            ensure_ascii=False,
        ).encode()
        sent = 0
        for item in self.store.subscriptions(admins_only=True):
            if await self.send(item["sub"], payload):
                sent += 1
        return sent

    async def door_action(self, door: str, status: str, by: str, device_id: str | None) -> int:
        """Avisa os outros administradores que alguém mexeu no portão ou numa garagem."""
        payload = json.dumps(
            {"title": f"{door}: {status.lower()}", "body": f"por {by}", "tag": "portaria", "url": "/#alertas"},
            ensure_ascii=False,
        ).encode()
        sent = 0
        for item in self.store.subscriptions(admins_only=True):
            if item["device_id"] != device_id and await self.send(item["sub"], payload):
                sent += 1
        return sent

    async def doorbell(self, house: int = 1, name: str | None = None) -> int:
        """Campainha: avisa os aparelhos que podem atender (permissão de portaria).

        Com uma campainha por casa (`name`), só os aparelhos daquela casa e os sem casa.
        """
        payload = json.dumps(
            {"title": f"Campainha: {name}" if name else "Campainha", "body": f"Tocaram no portão às {time.strftime('%H:%M')}",
             "tag": f"campainha-{house}", "url": "/#portaria", "urgent": True},
            ensure_ascii=False,
        ).encode()
        sent = 0
        for item in self.store.subscriptions(gate_only=True, house=house if name else None):
            if await self.send(item["sub"], payload):
                sent += 1
        return sent

    async def send(self, sub: dict, payload: bytes) -> bool:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        endpoint = sub["endpoint"]
        body = encrypt(payload, sub["keys"]["p256dh"], sub["keys"]["auth"])
        headers = {
            "Authorization": self.vapid.header(endpoint),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": "3600",
            "Urgency": "high",
        }
        try:
            async with self._session.post(endpoint, data=body, headers=headers) as resp:
                if resp.status in (404, 410):
                    self.store.delete_subscription(endpoint)
                    return False
                if resp.status >= 400:
                    log.warning("push recusado (%s): %s", resp.status, (await resp.text())[:200])
                    return False
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("push falhou: %s", exc)
            return False
