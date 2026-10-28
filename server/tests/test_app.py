import pytest
from aiohttp import web

from zoio.app import create_app
from zoio import __version__


@pytest.fixture
async def go2rtc(aiohttp_server):
    """go2rtc simulado: ecoa o que recebe no WebSocket e serve o script do player."""
    app = web.Application()

    async def ws(request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        async for msg in sock:
            await sock.send_str("eco:" + request.query["src"] + ":" + msg.data)
        return sock

    async def script(request):
        return web.Response(text="export class VideoRTC {}", content_type="text/javascript")

    async def api(request):
        return web.json_response({})

    app.router.add_get("/api/ws", ws)
    app.router.add_get("/video-rtc.js", script)
    app.router.add_get("/api", api)
    app.router.add_get("/api/streams", api)
    return await aiohttp_server(app)


@pytest.fixture
async def client(aiohttp_client, make_cfg, go2rtc):
    # A aprovação de aparelhos tem testes próprios (test_devices.py).
    cfg = make_cfg(1, allowed_users={"jonas@github"}, alerts=False, device_approval=False)
    cfg.go2rtc_url = str(go2rtc.make_url("")).rstrip("/")
    return await aiohttp_client(create_app(cfg))


H = {"Tailscale-User-Login": "Jonas@github"}


async def test_rejects_unknown_user(client):
    assert (await client.get("/api/config")).status == 403
    assert (await client.get("/api/config", headers={"Tailscale-User-Login": "outro@x"})).status == 403


async def test_config_and_static(client):
    resp = await client.get("/api/config", headers=H)
    data = await resp.json()
    assert data["user"] == "jonas@github"
    assert [c["sub"] for c in data["cameras"]] == ["cam1_sub", "cam2_sub", "cam3_sub", "cam4_sub"]
    assert data["push_key"]
    page = await client.get("/", headers=H)
    assert page.status == 200
    html = await page.text()
    assert f'/web/app.css?v={__version__}' in html and f'/web/app.js?v={__version__}' in html
    css = await client.get(f"/web/app.css?v={__version__}", headers=H)
    assert css.status == 200 and css.headers["Cache-Control"] == "no-cache"
    assert (await client.get("/sw.js", headers=H)).status == 200
    status = await (await client.get("/api/status", headers=H)).json()
    assert status["live"] is True


async def test_live_proxy_only_known_streams(client):
    assert (await client.get("/live/video-rtc.js", headers=H)).status == 200
    assert (await client.get("/live/api/streams", headers=H)).status == 404
    assert (await client.get("/live/ws?src=exec:rm", headers=H)).status == 403
    async with client.ws_connect("/live/ws?src=cam2_sub", headers=H) as ws:
        await ws.send_str("oi")
        assert (await ws.receive()).data == "eco:cam2_sub:oi"


async def test_cross_origin_blocked(client):
    resp = await client.post("/api/clips", json={}, headers={**H, "Origin": "https://malicioso.example"})
    assert resp.status == 403


async def test_clip_validation(client):
    bad = {"camera": 1, "file": "../etc/passwd", "begin": "2026-10-26 12:00:00", "end": "2026-10-26 12:01:00"}
    assert (await client.post("/api/clips", json=bad, headers=H)).status == 400
    assert (await client.post("/api/clips", json={**bad, "camera": 99, "file": "/a"}, headers=H)).status == 404
    assert (await client.get("/api/clips/../../x/video.mp4", headers=H)).status == 404


async def test_push_subscribe(client):
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
    assert (await client.post("/api/push/subscribe", json={"subscription": sub}, headers=H)).status == 200
    assert (await client.post("/api/push/subscribe", json={"subscription": {"endpoint": "http://x"}}, headers=H)).status == 400
    assert (await client.post("/api/push/test", json={"subscription": {"endpoint": "https://outro"}}, headers=H)).status == 404
