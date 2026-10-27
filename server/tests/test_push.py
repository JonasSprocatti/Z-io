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
