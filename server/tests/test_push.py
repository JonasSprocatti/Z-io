import base64
import json
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from zoio import push
from zoio.store import Store


def hkdf(salt, ikm, info, n):
    return HKDF(algorithm=hashes.SHA256(), length=n, salt=salt, info=info).derive(ikm)


def test_encrypt_round_trip():
    """Decifra como um navegador faria (RFC 8291)."""
    ua = ec.generate_private_key(ec.SECP256R1())
    ua_pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    body = push.encrypt(b'{"oi":1}', push.b64u(ua_pub), push.b64u(auth))

    salt, rs, idlen = body[:16], *struct.unpack(">IB", body[16:21])
    as_pub = body[21 : 21 + idlen]
    assert rs == 4096 and idlen == 65
    shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen :], None)
    assert plain == b'{"oi":1}\x02'


def test_vapid_header_is_valid_jwt(tmp_path):
    store = Store(tmp_path / "z.db")
    vapid = push.Vapid(store, "mailto:a@b.c")
    assert push.Vapid(store, "mailto:a@b.c").public_key == vapid.public_key  # chave persiste
    header = vapid.header("https://fcm.googleapis.com/fcm/send/abc")
    token = header.split("t=")[1].split(",")[0]
    h, c, sig = token.split(".")
    claims = json.loads(push.b64u_decode(c))
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "mailto:a@b.c"
    raw = push.b64u_decode(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.b64u_decode(vapid.public_key))
    pub.verify(der, f"{h}.{c}".encode(), ec.ECDSA(hashes.SHA256()))


def test_cooldown_and_channel_filter(tmp_path):
    n = push.Notifier(Store(tmp_path / "z.db"), "mailto:x", cooldown=300, channels={1, 2})
    assert n.should_notify(1, now=1000)
    assert not n.should_notify(1, now=1100)
    assert n.should_notify(1, now=1301)
    assert not n.should_notify(3, now=1000)


async def test_device_request_goes_to_admins_and_is_throttled(tmp_path):
    store = Store(tmp_path / "z.db")
    n = push.Notifier(store, "mailto:x", cooldown=300, channels=set(), approved_only=True)
    store.add_device({"id": "a1", "name": "Admin", "public_key": "k1", "code": "AAAA-AAAA", "status": "approved", "admin": 1, "created_at": 1})
    store.add_device({"id": "c1", "name": "Comum", "public_key": "k2", "code": "BBBB-BBBB", "status": "approved", "admin": 0, "created_at": 2})
    store.save_subscription({"endpoint": "https://p/admin", "keys": {}}, None, None, "a1")
    store.save_subscription({"endpoint": "https://p/comum", "keys": {}}, None, None, "c1")
    sent = []

    async def fake_send(sub, payload):
        sent.append((sub["endpoint"], json.loads(payload)))
        return True

    n.send = fake_send
    assert await n.device_request("Tablet", "CCCC-CCCC") == 1
    assert sent[0][0] == "https://p/admin" and "CCCC-CCCC" in sent[0][1]["body"] and sent[0][1]["url"] == "/#ajustes"
    assert await n.device_request("Outro", "DDDD-DDDD") == 0  # menos de 1 minuto depois
    # movimento vai para os dois aprovados
    sent.clear()
    assert await n.motion(1, "Portão", 1) == 2
