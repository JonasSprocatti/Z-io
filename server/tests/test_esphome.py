import asyncio

import pytest
from fake_esphome import FakeEspHome

from zoio import esphome as eh

KEY = eh.new_key()


def test_protobuf_roundtrip():
    data = eh.pb_string(1, "portão") + eh.pb_uint(2, 300) + eh.pb_fixed32(3, 0xDEADBEEF)
    assert eh.pb_parse(data) == {1: "portão".encode(), 2: 300, 3: 0xDEADBEEF}
    with pytest.raises(eh.EspHomeError):
        eh.pb_parse(b"\x08")  # varint cortado


def test_key_validation():
    assert len(eh.decode_key(KEY)) == 32
    for bad in ("", "abc", "não é base64", eh.new_key()[:20]):
        with pytest.raises(eh.EspHomeError):
            eh.decode_key(bad)


def test_object_id_follows_esphome():
    # mesma regra do ESPHome 2026 (helpers.py: sanitize(snake_case(nome)))
    assert eh.object_id("Abrir portao") == "abrir_portao"
    assert eh.object_id("Garagem 1 aberta") == "garagem_1_aberta"
    assert eh.object_id("Sinal Wi-Fi") == "sinal_wi-fi"
    assert eh.object_id("Portão") == "port_o"


@pytest.mark.parametrize("send_object_id", [False, True], ids=["esphome-2026", "firmware-antigo"])
async def test_entities_by_object_id(send_object_id):
    """A placa nova manda o object_id vazio; a antiga mandava pronto. Os dois dão o mesmo resultado."""
    board = await FakeEspHome(KEY, send_object_id=send_object_id).start()
    try:
        async with eh.EspHome("127.0.0.1", board.port, KEY) as dev:
            ents = await dev.entities()
    finally:
        await board.stop()
    assert set(ents.buttons) == {"abrir_portao", "garagem_1", "garagem_2"}
    assert set(ents.binary_sensors) == {"campainha", "garagem_1_aberta"}
    assert ents.sensors == {"sinal_wi-fi": (7, "Sinal Wi-Fi")}


async def test_press_button_over_encrypted_api():
    board = await FakeEspHome(KEY).start()
    try:
        async with eh.EspHome("127.0.0.1", board.port, KEY, expected_name="zoio-porteiro") as dev:
            assert (dev.name, dev.mac, dev.server_info) == ("zoio-porteiro", "AA:BB:CC:DD:EE:FF", "fake 2026.10")
            ents = await dev.entities()
            assert ents.buttons["abrir_portao"] == (0x1234ABCD, "Abrir portao")
            assert ents.binary_sensors == {"campainha": (0x3000, "Campainha"), "garagem_1_aberta": (0x3001, "Garagem 1 aberta")}
            await dev.press(0x1234ABCD)
            assert board.pressed == [0x1234ABCD]  # a resposta ao ping só chega depois do comando
        await asyncio.sleep(0.05)
    finally:
        await board.stop()
    assert board.hello[1] == b"zoio"
    assert board.pongs == 1  # respondeu ao ping que a placa mandou
    assert board.disconnects == 1


async def test_states_arrive_while_running():
    board = await FakeEspHome(KEY).start()
    seen = []
    try:
        async with eh.EspHome("127.0.0.1", board.port, KEY) as dev:
            await dev.entities()
            dev.on_state = lambda key, state: seen.append((key, state))
            await dev.subscribe_states()
            task = asyncio.create_task(dev.run())
            await asyncio.sleep(0.05)
            assert seen == [(0x3000, False), (0x3001, False)]  # leitura inicial; o sensor sem leitura fica de fora
            await board.set_state("campainha", True)
            assert seen[-1] == (0x3000, True)
            await dev.press(0x2001)  # com a leitura rodando, a confirmação vem pelo run()
            assert board.pressed == [0x2001]
            await board.stop()
            with pytest.raises(eh.EspHomeError, match="fechou a conexão"):
                await asyncio.wait_for(task, 2)
    finally:
        await board.stop()


async def test_silent_board_is_dropped(monkeypatch):
    monkeypatch.setattr(eh, "KEEPALIVE", 0.2)
    monkeypatch.setattr(eh, "DEAD_AFTER", 0.5)
    board = await FakeEspHome(KEY).start()
    try:
        async with eh.EspHome("127.0.0.1", board.port, KEY) as dev:
            await dev.entities()
            board.silent = True
            with pytest.raises(eh.EspHomeError, match="parou de responder"):
                await asyncio.wait_for(dev.run(), 3)
    finally:
        await board.stop()


async def test_wrong_key_wrong_name_and_plaintext():
    board = await FakeEspHome(KEY).start()
    try:
        with pytest.raises(eh.EspHomeError, match="recusou a chave"):
            async with eh.EspHome("127.0.0.1", board.port, eh.new_key()):
                pass
        with pytest.raises(eh.EspHomeError, match="se apresentou como"):
            async with eh.EspHome("127.0.0.1", board.port, KEY, expected_name="outra-placa"):
                pass
        board.encrypted = False
        with pytest.raises(eh.EspHomeError, match="sem criptografia"):
            async with eh.EspHome("127.0.0.1", board.port, KEY):
                pass
    finally:
        await board.stop()
    assert board.pressed == []


async def test_unreachable_board():
    board = await FakeEspHome(KEY).start()
    port = board.port
    await board.stop()
    with pytest.raises(eh.EspHomeError, match="não consegui conectar"):
        async with eh.EspHome("127.0.0.1", port, KEY, timeout=1):
            pass

    async def silent(r, w):
        await asyncio.sleep(5)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    try:
        with pytest.raises(eh.EspHomeError, match="não respondeu"):
            async with eh.EspHome("127.0.0.1", server.sockets[0].getsockname()[1], KEY, timeout=0.3):
                pass
    finally:
        server.close()
