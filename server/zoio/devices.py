"""Aprovação de aparelhos: cada aparelho tem uma chave própria e só entra se for aprovado.

Fluxo:
1. Na primeira vez, o app gera no aparelho um par de chaves ECDSA P-256 pela
   WebCrypto, com a chave privada marcada como não exportável. Ela nunca sai
   do aparelho, e nem o próprio app consegue lê-la.
2. O app registra a chave pública. O aparelho fica "aguardando" e mostra um
   código curto (impressão digital da chave) para ser conferido.
3. Alguém aprova o aparelho: um aparelho já aprovado com permissão de
   administrador, ou o dono do Pi pela linha de comando.
4. Para entrar, o aparelho assina um desafio aleatório de uso único. Com a
   assinatura válida, recebe uma sessão em cookie HttpOnly. A sessão é guardada
   só como hash e cai na hora em que o aparelho é revogado.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from .store import Store

PENDING, APPROVED, REVOKED = "pending", "approved", "revoked"
MAX_PENDING = 10
CHALLENGE_TTL = 120
MAX_CHALLENGES = 1000
LAST_SEEN_EVERY = 300


class DeviceError(Exception):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError) as exc:
        raise DeviceError("base64 inválido") from exc


def public_key_from_jwk(jwk: dict) -> bytes:
    """Valida a chave pública (JWK P-256) e devolve o ponto não comprimido."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise DeviceError("a chave precisa ser EC P-256")
    x, y = b64u_decode(str(jwk.get("x", ""))), b64u_decode(str(jwk.get("y", "")))
    if len(x) != 32 or len(y) != 32:
        raise DeviceError("chave com tamanho inválido")
    try:  # garante que o ponto está na curva
        ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1()).public_key()
    except ValueError as exc:
        raise DeviceError("chave inválida") from exc
    return b"\x04" + x + y


def fingerprint(raw_public_key: bytes) -> str:
    """Código curto para conferir a chave a olho: 8 letras/números (40 bits)."""
    digest = hashlib.sha256(raw_public_key).digest()
    code = base64.b32encode(digest[:5]).decode()
    return f"{code[:4]}-{code[4:]}"


def normalize_code(text: str) -> str:
    return "".join(ch for ch in text.upper() if ch.isalnum())


def login_message(nonce: str, device_id: str) -> bytes:
    return f"zoio-login\n{nonce}\n{device_id}".encode()


def gate_message(nonce: str, device_id: str) -> bytes:
    """O que o aparelho assina para abrir o portão (confirmação além da sessão)."""
    return f"zoio-portao\n{nonce}\n{device_id}".encode()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    device: dict
    token_hash: str
    expires_at: float


class Devices:
    def __init__(self, store: Store, session_days: int = 30):
        self.store = store
        self.session_seconds = session_days * 86400
        self._challenges: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}

    # ---- cadastro ----

    def register(self, jwk: dict, name: str, user: str | None, user_agent: str | None) -> tuple[dict, bool]:
        """Cadastra a chave (ou devolve o cadastro existente). Retorna (aparelho, é_novo)."""
        raw = public_key_from_jwk(jwk)
        name = " ".join(str(name or "").split())[:60] or "Aparelho sem nome"
        existing = self.store.device_by_key(b64u(raw))
        if existing:
            if existing["status"] == PENDING and existing["name"] != name:
                self.store.update_device(existing["id"], name=name)
                existing["name"] = name
            return existing, False
        pending = self.store.devices(status=PENDING)
        for old in pending[MAX_PENDING - 1 :]:  # lista vem do mais novo ao mais antigo
            self.store.delete_device(old["id"])
        device = {
            "id": secrets.token_hex(8),
            "name": name,
            "public_key": b64u(raw),
            "code": fingerprint(raw),
            "status": PENDING,
            "admin": 0,
            "user": user,
            "user_agent": (user_agent or "")[:200],
            "created_at": time.time(),
        }
        self.store.add_device(device)
        return self.store.device(device["id"]), True

    def find(self, ref: str) -> dict:
        """Acha por código (com ou sem traço) ou por começo do id."""
        ref_code = normalize_code(ref)
        matches = [d for d in self.store.devices() if normalize_code(d["code"]) == ref_code or (len(ref) >= 4 and d["id"].startswith(ref.lower()))]
        if not matches:
            raise DeviceError(f"nenhum aparelho com o código ou id {ref!r}")
        if len(matches) > 1:
            raise DeviceError("mais de um aparelho combina; use o id completo")
        return matches[0]

    def approve(self, device_id: str, admin: bool, by: str, gate: bool | None = None) -> dict:
        """Aprova. Sem dizer nada sobre o portão, só administradores podem abri-lo."""
        device = self.store.device(device_id)
        if device is None:
            raise DeviceError("aparelho não encontrado")
        if device["status"] == REVOKED:
            raise DeviceError("aparelho revogado; exclua e peça um novo cadastro")
        gate = admin if gate is None else gate
        self.store.update_device(device_id, status=APPROVED, admin=int(admin), gate=int(gate), approved_at=time.time(), approved_by=by)
        return self.store.device(device_id)

    def set_gate(self, device_id: str, allow: bool) -> dict:
        device = self.store.device(device_id)
        if device is None:
            raise DeviceError("aparelho não encontrado")
        if allow and device["status"] != APPROVED:
            raise DeviceError("só aparelhos aprovados podem abrir o portão")
        self.store.update_device(device_id, gate=int(allow))
        return self.store.device(device_id)

    def revoke(self, device_id: str) -> dict:
        device = self.store.device(device_id)
        if device is None:
            raise DeviceError("aparelho não encontrado")
        self.store.update_device(device_id, status=REVOKED, admin=0, gate=0)
        self.store.delete_sessions(device_id)
        self.store.delete_device_subscriptions(device_id)
        return self.store.device(device_id)

    def delete(self, device_id: str) -> None:
        self.store.delete_sessions(device_id)
        self.store.delete_device_subscriptions(device_id)
        self.store.delete_device(device_id)

    def admin_count(self) -> int:
        return sum(1 for d in self.store.devices(status=APPROVED) if d["admin"])

    # ---- entrada ----

    def challenge(self) -> str:
        now = time.time()
        if len(self._challenges) >= MAX_CHALLENGES:
            self._challenges = {n: exp for n, exp in self._challenges.items() if exp > now}
            if len(self._challenges) >= MAX_CHALLENGES:
                raise DeviceError("muitos pedidos de entrada; tente de novo em instantes")
        nonce = secrets.token_urlsafe(32)
        self._challenges[nonce] = now + CHALLENGE_TTL
        return nonce

    def verify(self, device_id: str, nonce: str, signature: str, message=login_message) -> dict:
        """Confere a assinatura do desafio. Devolve o aparelho (qualquer status)."""
        expires = self._challenges.pop(str(nonce), None)  # uso único, mesmo se falhar
        if expires is None or expires < time.time():
            raise DeviceError("desafio inválido ou expirado")
        device = self.store.device(str(device_id))
        if device is None:
            raise LookupError("aparelho desconhecido")
        sig = b64u_decode(str(signature))
        if len(sig) != 64:
            raise DeviceError("assinatura com tamanho inválido")
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64u_decode(device["public_key"]))
        try:
            key.verify(der, message(nonce, device["id"]), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature as exc:
            raise DeviceError("assinatura inválida") from exc
        return device

    def open_session(self, device_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.store.add_session(_hash_token(token), device_id, now, now + self.session_seconds)
        self.store.update_device(device_id, last_seen=now)
        self._last_seen[device_id] = now
        return token

    def session(self, token: str | None) -> Session | None:
        """Sessão válida de um aparelho aprovado, ou None. Renova a validade com o uso."""
        if not token:
            return None
        token_hash = _hash_token(token)
        row = self.store.session(token_hash)
        now = time.time()
        if row is None or row["expires_at"] < now:
            return None
        device = self.store.device(row["device_id"])
        if device is None or device["status"] != APPROVED:
            return None
        # Renova no máximo uma vez por dia para não escrever a cada pedido.
        if row["expires_at"] - now < self.session_seconds - 86400:
            row["expires_at"] = now + self.session_seconds
            self.store.extend_session(token_hash, row["expires_at"])
        if now - self._last_seen.get(device["id"], 0) > LAST_SEEN_EVERY:
            self._last_seen[device["id"]] = now
            self.store.update_device(device["id"], last_seen=now)
        return Session(device, token_hash, row["expires_at"])

    def close_session(self, token_hash: str) -> None:
        self.store.delete_session(token_hash)


def public(device: dict, current_id: str | None = None) -> dict:
    """Dados do aparelho que podem ir para o app (sem a chave)."""
    return {
        "id": device["id"],
        "name": device["name"],
        "code": device["code"],
        "status": device["status"],
        "admin": bool(device["admin"]),
        "gate": bool(device.get("gate")),
        "user": device.get("user"),
        "user_agent": device.get("user_agent"),
        "created_at": device["created_at"],
        "approved_at": device.get("approved_at"),
        "last_seen": device.get("last_seen"),
        "current": device["id"] == current_id,
    }
