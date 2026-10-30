import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from aiohttp import web
from fake_esphome import FakeEspHome
from test_devices import Phone, pair_admin

from zoio import devices as dev
from zoio import esphome, porteiro
from zoio.alarms import EventBus
from zoio.app import create_app
from zoio.store import Store

KEY = esphome.new_key()
JPEG = b"\xff\xd8\xff\xe0foto-do-portao\xff\xd9"
RADIO_LOG = [
    "\x1b[0;32m[I][remote.rc_switch:256]: Received RCSwitch Raw: protocol=6 data='0110100101101001'\x1b[0m",
    "[I][remote.rc_switch:256]: Received RCSwitch Raw: protocol=6 data='0110100101101001'",  # o controle repete o código
    "[I][wifi:512]: outra coisa qualquer",
    "[I][remote.rc_switch:256]: Received RCSwitch Raw: protocol=1 data='101010101010101010101110'",
]


async def until(check, timeout=3.0):
    end = time.monotonic() + timeout
    while not check():
        if time.monotonic() > end:
            raise AssertionError("condição não aconteceu a tempo")
        await asyncio.sleep(0.02)


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(porteiro, "RETRY_MIN", 0.05)


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
    cfg = make_cfg(1, alerts=False, gate_host="127.0.0.1", gate_port=board.port, gate_key=KEY, gate_camera=2, garage_cameras=[3, None], garage_travel=30)
    cfg.go2rtc_url = str(go2rtc.make_url("")).rstrip("/")
    app = create_app(cfg)
    c = await aiohttp_client(app)
    c.store = app["zoio_store"]
    c.devices = app["zoio_devices"]
    c.porteiro = app["zoio_porteiro"]
    c.bus = app["zoio_bus"]
    await until(lambda: c.porteiro.online)
    return c


async def act(client, phone, door, action, nonce=None, sign_as=None):
    if nonce is None:
        resp = await client.post("/api/portaria/challenge", headers=phone.h)
        if resp.status != 200:
            return resp.status, await resp.text()
        nonce = (await resp.json())["nonce"]
    sign_door, sign_action = sign_as or (door, action)
    signature = phone.sign(nonce, phone.id, lambda n, i: dev.portaria_message(n, i, sign_door, sign_action))
    resp = await client.post("/api/portaria/act", headers=phone.h, json={"door": door, "action": action, "nonce": nonce, "signature": signature})
    body = await resp.json() if resp.content_type == "application/json" else await resp.text()
    return resp.status, body


async def portaria(client, phone) -> dict:
    return (await (await client.get("/api/config", headers=phone.h)).json())["portaria"]


async def test_doors_come_from_the_board(client):
    admin = await pair_admin(client)
    info = await portaria(client, admin)
    assert (info["enabled"], info["online"], info["allowed"], info["doorbell"], info["camera"]) == (True, True, True, True, 2)
    doors = {d["id"]: d for d in info["doors"]}
    assert list(doors) == ["portao", "garagem_1", "garagem_2"]
    assert doors["portao"]["actions"] == ["abrir"] and doors["portao"]["camera"] == 2
    g1, g2 = doors["garagem_1"], doors["garagem_2"]
    assert (g1["name"], g1["camera"], g1["sensor"], g1["open"], g1["actions"]) == ("Garagem 1", 3, True, False, ["abrir"])
    assert (g2["sensor"], g2["open"], g2["actions"]) == (False, None, ["acionar"])  # sem sensor: só o comando genérico


async def test_admin_opens_gate_with_photo_and_log(client, board, go2rtc):
    admin = await pair_admin(client)
    status, event = await act(client, admin, "portao", "abrir")
    assert status == 200, event
    assert event["status"] == "Aberto" and event["kind"] == "Portao" and event["camera"] == 2
    assert event["detail"] == {"door": "portao", "name": "Portão", "action": "abrir", "by": "Admin", "device_id": admin.id, "error": None, "photo": True}
    assert board.pressed == [0x1234ABCD]
    assert go2rtc.frames == ["cam2"]  # fluxo principal, para reconhecer quem está no portão

    photo = await client.get(f"/api/portaria/photos/{event['id']}.jpg", headers=admin.h)
    assert photo.status == 200 and await photo.read() == JPEG
    assert (await client.get(f"/api/portaria/photos/{event['id']}.jpg")).status == 401

    events = await (await client.get("/api/events", headers=admin.h)).json()
    assert events[0]["id"] == event["id"] and events[0]["detail"]["by"] == "Admin"


async def test_permission_and_signature_bound_to_door_and_action(client, board):
    admin = await pair_admin(client)
    phone = Phone("Celular da visita")
    await phone.register(client)
    client.devices.approve(phone.id, admin=False, by="pi")
    await phone.login(client)

    assert (await portaria(client, phone))["allowed"] is False
    assert (await act(client, phone, "portao", "abrir"))[0] == 403

    resp = await client.post(f"/api/devices/{phone.id}/gate", headers=admin.h, json={"allow": True})
    assert resp.status == 200 and (await resp.json())["gate"] is True
    assert (await client.post(f"/api/devices/{admin.id}/gate", headers=phone.h, json={"allow": False})).status == 403

    # a assinatura vale só para a porta e o comando assinados, e o desafio é de uso único
    nonce = (await (await client.post("/api/portaria/challenge", headers=phone.h)).json())["nonce"]
    assert (await act(client, phone, "garagem_1", "abrir", nonce=nonce, sign_as=("portao", "abrir")))[0] == 401
    assert (await act(client, phone, "portao", "abrir", nonce=nonce))[0] == 401
    nonce = (await (await client.post("/api/portaria/challenge", headers=phone.h)).json())["nonce"]
    login_sig = phone.sign(nonce, phone.id)
    resp = await client.post("/api/portaria/act", headers=phone.h, json={"door": "portao", "action": "abrir", "nonce": nonce, "signature": login_sig})
    assert resp.status == 401
    assert board.pressed == []

    assert (await act(client, phone, "portao", "abrir"))[0] == 200
    status, text = await act(client, phone, "portao", "abrir")
    assert status == 409 and "aguarde" in text
    assert len(board.pressed) == 1

    await client.post(f"/api/devices/{phone.id}/gate", headers=admin.h, json={"allow": False})
    assert (await act(client, phone, "portao", "abrir"))[0] == 403


async def test_garage_open_stop_and_close_rules(client, board, go2rtc):
    admin = await pair_admin(client)
    status, text = await act(client, admin, "garagem_1", "fechar")
    assert status == 409 and "já está fechada" in text

    status, event = await act(client, admin, "garagem_1", "abrir")
    assert status == 200 and (event["kind"], event["status"], event["camera"]) == ("Garagem", "Abrindo", 3)
    assert board.pressed == [0x2001] and go2rtc.frames == ["cam3"]
    door = client.porteiro.doors["garagem_1"]
    assert door.actions() == ["parar"]  # em movimento: só dá para parar

    # o sensor abre logo depois do comando: não vira um segundo registro
    await board.set_state("garagem_1_aberta", True)
    assert door.open is True and door.actions() == ["parar"]
    status, text = await act(client, admin, "garagem_1", "fechar")
    assert status == 409 and "em movimento" in text

    status, event = await act(client, admin, "garagem_1", "parar")  # Parar não espera o intervalo mínimo
    assert status == 200 and event["status"] == "Parado"
    assert door.actions() == ["acionar"]  # depois de parar, o próximo toque pode abrir ou fechar
    assert board.pressed == [0x2001, 0x2001]

    # passou o tempo de manobra; alguém fecha pelo controle remoto
    door.unsure, door.expect_until = False, 0
    assert door.actions() == ["fechar"]
    await board.set_state("garagem_1_aberta", False)
    await until(lambda: client.store.events(status=None)[0]["status"] == "Fechou")
    event = client.store.events(status=None)[0]
    assert event["detail"] == {"door": "garagem_1", "name": "Garagem 1", "photo": True}
    kinds = [(e["kind"], e["status"]) for e in client.store.events(status=None)]
    assert kinds == [("Garagem", "Fechou"), ("Garagem", "Parado"), ("Garagem", "Abrindo")]

    # garagem sem sensor só aceita o comando genérico
    assert (await act(client, admin, "garagem_2", "abrir"))[0] == 409
    status, event = await act(client, admin, "garagem_2", "acionar")
    assert status == 200 and event["status"] == "Acionado"
    assert (await act(client, admin, "garagem_9", "abrir"))[0] == 404


async def test_doorbell_notifies_and_keeps_photo(client, board, go2rtc):
    admin = await pair_admin(client)
    rings = []
    client.porteiro.notifier.doorbell = lambda house, name: _record(rings)
    queue = client.bus.subscribe()
    await board.set_state("campainha", True)
    await board.set_state("campainha", False)
    await until(lambda: any(e["kind"] == "Campainha" for e in client.store.events(status=None)))
    event = client.store.events()[0]  # aparece no histórico de alertas
    assert (event["kind"], event["status"], event["camera"]) == ("Campainha", "Tocou", 2)
    assert event["detail"] == {"door": "portao", "photo": True, "house": 1, "house_name": None}  # uma campainha só: sem nome de casa
    assert (await portaria(client, admin))["bells"] == [{"house": 1, "name": None}]
    assert rings == [1] and go2rtc.frames == ["cam2"]
    published = [queue.get_nowait() for _ in range(queue.qsize())]
    assert any(p["type"] == "campainha" and p["id"] == event["id"] for p in published)

    # toques seguidos viram um aviso só
    await board.set_state("campainha", True)
    await board.set_state("campainha", False)
    await asyncio.sleep(0.1)
    assert rings == [1] and len([e for e in client.store.events(status=None) if e["kind"] == "Campainha"]) == 1
    photo = await client.get(f"/api/portaria/photos/{event['id']}.jpg", headers=admin.h)
    assert photo.status == 200


async def _record(items, item=1):
    items.append(item)
    return 1


TWO_BELLS = {**FakeEspHome(KEY).sensors, "campainha_2": (0x3002, "Campainha 2")}


async def test_one_doorbell_per_house(aiohttp_client, make_cfg, go2rtc):
    """Duas casas: cada toque diz a casa, e cada aparelho só recebe o aviso da sua."""
    board = await FakeEspHome(KEY, sensors=TWO_BELLS).start()
    try:
        cfg = make_cfg(1, alerts=False, gate_host="127.0.0.1", gate_port=board.port, gate_key=KEY, gate_camera=2, bell_names=["Casa do Jonas"])
        cfg.go2rtc_url = str(go2rtc.make_url("")).rstrip("/")
        app = create_app(cfg)
        client = await aiohttp_client(app)
        client.store, client.devices, client.porteiro = app["zoio_store"], app["zoio_devices"], app["zoio_porteiro"]
        await until(lambda: client.porteiro.online)
        admin = await pair_admin(client)
        assert (await portaria(client, admin))["bells"] == [{"house": 1, "name": "Casa do Jonas"}, {"house": 2, "name": "Casa 2"}]

        # cada aparelho com a sua casa; o admin fica com todas
        phones = {}
        for house in (1, 2):
            phone = Phone(f"Celular da casa {house}")
            await phone.register(client)
            client.devices.approve(phone.id, admin=False, by="pi", gate=True)
            await phone.login(client)
            resp = await client.post(f"/api/devices/{phone.id}/house", headers=admin.h, json={"house": house})
            assert resp.status == 200 and (await resp.json())["house"] == house
            phones[house] = phone
        for bad in ("2", True, 0, 100):
            assert (await client.post(f"/api/devices/{phones[1].id}/house", headers=admin.h, json={"house": bad})).status == 400
        assert (await client.post(f"/api/devices/{phones[1].id}/house", headers=phones[2].h, json={"house": 2})).status == 403
        for name, phone in (("admin", admin), ("casa1", phones[1]), ("casa2", phones[2])):
            sub = {"endpoint": f"https://push.example/{name}", "keys": {"p256dh": "x", "auth": "y"}}
            assert (await client.post("/api/push/subscribe", json={"subscription": sub}, headers=phone.h)).status == 200
        endpoints = lambda house: sorted(s["sub"]["endpoint"].rsplit("/", 1)[1] for s in client.store.subscriptions(gate_only=True, house=house))
        assert endpoints(1) == ["admin", "casa1"] and endpoints(2) == ["admin", "casa2"]
        assert endpoints(None) == ["admin", "casa1", "casa2"]

        rings = []
        client.porteiro.notifier.doorbell = lambda house, name: _record(rings, (house, name))
        await board.set_state("campainha_2", True)
        await board.set_state("campainha_2", False)
        await until(lambda: rings == [(2, "Casa 2")])
        # a outra casa toca logo depois: não é engolida pelo intervalo da primeira
        await board.set_state("campainha", True)
        await board.set_state("campainha", False)
        await until(lambda: rings == [(2, "Casa 2"), (1, "Casa do Jonas")])
        await until(lambda: len([e for e in client.store.events(status=None) if e["kind"] == "Campainha"]) == 2)
        details = [e["detail"] for e in client.store.events(status=None) if e["kind"] == "Campainha"]
        assert [(d["house"], d["house_name"]) for d in details] == [(1, "Casa do Jonas"), (2, "Casa 2")]
        # de volta a todas
        resp = await client.post(f"/api/devices/{phones[1].id}/house", headers=admin.h, json={"house": None})
        assert (await resp.json())["house"] is None
    finally:
        await board.stop()


async def test_exit_button_is_logged(aiohttp_client, make_cfg, go2rtc):
    """Botão de saída: a placa abre sozinha; o Zóio só registra, sem apertar nada na placa."""
    board = await FakeEspHome(KEY, sensors={**FakeEspHome(KEY).sensors, "botao_saida": (0x3010, "Botao saida")}).start()
    try:
        cfg = make_cfg(1, alerts=False, gate_host="127.0.0.1", gate_port=board.port, gate_key=KEY, gate_camera=2)
        cfg.go2rtc_url = str(go2rtc.make_url("")).rstrip("/")
        app = create_app(cfg)
        await aiohttp_client(app)
        store, gate = app["zoio_store"], app["zoio_porteiro"]
        await until(lambda: gate.online)
        assert gate.info["exit_button"] and "botao_saida" not in [d.sensor for d in gate.doors.values()]
        queue = app["zoio_bus"].subscribe()
        for _ in range(2):  # dois apertos seguidos: um registro só
            await board.set_state("botao_saida", True)
            await board.set_state("botao_saida", False)
        await until(lambda: any(e["kind"] == "Portao" for e in store.events(status=None)))
        await asyncio.sleep(0.1)
        events = [e for e in store.events(status=None) if e["kind"] == "Portao"]
        assert len(events) == 1 and events[0]["status"] == "Aberto" and events[0]["camera"] == 2
        assert events[0]["detail"]["by"] == "botão de dentro" and events[0]["detail"]["photo"]
        assert board.pressed == []
        assert any(p["type"] == "porta" and p["id"] == events[0]["id"] for p in [queue.get_nowait() for _ in range(queue.qsize())])
    finally:
        await board.stop()


async def test_push_text_names_the_house(client, monkeypatch):
    sent = []

    async def fake_send(sub, payload):
        sent.append(json.loads(payload))
        return True

    notifier = client.porteiro.notifier
    monkeypatch.setattr(notifier, "send", fake_send)
    admin = await pair_admin(client)
    sub = {"endpoint": "https://push.example/admin", "keys": {"p256dh": "x", "auth": "y"}}
    await client.post("/api/push/subscribe", json={"subscription": sub}, headers=admin.h)
    assert await notifier.doorbell() == 1 and await notifier.doorbell(2, "Casa 2") == 1
    assert [(p["title"], p["tag"]) for p in sent] == [("Campainha", "campainha-1"), ("Campainha: Casa 2", "campainha-2")]


async def test_board_offline_then_back(client, board):
    admin = await pair_admin(client)
    await board.stop()
    await until(lambda: not client.porteiro.online)
    info = await portaria(client, admin)
    assert info["online"] is False and [d["id"] for d in info["doors"]] == ["portao", "garagem_1", "garagem_2"]
    status, event = await act(client, admin, "portao", "abrir")
    assert status == 502 and event["status"] == "Falhou" and "desconectada" in event["detail"]["error"]
    status_info = await (await client.get("/api/status", headers=admin.h)).json()
    assert status_info["portaria"] is True and status_info["portaria_online"] is False and status_info["portaria_error"]

    await board.start()  # mesma porta: o Zóio reconecta sozinho
    await until(lambda: client.porteiro.online)
    client.porteiro._last.clear()
    assert (await act(client, admin, "portao", "abrir"))[0] == 200


async def test_check_board(client, board):
    admin = await pair_admin(client)
    resp = await client.post("/api/portaria/check", headers=admin.h, json={})
    assert resp.status == 200
    info = await resp.json()
    assert info["name"] == "zoio-porteiro" and info["online"] and info["doorbell"] and info["wifi_dbm"] == -61
    assert info["buttons"] == ["abrir_portao", "garagem_1", "garagem_2"] and info["sensors"] == ["campainha", "garagem_1_aberta"]
    assert board.pressed == []


async def test_doors_survive_restart_without_board(aiohttp_client, make_cfg, board):
    cfg = make_cfg(1, alerts=False, gate_host="127.0.0.1", gate_port=board.port, gate_key=KEY)
    app = create_app(cfg)
    client = await aiohttp_client(app)
    await until(lambda: app["zoio_porteiro"].online)
    await client.close()
    await board.stop()

    app2 = create_app(cfg)  # servidor reiniciado com a placa fora do ar
    client2 = await aiohttp_client(app2)
    client2.devices = app2["zoio_devices"]
    admin = await pair_admin(client2)
    info = await portaria(client2, admin)
    assert info["online"] is False and [d["id"] for d in info["doors"]] == ["portao", "garagem_1", "garagem_2"]


async def test_portaria_off_without_config(aiohttp_client, make_cfg):
    app = create_app(make_cfg(1, alerts=False))
    client = await aiohttp_client(app)
    client.devices = app["zoio_devices"]
    admin = await pair_admin(client)
    info = await portaria(client, admin)
    assert info["enabled"] is False and info["doors"] == []
    assert (await client.post("/api/portaria/challenge", headers=admin.h)).status == 404


async def test_radio_lists_each_code_once(make_cfg, board):
    board.logs = RADIO_LOG
    cfg = make_cfg(1, gate_host="127.0.0.1", gate_port=board.port, gate_key=KEY)
    cfg.data_dir.mkdir(parents=True)
    store = Store(cfg.data_dir / "zoio.db")
    codes = []
    await porteiro.Porteiro(cfg, store, EventBus()).radio(0.5, lambda p, c: codes.append((p, c)))
    store.close()
    assert codes == [(6, "0110100101101001"), (1, "101010101010101010101110")]
    assert board.log_level == esphome.LOG_LEVEL_INFO
    assert board.pressed == []  # escutar não aperta nada


def test_config(make_cfg, tmp_path):
    assert make_cfg(1, gate_host="x", gate_key=KEY).gate_enabled
    assert not make_cfg(1, gate_host="x", gate_key=KEY, device_approval=False).gate_enabled
    env = tmp_path / "zoio.env"
    env.write_text(
        "DVR_HOST=x\nDVR_USER=u\nDVR_PASS=p\nZOIO_CHANNELS=4\nZOIO_GATE_HOST=192.168.15.20\nZOIO_GATE_KEY=curta\nZOIO_GATE_CAMERA=9\n"
        "ZOIO_GARAGE_CAMERAS=3, 9\nZOIO_GARAGE_NAMES=Garagem da frente\nZOIO_GARAGE_TRAVEL=25\n"
        "ZOIO_BELL_NAMES=Casa da frente, \n"
    )
    from zoio import config

    cfg = config.load(str(env))
    assert not cfg.gate_enabled and "ZOIO_GATE_KEY" in cfg.gate_error
    assert cfg.gate_camera is None  # câmera que não existe é ignorada
    assert (cfg.garage_camera(1), cfg.garage_camera(2), cfg.garage_camera(3)) == (3, None, None)
    assert (cfg.garage_name(1), cfg.garage_name(2), cfg.garage_travel) == ("Garagem da frente", "Garagem 2", 25)
    assert (cfg.bell_name(1), cfg.bell_name(2), cfg.bell_name(3)) == ("Casa da frente", "Casa 2", "Casa 3")


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
    assert store.device("a")["house"] is None  # sem casa: recebe todas as campainhas
    assert store.events()[0]["detail"] is None
    store.close()


async def test_cli_commands(tmp_path, make_cfg, board):
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
    assert code == 0 and "zoio-porteiro" in out and "garagem_1: Garagem 1 (com sensor)" in out and "campainha: ok" in out, out + err
    assert board.pressed == []
    code, out, err = await run("portao", "abrir")
    assert code == 0 and "Portão: aberto" in out, err
    code, out, err = await run("portao", "acionar", "garagem_2")
    assert code == 0 and "Garagem 2: acionado" in out, err
    code, out, err = await run("portao", "acionar", "garagem_7")
    assert code == 2 and "Porta desconhecida" in err
    assert board.pressed == [0x1234ABCD, 0x2002]

    code, out, err = await run("portao", "radio", "--segundos", "0.5")
    assert code == 1 and "Nenhum código chegou" in err
    board.logs = RADIO_LOG
    code, out, err = await run("portao", "radio", "--segundos", "0.5")
    assert code == 0, err
    assert "1. protocolo 6, código 0110100101101001 (16 bits)" in out and "2. protocolo 1" in out and "3." not in out

    code, out, err = await run("aparelhos", "portao", device["code"])
    assert code == 0 and "pode usar a portaria" in out, err
    code, out, err = await run("aparelhos", "portao", device["code"], "--nao")
    assert code == 0 and "não pode mais" in out, err

    store = Store(cfg.data_dir / "zoio.db")
    assert store.device(device["id"])["gate"] == 0
    assert store.events()[0]["detail"]["by"] == "Pi"
    store.close()

    code, out, _ = await run("portao", "chave")
    assert len(esphome.decode_key(out.strip())) == 32
