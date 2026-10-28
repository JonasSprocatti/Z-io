import asyncio
import json

import aiohttp
import pytest
from fake_esphome import FakeEspHome
from fake_intercom import AUDIO, END, FakeIntercom, board_nonce
from test_devices import Phone, pair_admin
from test_porteiro import JPEG, KEY, go2rtc, until  # noqa: F401 (go2rtc é fixture)

from zoio import conversa
from zoio import devices as dev
from zoio import porteiro
from zoio.app import create_app

PCM = bytes(range(256)) * 6 + bytes(64)  # 1600 bytes: 50 ms de áudio


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(porteiro, "RETRY_MIN", 0.05)
    monkeypatch.setattr(conversa, "ANSWER_TIMEOUT", 0.6)


@pytest.fixture
async def intercom():
    i = await FakeIntercom().open()
    yield i
    i.close()


@pytest.fixture
async def board(intercom):
    b = FakeEspHome(KEY)
    b.on_execute = lambda name, args: intercom.start(args[0]) if name == "conversa_iniciar" else intercom.stop()
    await b.start()
    yield b
    await b.stop()


@pytest.fixture
async def client(aiohttp_client, make_cfg, board, intercom, go2rtc):  # noqa: F811
    cfg = make_cfg(1, alerts=False, gate_host="127.0.0.1", gate_port=board.port, gate_audio_port=intercom.port, gate_key=KEY, gate_camera=2)
    cfg.go2rtc_url = str(go2rtc.make_url("")).rstrip("/")
    app = create_app(cfg)
    c = await aiohttp_client(app)
    c.devices = app["zoio_devices"]
    c.store = app["zoio_store"]
    c.porteiro = app["zoio_porteiro"]
    await until(lambda: c.porteiro.online)
    return c


async def call(client, phone, action="conversar"):
    """Abre a conversa como o app faz. Devolve o WebSocket e a primeira resposta."""
    nonce = (await (await client.post("/api/portaria/challenge", headers=phone.h)).json())["nonce"]
    ws = await client.ws_connect("/api/portaria/conversa", headers=phone.h)
    signature = phone.sign(nonce, phone.id, lambda n, i: dev.portaria_message(n, i, "portao", action))
    await ws.send_json({"type": "iniciar", "nonce": nonce, "signature": signature})
    msg = await ws.receive(timeout=3)
    return ws, json.loads(msg.data)


async def test_talk_both_ways_and_log(client, board, intercom, go2rtc):  # noqa: F811
    admin = await pair_admin(client)
    info = (await (await client.get("/api/config", headers=admin.h)).json())["portaria"]
    assert info["talk"] is True and info["talking"] is None

    ws, first = await call(client, admin)
    assert first == {"type": "pronto"}
    (name, (key_hex,)), = board.executed
    assert name == "conversa_iniciar" and len(key_hex) == 32 and intercom.active
    info = (await (await client.get("/api/config", headers=admin.h)).json())["portaria"]
    assert info["talking"] == "Admin"

    # celular -> portão: em quadros de até 20 ms, na ordem
    await ws.send_bytes(PCM)
    await until(lambda: len(intercom.audio()) == len(PCM))
    assert intercom.audio() == PCM
    assert [len(p) for kind, p in intercom.received if kind == AUDIO] == [640, 640, 320]

    # portão -> celular
    intercom.send(AUDIO, PCM[:640])
    msg = await ws.receive(timeout=2)
    assert msg.type == aiohttp.WSMsgType.BINARY and msg.data == PCM[:640]

    await ws.send_json({"type": "fim"})
    msg = await ws.receive(timeout=3)
    assert json.loads(msg.data) == {"type": "fim", "motivo": "encerrada"}
    await ws.close()
    await until(lambda: [n for n, _ in board.executed] == ["conversa_iniciar", "conversa_parar"])
    assert intercom.received[-1][0] == END and not intercom.active
    assert intercom.rejected == 0
    event = client.store.events(limit=1)[0]
    assert (event["kind"], event["status"], event["camera"]) == ("Conversa", "Conversou", 2)
    assert event["detail"]["by"] == "Admin" and event["detail"]["photo"] and event["detail"]["error"] is None
    assert client.porteiro.photo_path(event["id"]).read_bytes() == JPEG
    assert client.porteiro.call is None


async def test_one_call_at_a_time(client, board):
    admin = await pair_admin(client)
    ws, first = await call(client, admin)
    assert first["type"] == "pronto"
    other = Phone("Celular da Ana")
    await other.register(client)
    client.devices.approve(other.id, admin=False, by="pi")
    client.devices.set_gate(other.id, True)
    await other.login(client)
    ws2, refused = await call(client, other)
    assert refused == {"type": "fim", "motivo": "Admin já está falando com o portão"}
    assert [n for n, _ in board.executed] == ["conversa_iniciar"]
    await ws.close()
    await until(lambda: client.porteiro.call is None)
    # a primeira acabou: agora dá
    ws3, first = await call(client, other)
    assert first["type"] == "pronto"
    await ws3.close()


async def test_many_phones_at_the_same_instant(client, board):
    """Três aparelhos apertam Conversar juntos: um fala, os outros ficam sabendo quem."""
    phones = []
    for name in ("Celular do Jonas", "Celular da Ana", "Tablet"):
        phone = Phone(name)
        await phone.register(client)
        client.devices.approve(phone.id, admin=False, by="pi", gate=True)
        await phone.login(client)
        phones.append(phone)
    results = await asyncio.gather(*(call(client, phone) for phone in phones))
    ready = [i for i, (_, first) in enumerate(results) if first["type"] == "pronto"]
    assert len(ready) == 1
    talker = phones[ready[0]].name
    for i, (_, first) in enumerate(results):
        if i != ready[0]:
            assert first == {"type": "fim", "motivo": f"{talker} já está falando com o portão"}
    assert [n for n, _ in board.executed] == ["conversa_iniciar"]  # a placa só recebeu uma chave
    info = (await (await client.get("/api/config", headers=phones[0].h)).json())["portaria"]
    assert info["talking"] == talker
    for ws, _ in results:
        await ws.close()
    await until(lambda: client.porteiro.call is None)
    events = [e for e in client.store.events(limit=10) if e["kind"] == "Conversa"]
    assert [(e["status"], e["detail"]["by"]) for e in events] == [("Conversou", talker)]


async def test_phone_that_vanishes_frees_the_gate(client, board, intercom, monkeypatch):
    """Celular sem sinal no meio da conversa: o Zóio percebe e libera para outro."""
    admin = await pair_admin(client)
    ws, first = await call(client, admin)
    assert first["type"] == "pronto"
    client.porteiro.call.finish("conexão com o aparelho caiu")  # o que o heartbeat do WebSocket faz
    await until(lambda: client.porteiro.call is None)
    assert not intercom.active  # a placa desligou o microfone
    ws2, again = await call(client, admin)
    assert again["type"] == "pronto"
    await ws2.close()


async def test_needs_gate_permission_and_signed_challenge(client, board):
    await pair_admin(client)
    visitor = Phone("Visita")
    await visitor.register(client)
    client.devices.approve(visitor.id, admin=False, by="pi")
    await visitor.login(client)
    with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
        await client.ws_connect("/api/portaria/conversa", headers=visitor.h)
    assert exc.value.status == 403

    admin = Phone("Outro admin")
    await admin.register(client)
    client.devices.approve(admin.id, admin=True, by="pi")
    await admin.login(client)
    # assinatura de outro comando não vale para a conversa
    ws, refused = await call(client, admin, action="abrir")
    assert refused["type"] == "fim" and refused["motivo"].startswith("confirmação do aparelho falhou")
    assert board.executed == []


async def test_websocket_from_another_site_is_refused(client):
    admin = await pair_admin(client)
    with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
        await client.ws_connect("/api/portaria/conversa", headers={**admin.h, "Origin": "https://outro-site.example"})
    assert exc.value.status == 403


async def test_board_that_does_not_answer(client, board, intercom):
    admin = await pair_admin(client)
    intercom.silent = True
    ws, refused = await call(client, admin)
    assert refused["type"] == "fim" and refused["motivo"].startswith("a placa não respondeu")
    await until(lambda: [n for n, _ in board.executed] == ["conversa_iniciar", "conversa_parar"])
    event = client.store.events(limit=1)[0]
    assert (event["kind"], event["status"]) == ("Conversa", "Falhou")
    assert event["detail"]["error"].startswith("a placa não respondeu")
    assert client.porteiro.call is None


async def test_board_ends_the_call(client, board, intercom):
    admin = await pair_admin(client)
    ws, first = await call(client, admin)
    assert first["type"] == "pronto"
    await until(lambda: intercom.peer is not None)
    intercom.stop()  # tempo máximo na placa, ou alguém mandou parar pela API
    msg = await ws.receive(timeout=3)
    assert json.loads(msg.data) == {"type": "fim", "motivo": "a placa encerrou a conversa"}
    await until(lambda: client.porteiro.call is None)


async def test_board_without_talk(aiohttp_client, make_cfg, intercom):
    b = await FakeEspHome(KEY, services={}).start()
    try:
        cfg = make_cfg(1, alerts=False, gate_host="127.0.0.1", gate_port=b.port, gate_audio_port=intercom.port, gate_key=KEY)
        app = create_app(cfg)
        c = await aiohttp_client(app)
        c.devices = app["zoio_devices"]
        await until(lambda: app["zoio_porteiro"].online)
        admin = await pair_admin(c)
        assert (await (await c.get("/api/config", headers=admin.h)).json())["portaria"]["talk"] is False
        ws, refused = await call(c, admin)
        assert refused == {"type": "fim", "motivo": "a placa do porteiro ainda não tem a conversa (grave o firmware novo)"}
    finally:
        await b.stop()


async def test_replayed_tampered_and_foreign_packets_are_ignored(intercom):
    got = []
    c = conversa.Call("127.0.0.1", intercom.port, on_audio=got.append)
    await c.open()
    intercom.start(c.key.hex())
    c.begin()
    assert await asyncio.wait_for(c.ready(), 2) is None

    intercom.send(AUDIO, b"\x01\x02" * 10)
    await until(lambda: got == [b"\x01\x02" * 10])

    # repetido, adulterado ou de outra chave: ignorado
    assert c.channel.open(board_packet(intercom, intercom.tx, b"\x05\x06")) is None
    tampered = bytearray(board_packet(intercom, intercom.tx + 5, b"\x05\x06"))
    tampered[-1] ^= 1
    assert c.channel.open(bytes(tampered)) is None
    assert conversa.Channel(bytes(16)).open(board_packet(intercom, intercom.tx + 6, b"\x05\x06")) is None
    assert c.channel.open(board_packet(intercom, intercom.tx + 7, b"\x05\x06")) == (AUDIO, b"\x05\x06")
    await c.close()


def board_packet(intercom, counter, payload):
    header = b"Z" + bytes([AUDIO]) + counter.to_bytes(8, "big")
    return header + intercom.aead.encrypt(board_nonce(2, counter), payload, header)


async def test_app_cannot_flood_the_board(intercom):
    c = conversa.Call("127.0.0.1", intercom.port)
    await c.open()
    intercom.start(c.key.hex())
    c.begin()
    await asyncio.wait_for(c.ready(), 2)
    assert c.send_audio(bytes(conversa.SEND_BURST))
    assert not c.send_audio(bytes(6400))  # sem fôlego: descarta
    await asyncio.sleep(0.15)  # 0,15 s a 2x o tempo real libera 9600 bytes
    assert c.send_audio(bytes(6400))
    assert not c.send_audio(b"\x01")  # meio amostra não vai
    await c.close()
