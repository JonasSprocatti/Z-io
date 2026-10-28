import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from aiohttp import web
from fake_esphome import FakeEspHome
from test_devices import Phone, pair_admin

from zoio import devices as dev
from zoio import esphome
from zoio.app import create_app
from zoio.store import Store

KEY = esphome.new_key()
JPEG = b"\xff\xd8\xff\xe0foto-do-portao\xff\xd9"


@pytest.fixture
async def board():
    b = await FakeEspHome(KEY).start()
    yield b
    await b.stop()


@pytest.fixture
async def go2rtc(aiohttp_server):
    frames = []

    async def frame(request):
        frames.append(request.query.get("src"))
        return web.Response(body=JPEG, content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/api/frame.jpeg", frame)
    server = await aiohttp_server(app)
    server.frames = frames
    return server


@pytest.fixture
async def client(aiohttp_client, make_cfg, board, go2rtc):
    cfg = make_cfg(1, alerts=False, gate_host="127.0.0.1", gate_port=board.port, gate_key=KEY, gate_camera=2)
    cfg.go2rtc_url = str(go2rtc.make_url("")).rstrip("/")
    app = create_app(cfg)
    c = await aiohttp_client(app)
    c.store = app["zoio_store"]
    c.devices = app["zoio_devices"]
    c.gate = app["zoio_gate"]
    return c


async def open_gate(client, phone, message=dev.gate_message, nonce=None):
    if nonce is None:
        resp = await client.post("/api/gate/challenge", headers=phone.h)
        if resp.status != 200:
            return resp.status, await resp.text()
        nonce = (await resp.json())["nonce"]
    resp = await client.post("/api/gate/open", headers=phone.h, json={"nonce": nonce, "signature": phone.sign(nonce, phone.id, message)})
    body = await resp.json() if resp.content_type == "application/json" else await resp.text()
    return resp.status, body


async def test_admin_opens_gate_with_photo_and_log(client, board, go2rtc):
    admin = await pair_admin(client)
    cfg = await (await client.get("/api/config", headers=admin.h)).json()
    assert cfg["gate"] == {"enabled": True, "camera": 2, "allowed": True}

    status, event = await open_gate(client, admin)
    assert status == 200, event
    assert event["status"] == "Aberto" and event["kind"] == "Portao" and event["camera"] == 2
    assert event["detail"] == {"by": "Admin", "device_id": admin.id, "error": None, "photo": True}
    assert board.pressed == [0x1234ABCD]
    assert go2rtc.frames == ["cam2"]  # fluxo principal, para reconhecer quem está no portão

    photo = await client.get(f"/api/gate/photos/{event['id']}.jpg", headers=admin.h)
    assert photo.status == 200 and await photo.read() == JPEG
    assert (await client.get(f"/api/gate/photos/{event['id']}.jpg")).status == 401

    events = await (await client.get("/api/events", headers=admin.h)).json()
    assert events[0]["id"] == event["id"] and events[0]["detail"]["by"] == "Admin"


async def test_permission_fresh_signature_and_rate_limit(client, board):
    admin = await pair_admin(client)
    phone = Phone("Celular da visita")
    await phone.register(client)
    client.devices.approve(phone.id, admin=False, by="pi")
    await phone.login(client)

    cfg = await (await client.get("/api/config", headers=phone.h)).json()
    assert cfg["gate"]["allowed"] is False
    assert (await open_gate(client, phone))[0] == 403

    # um administrador libera o portão para o aparelho
    resp = await client.post(f"/api/devices/{phone.id}/gate", headers=admin.h, json={"allow": True})
    assert resp.status == 200 and (await resp.json())["gate"] is True
    # comum não pode mudar permissões
    assert (await client.post(f"/api/devices/{admin.id}/gate", headers=phone.h, json={"allow": False})).status == 403

    # a assinatura de entrada não serve para abrir o portão, e o desafio é de uso único
    nonce = (await (await client.post("/api/gate/challenge", headers=phone.h)).json())["nonce"]
    assert (await open_gate(client, phone, message=dev.login_message, nonce=nonce))[0] == 401
    assert (await open_gate(client, phone, nonce=nonce))[0] == 401
    assert board.pressed == []

    assert (await open_gate(client, phone))[0] == 200
    status, text = await open_gate(client, phone)
    assert status == 429 and "aguarde" in text
    assert len(board.pressed) == 1

    await client.post(f"/api/devices/{phone.id}/gate", headers=admin.h, json={"allow": False})
    client.gate._last = -100
    assert (await open_gate(client, phone))[0] == 403


async def test_failure_is_reported_and_logged(client, board):
    admin = await pair_admin(client)
    await board.stop()
    status, event = await open_gate(client, admin)
    assert status == 502
    assert event["status"] == "Falhou" and "não consegui conectar" in event["detail"]["error"]
    events = await (await client.get("/api/events", headers=admin.h)).json()
    assert events[0]["status"] == "Falhou"


async def test_check_board(client, board):
    admin = await pair_admin(client)
    resp = await client.post("/api/gate/check", headers=admin.h, json={})
    assert resp.status == 200
    info = await resp.json()
    assert info["name"] == "zoio-portao" and info["buttons"] == ["abrir_portao"] and info["button_ok"]
    assert board.pressed == []


async def test_gate_off_without_config(aiohttp_client, make_cfg):
    app = create_app(make_cfg(1, alerts=False))
    client = await aiohttp_client(app)
    client.devices = app["zoio_devices"]
    admin = await pair_admin(client)
    cfg = await (await client.get("/api/config", headers=admin.h)).json()
    assert cfg["gate"]["enabled"] is False
    assert (await client.post("/api/gate/challenge", headers=admin.h)).status == 404


def test_gate_needs_device_approval_and_valid_key(make_cfg, tmp_path):
    assert make_cfg(1, gate_host="x", gate_key=KEY).gate_enabled
    assert not make_cfg(1, gate_host="x", gate_key=KEY, device_approval=False).gate_enabled
    env = tmp_path / "zoio.env"
    env.write_text("DVR_HOST=x\nDVR_USER=u\nDVR_PASS=p\nZOIO_GATE_HOST=192.168.15.20\nZOIO_GATE_KEY=curta\nZOIO_GATE_CAMERA=9\n")
    from zoio import config

    cfg = config.load(str(env))
    assert not cfg.gate_enabled and "ZOIO_GATE_KEY" in cfg.gate_error
    assert cfg.gate_camera is None  # câmera que não existe é ignorada


def test_migration_gives_gate_to_existing_admins(tmp_path):
    db = sqlite3.connect(tmp_path / "zoio.db")
    db.executescript(
        """
        CREATE TABLE events (id INTEGER PRIMARY KEY, camera INTEGER NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, dvr_time TEXT, received_at REAL NOT NULL);
        CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT NOT NULL, public_key TEXT NOT NULL UNIQUE, code TEXT NOT NULL, status TEXT NOT NULL,
            admin INTEGER NOT NULL DEFAULT 0, user TEXT, user_agent TEXT, created_at REAL NOT NULL, approved_at REAL, approved_by TEXT, last_seen REAL);
        INSERT INTO devices VALUES ('a', 'PC', 'k1', 'AAAA-AAAA', 'approved', 1, NULL, NULL, 0, 0, 'pi', NULL);
        INSERT INTO devices VALUES ('b', 'Visita', 'k2', 'BBBB-BBBB', 'approved', 0, NULL, NULL, 0, 0, 'a', NULL);
        INSERT INTO events (camera, kind, status, received_at) VALUES (1, 'VideoMotion', 'Start', 1);
        """
    )
    db.commit()
    db.close()
    store = Store(tmp_path / "zoio.db")
    assert store.device("a")["gate"] == 1 and store.device("b")["gate"] == 0
    assert store.events()[0]["detail"] is None
    store.close()


async def test_cli_gate_commands(tmp_path, make_cfg, board):
    cfg = make_cfg(1)
    cfg.data_dir.mkdir(parents=True)
    store = Store(cfg.data_dir / "zoio.db")
    phone = Phone()
    device, _ = dev.Devices(store).register(phone.jwk, "Celular", None, None)
    dev.Devices(store).approve(device["id"], admin=False, by="pi")
    store.close()
    env = tmp_path / "zoio.env"
    env.write_text(f"DVR_HOST=x\nDVR_USER=u\nDVR_PASS=p\nZOIO_DATA_DIR={cfg.data_dir}\nZOIO_GATE_HOST=127.0.0.1\nZOIO_GATE_PORT={board.port}\nZOIO_GATE_KEY={KEY}\n")

    async def run(*args):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "zoio", "--env", str(env), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(Path(__file__).parent.parent),
        )
        out, err = await proc.communicate()
        return proc.returncode, out.decode(), err.decode()

    code, out, err = await run("portao", "testar")
    assert code == 0 and "zoio-portao" in out and "abrir_portao" in out, err
    assert board.pressed == []
    code, out, err = await run("portao", "abrir")
    assert code == 0 and "Portão aberto" in out, err
    assert board.pressed == [0x1234ABCD]

    code, out, err = await run("aparelhos", "portao", device["code"])
    assert code == 0 and "pode abrir" in out, err
    code, out, err = await run("aparelhos", "portao", device["code"], "--nao")
    assert code == 0 and "não pode mais" in out, err

    store = Store(cfg.data_dir / "zoio.db")
    assert store.device(device["id"])["gate"] == 0
    assert store.events()[0]["detail"]["by"] == "Pi"
    store.close()

    code, out, _ = await run("portao", "chave")
    assert len(esphome.decode_key(out.strip())) == 32
