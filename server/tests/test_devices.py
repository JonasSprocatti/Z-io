import base64
import subprocess
import sys
import time

import pytest
from aiohttp import web
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from zoio import devices as dev
from zoio.app import SESSION_COOKIE, create_app
from zoio.store import Store


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Phone:
    """Imita o que o app faz no navegador com a WebCrypto."""

    def __init__(self, name="Celular do Jonas"):
        self.name = name
        self.key = ec.generate_private_key(ec.SECP256R1())
        nums = self.key.public_key().public_numbers()
        self.jwk = {"kty": "EC", "crv": "P-256", "x": b64u(nums.x.to_bytes(32, "big")), "y": b64u(nums.y.to_bytes(32, "big"))}
        self.id = None
        self.code = None
        self.cookie = None

    def sign(self, nonce: str, device_id: str, message=dev.login_message) -> str:
        r, s = decode_dss_signature(self.key.sign(message(nonce, device_id), ec.ECDSA(hashes.SHA256())))
        return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    async def register(self, client):
        resp = await client.post("/api/devices/register", json={"public_key": self.jwk, "name": self.name})
        data = await resp.json()
        self.id, self.code = data.get("id"), data.get("code")
        return resp.status, data

    async def login(self, client, device_id=None, tamper=False):
        nonce = (await (await client.post("/api/devices/challenge")).json())["nonce"]
        sig = self.sign(nonce, device_id or self.id)
        if tamper:
            sig = self.sign(nonce + "x", device_id or self.id)
        resp = await client.post("/api/devices/login", json={"id": device_id or self.id, "nonce": nonce, "signature": sig})
        if resp.status == 200 and SESSION_COOKIE in resp.cookies:
            morsel = resp.cookies[SESSION_COOKIE]
            assert morsel["httponly"] and morsel["secure"] and morsel["samesite"] == "Strict"
            self.cookie = morsel.value
        return resp.status, await resp.json()

    @property
    def h(self):
        return {"Cookie": f"{SESSION_COOKIE}={self.cookie}"} if self.cookie else {}


@pytest.fixture
async def client(aiohttp_client, make_cfg):
    cfg = make_cfg(1, alerts=False)
    app = create_app(cfg)
    c = await aiohttp_client(app)
    c.store = app["zoio_store"]
    c.devices = app["zoio_devices"]
    return c


async def pair_admin(client) -> Phone:
    """Primeiro aparelho: aprovado como administrador pela linha de comando do Pi."""
    phone = Phone("Admin")
    await phone.register(client)
    client.devices.approve(phone.id, admin=True, by="pi")
    assert (await phone.login(client))[0] == 200
    return phone


def test_fingerprint_and_key_validation():
    phone = Phone()
    raw = dev.public_key_from_jwk(phone.jwk)
    code = dev.fingerprint(raw)
    assert len(code) == 9 and code[4] == "-" and dev.normalize_code(code.lower()) == code.replace("-", "")
    bad = dict(phone.jwk, y=phone.jwk["x"])  # ponto fora da curva
    with pytest.raises(dev.DeviceError):
        dev.public_key_from_jwk(bad)
    with pytest.raises(dev.DeviceError):
        dev.public_key_from_jwk({"kty": "RSA"})


async def test_everything_locked_without_session(client):
    for path in ("/api/config", "/api/events", "/api/status", "/api/recordings?camera=1", "/live/ws?src=cam1_sub", "/api/clips/0123456789abcdef/video.mp4"):
        assert (await client.get(path)).status == 401, path
    # a casca do app e o cadastro continuam abertos
    assert (await client.get("/")).status == 200
    assert (await client.get("/web/app.js")).status == 200
    me = await (await client.get("/api/devices/me")).json()
    assert me == {"required": True, "device": None, "has_admin": False}


async def test_pairing_flow(client):
    phone = Phone()
    status, data = await phone.register(client)
    assert status == 200 and data["status"] == "pending"
    assert data["code"] == dev.fingerprint(dev.public_key_from_jwk(phone.jwk))

    # registrar de novo com a mesma chave não cria outro aparelho
    await phone.register(client)
    assert len(client.store.devices()) == 1

    # aguardando: assinatura válida, mas ainda sem acesso
    status, data = await phone.login(client)
    assert status == 200 and data == {"status": "pending", "code": phone.code} and phone.cookie is None

    client.devices.approve(phone.id, admin=False, by="pi")
    status, data = await phone.login(client)
    assert status == 200 and data["device"]["current"] is True
    resp = await client.get("/api/config", headers=phone.h)
    assert resp.status == 200
    assert (await resp.json())["device"]["name"] == "Celular do Jonas"
    assert "Content-Security-Policy" in resp.headers

    # revogado: a sessão cai na hora e o cadastro não volta sozinho
    client.devices.revoke(phone.id)
    assert (await client.get("/api/config", headers=phone.h)).status == 401
    status, _ = await phone.register(client)
    assert status == 403


async def test_bad_signatures_and_replay(client):
    phone = Phone()
    await phone.register(client)
    client.devices.approve(phone.id, admin=False, by="pi")

    # assinatura de outra mensagem
    assert (await phone.login(client, tamper=True))[0] == 401
    # outra chave tentando entrar como este aparelho
    intruder = Phone("Intruso")
    assert (await intruder.login(client, device_id=phone.id))[0] == 401
    # id desconhecido
    assert (await phone.login(client, device_id="ffffffffffffffff"))[0] == 404

    # desafio de uso único
    nonce = (await (await client.post("/api/devices/challenge")).json())["nonce"]
    body = {"id": phone.id, "nonce": nonce, "signature": phone.sign(nonce, phone.id)}
    assert (await client.post("/api/devices/login", json=body)).status == 200
    assert (await client.post("/api/devices/login", json=body)).status == 401

    # desafio vencido
    nonce = (await (await client.post("/api/devices/challenge")).json())["nonce"]
    client.devices._challenges[nonce] = time.time() - 1
    body = {"id": phone.id, "nonce": nonce, "signature": phone.sign(nonce, phone.id)}
    assert (await client.post("/api/devices/login", json=body)).status == 401

    # cookie inventado
    assert (await client.get("/api/config", headers={"Cookie": f"{SESSION_COOKIE}=inventado"})).status == 401


async def test_admin_approves_from_app(client):
    admin = await pair_admin(client)
    phone = Phone("Tablet")
    await phone.register(client)

    # aparelho comum não aprova ninguém
    common = Phone("Comum")
    await common.register(client)
    client.devices.approve(common.id, admin=False, by="pi")
    await common.login(client)
    assert (await client.get("/api/devices", headers=common.h)).status == 403
    assert (await client.post("/api/devices/approve", json={"code": phone.code}, headers=common.h)).status == 403

    listing = await (await client.get("/api/devices", headers=admin.h)).json()
    assert [d["status"] for d in listing] == ["pending", "approved", "approved"]
    assert listing[0]["name"] == "Tablet" and "public_key" not in listing[0]

    # aprova pelo código que aparece no aparelho novo (sem traço e minúsculo também vale)
    assert (await client.post("/api/devices/approve", json={"code": "AAAA-AAAA"}, headers=admin.h)).status == 404
    assert (await client.post("/api/devices/approve", json={}, headers=admin.h)).status == 404
    resp = await client.post("/api/devices/approve", json={"code": phone.code.lower().replace("-", ""), "admin": False}, headers=admin.h)
    data = await resp.json()
    assert resp.status == 200 and data["status"] == "approved" and data["name"] == "Tablet" and data["admin"] is False
    # aparelho já aprovado não é aprovado de novo pelo código
    assert (await client.post("/api/devices/approve", json={"code": phone.code}, headers=admin.h)).status == 404
    assert (await phone.login(client))[0] == 200

    # não dá para revogar o único administrador
    assert (await client.post(f"/api/devices/{admin.id}/revoke", headers=admin.h)).status == 409
    # revogar e excluir um aparelho comum
    assert (await client.post(f"/api/devices/{phone.id}/delete", headers=admin.h)).status == 409
    assert (await client.post(f"/api/devices/{phone.id}/revoke", headers=admin.h)).status == 200
    assert (await client.get("/api/config", headers=phone.h)).status == 401
    assert (await client.post(f"/api/devices/{phone.id}/delete", headers=admin.h)).status == 200
    # depois de excluído, pode pedir acesso de novo
    assert (await phone.register(client))[1]["status"] == "pending"


async def test_pending_devices_are_capped(client):
    for i in range(dev.MAX_PENDING + 5):
        await Phone(f"p{i}").register(client)
    pending = client.store.devices(status="pending")
    assert len(pending) == dev.MAX_PENDING
    assert pending[0]["name"] == f"p{dev.MAX_PENDING + 4}"  # os mais novos ficam


async def test_push_only_for_approved_devices(client):
    admin = await pair_admin(client)
    sub = {"endpoint": "https://push.example/admin", "keys": {"p256dh": "x", "auth": "y"}}
    assert (await client.post("/api/push/subscribe", json={"subscription": sub}, headers=admin.h)).status == 200
    # inscrição antiga sem aparelho (de quando a aprovação estava desligada) não recebe mais
    client.store.save_subscription({"endpoint": "https://push.example/velha", "keys": {}}, None, None)
    assert [s["sub"]["endpoint"] for s in client.store.subscriptions(approved_only=True)] == ["https://push.example/admin"]
    assert [s["sub"]["endpoint"] for s in client.store.subscriptions(admins_only=True)] == ["https://push.example/admin"]
    phone = Phone()
    await phone.register(client)
    client.devices.approve(phone.id, admin=False, by="pi")
    await phone.login(client)
    sub2 = {"endpoint": "https://push.example/phone", "keys": {"p256dh": "x", "auth": "y"}}
    await client.post("/api/push/subscribe", json={"subscription": sub2}, headers=phone.h)
    client.devices.revoke(phone.id)
    assert "https://push.example/phone" not in [s["sub"]["endpoint"] for s in client.store.subscriptions()]


async def test_hls_segments_need_go2rtc_session_id(client):
    # sem id válido, segue bloqueado; com id, passa para o go2rtc (aqui, sem go2rtc: 502)
    assert (await client.get("/live/hls/segment.m4s")).status == 401
    assert (await client.get("/live/hls/segment.m4s?id=curto")).status == 401
    assert (await client.get("/live/hls/segment.m4s?id=Ab3dE5gH&n=1")).status == 502


async def test_logout_and_rename(client):
    admin = await pair_admin(client)
    assert (await client.post("/api/devices/rename", json={"name": "  Celular   novo "}, headers=admin.h)).status == 200
    assert client.store.device(admin.id)["name"] == "Celular novo"
    assert (await client.post("/api/devices/logout", headers=admin.h)).status == 200
    assert (await client.get("/api/config", headers=admin.h)).status == 401


async def test_cross_origin_login_blocked(client):
    resp = await client.post("/api/devices/challenge", headers={"Origin": "https://malicioso.example"})
    assert resp.status == 403


async def test_session_expires(client):
    admin = await pair_admin(client)
    token_hash = dev._hash_token(admin.cookie)
    client.store.extend_session(token_hash, time.time() - 1)
    assert (await client.get("/api/config", headers=admin.h)).status == 401


def test_cli_approves_by_code(tmp_path, make_cfg):
    cfg = make_cfg(1)
    cfg.data_dir.mkdir(parents=True)
    store = Store(cfg.data_dir / "zoio.db")
    devices = dev.Devices(store)
    phone = Phone()
    device, _ = devices.register(phone.jwk, "Celular", None, None)
    store.close()
    env_file = tmp_path / "zoio.env"
    env_file.write_text(f"DVR_HOST=x\nDVR_USER=u\nDVR_PASS=p\nZOIO_DATA_DIR={cfg.data_dir}\n")
    run = lambda *args: subprocess.run([sys.executable, "-m", "zoio", "--env", str(env_file), *args], capture_output=True, text=True, cwd=str(__import__("pathlib").Path(__file__).parent.parent))
    out = run("aparelhos")
    assert device["code"] in out.stdout and "aguardando" in out.stdout
    out = run("aparelhos", "aprovar", device["code"].lower().replace("-", ""))
    assert out.returncode == 0, out.stderr
    store = Store(cfg.data_dir / "zoio.db")
    approved = store.device(device["id"])
    assert approved["status"] == "approved" and approved["admin"] == 1 and approved["approved_by"] == "pi"
    out = run("aparelhos", "aprovar", "ZZZZ-ZZZZ")
    assert out.returncode != 0 and "nenhum aparelho" in out.stderr


async def test_admin_renames_and_hides_cameras(client, aiohttp_client):
    admin = await pair_admin(client)
    common = Phone("Comum")
    await common.register(client)
    client.devices.approve(common.id, admin=False, by="pi")
    await common.login(client)

    # só o administrador mexe nas câmeras
    assert (await client.post("/api/cameras/3", json={"hidden": True}, headers=common.h)).status == 403
    assert (await client.post("/api/cameras/9", json={"hidden": True}, headers=admin.h)).status == 404
    assert (await client.post("/api/cameras/3", json={"hidden": "sim"}, headers=admin.h)).status == 400
    assert (await client.post("/api/cameras/3", json={"name": "x" * 41}, headers=admin.h)).status == 400

    resp = await client.post("/api/cameras/3", json={"hidden": True}, headers=admin.h)
    assert resp.status == 200 and (await resp.json())["hidden"] is True
    resp = await client.post("/api/cameras/1", json={"name": "  Portão   da frente "}, headers=admin.h)
    assert (await resp.json())["name"] == "Portão da frente"

    # vale para todos os aparelhos; a oculta continua na lista, marcada
    cams = (await (await client.get("/api/config", headers=common.h)).json())["cameras"]
    assert [(c["number"], c["name"], c["hidden"]) for c in cams] == [
        (1, "Portão da frente", False), (2, "Câmera 2", False), (3, "Câmera 3", True), (4, "Câmera 4", False)]
    assert cams[0]["default_name"] == "Câmera 1"

    # mudar só a visibilidade mantém o nome; nome vazio volta ao do zoio.env
    await client.post("/api/cameras/1", json={"hidden": True}, headers=admin.h)
    cam1 = client.app["zoio_cfg"].camera(1)
    assert (cam1.name, cam1.hidden) == ("Portão da frente", True)
    await client.post("/api/cameras/1", json={"name": "", "hidden": False}, headers=admin.h)
    assert (cam1.name, cam1.hidden) == ("Câmera 1", False)

    # alertas de câmera oculta somem da lista e não entram mais
    client.store.add_event(3, "VideoMotion", "Start", None)
    client.store.add_event(2, "VideoMotion", "Start", None)
    events = await (await client.get("/api/events", headers=admin.h)).json()
    assert [e["camera"] for e in events] == [2]
    await client.app["zoio_alarms"].handle({"Channel": 2, "Event": "VideoLoss", "Status": "Start"})
    assert [e["camera"] for e in client.store.events(status=None)] == [2, 3]

    # sobrevive a reiniciar o servidor
    cfg = client.app["zoio_cfg"]
    await client.close()
    for c in cfg.cameras:
        c.name, c.hidden = c.default_name, False
    app = create_app(cfg)
    await aiohttp_client(app)
    assert [c.number for c in app["zoio_cfg"].cameras if c.hidden] == [3]
