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


async def test_press_button_over_encrypted_api():
    board = await FakeEspHome(KEY).start()
    try:
        async with eh.EspHome("127.0.0.1", board.port, KEY, expected_name="zoio-portao") as dev:
            assert (dev.name, dev.mac, dev.server_info) == ("zoio-portao", "AA:BB:CC:DD:EE:FF", "fake 2026.10")
            assert await dev.buttons() == {"abrir_portao": (0x1234ABCD, "Abrir portao")}
            assert await dev.press("abrir_portao") == "abrir_portao"
        await asyncio.sleep(0.05)
    finally:
        await board.stop()
    assert board.pressed == [0x1234ABCD]
    assert board.hello[1] == b"zoio"
    assert board.pongs == 1  # respondeu ao ping que a placa mandou
    assert board.disconnects == 1


async def test_single_button_is_used_when_name_differs():
    board = await FakeEspHome(KEY, buttons={"abrir": (5, "Abrir")}).start()
    try:
        async with eh.EspHome("127.0.0.1", board.port, KEY) as dev:
            assert await dev.press("abrir_portao") == "abrir"
        board.buttons = {"a": (1, "A"), "b": (2, "B")}
        async with eh.EspHome("127.0.0.1", board.port, KEY) as dev:
            with pytest.raises(eh.EspHomeError, match="não existe"):
                await dev.press("abrir_portao")
    finally:
        await board.stop()
    assert board.pressed == [5]


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
