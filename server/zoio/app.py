"""Servidor HTTP do Zóio: app web, API e passagem do vídeo ao vivo do go2rtc."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from . import __version__
from .alarms import AlarmListener, EventBus
from .clips import Clips
from .config import Config
from .devices import APPROVED, PENDING, REVOKED, DeviceError, Devices, gate_message, normalize_code
from .devices import public as public_device
from .esphome import EspHomeError
from .gate import FAILED, Gate, GateBusy, GateError
from .push import Notifier
from .store import Store

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
DVR_TIME = "%Y-%m-%d %H:%M:%S"

CFG = "zoio_cfg"
STORE = "zoio_store"
BUS = "zoio_bus"
CLIPS = "zoio_clips"
ALARMS = "zoio_alarms"
NOTIFIER = "zoio_notifier"
HTTP = "zoio_http"
DEVICES = "zoio_devices"
GATE = "zoio_gate"
BG_TASKS = "zoio_bg_tasks"

SESSION_COOKIE = "zoio_sessao"

# Caminhos do go2rtc liberados pelo Zóio. A interface de administração
# dele (que permite criar fontes novas) nunca é exposta.
LIVE_PATHS = {
    "video-rtc.js": "/video-rtc.js",
    "video-stream.js": "/video-stream.js",
    "ws": "/api/ws",
    "stream.m3u8": "/api/stream.m3u8",
}
LIVE_HLS = re.compile(r"^hls/[\w.-]+$")


# ---------------------------------------------------------------- segurança


def current_user(request: web.Request) -> str | None:
    """Usuário do Tailscale, informado pelo `tailscale serve` em cada pedido."""
    login = request.headers.get("Tailscale-User-Login")
    return login.lower() if login else None


# Liberados sem sessão: a "casca" do app e o necessário para um aparelho se cadastrar e entrar.
PUBLIC_PATHS = {"/", "/sw.js", "/manifest.webmanifest", "/api/devices/me", "/api/devices/register", "/api/devices/challenge", "/api/devices/login"}
HLS_ID = re.compile(r"^[0-9A-Za-z]{8}$")


def is_public(request: web.Request) -> bool:
    path = request.path
    if path in PUBLIC_PATHS or path.startswith("/web/"):
        return True
    # No iPhone antigo (sem MSE), o player nativo busca a lista e os segmentos HLS
    # sozinho, a partir de uma lista em data: URL, e nem sempre manda o cookie. Esses
    # endereços só existem depois que um aparelho aprovado abriu o WebSocket, e
    # carregam um id aleatório de ~47 bits gerado pelo go2rtc para aquela sessão.
    if path.startswith("/live/hls/") and HLS_ID.match(request.query.get("id", "")):
        return True
    return False


SAFE_HOST = re.compile(r"^[A-Za-z0-9.\-]+(:\d+)?$")


def content_security_policy(request: web.Request) -> str:
    host = request.host if SAFE_HOST.match(request.host or "") else None
    connect = f"connect-src 'self' wss://{host} ws://{host}" if host else "connect-src 'self'"
    return "; ".join(
        [
            "default-src 'self'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",  # o player do go2rtc injeta um <style>
            "img-src 'self' data: blob:",
            "media-src 'self' data: blob:",  # MSE usa blob:, o HLS do iPhone usa data:
            connect,
            "worker-src 'self'",
            "manifest-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


@web.middleware
async def security(request: web.Request, handler):
    cfg = request.app[CFG]
    if cfg.allowed_users and current_user(request) not in cfg.allowed_users:
        raise web.HTTPForbidden(text="Aparelho ou usuário não autorizado no Zóio.")
    # Bloqueia pedidos vindos de outros sites (CSRF e WebSocket entre origens).
    origin = request.headers.get("Origin")
    if origin and (request.method not in ("GET", "HEAD") or request.path == "/live/ws"):
        if urlsplit(origin).netloc != request.host:
            raise web.HTTPForbidden(text="Origem não permitida.")
    request["session"] = None
    if cfg.device_approval:
        session = request.app[DEVICES].session(request.cookies.get(SESSION_COOKIE))
        request["session"] = session
        if session is None and not is_public(request):
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "aparelho_nao_autorizado"}),
                content_type="application/json",
            )
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", content_security_policy(request))
    return resp


def current_device(request: web.Request) -> dict | None:
    session = request.get("session")
    return session.device if session else None


def require_admin(request: web.Request) -> dict:
    device = current_device(request)
    if not request.app[CFG].device_approval:
        raise web.HTTPNotFound(text="aprovação de aparelhos desligada")
    if device is None or not device["admin"]:
        raise web.HTTPForbidden(text="só um aparelho administrador pode fazer isso")
    return device


async def json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="JSON inválido")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="JSON inválido")
    return body


# ---------------------------------------------------------------- páginas


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def service_worker(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "sw.js", headers={"Cache-Control": "no-cache", "Content-Type": "text/javascript"})


async def manifest(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "manifest.webmanifest", headers={"Content-Type": "application/manifest+json"})


# ---------------------------------------------------------------- API


async def api_config(request: web.Request) -> web.Response:
    cfg = request.app[CFG]
    notifier = request.app.get(NOTIFIER)
    return web.json_response(
        {
            "version": __version__,
            "user": current_user(request),
            "cameras": [{"number": c.number, "name": c.name, "main": c.main, "sub": c.sub} for c in cfg.cameras],
            "push_key": notifier.vapid.public_key if notifier else None,
            "device_approval": cfg.device_approval,
            "device": public_device(current_device(request), current_device(request)["id"]) if current_device(request) else None,
            "gate": {
                "enabled": request.app[GATE].enabled,
                "camera": cfg.gate_camera,
                "allowed": bool((current_device(request) or {}).get("gate")),
            },
        }
    )


async def api_status(request: web.Request) -> web.Response:
    alarms = request.app.get(ALARMS)
    go2rtc_ok = False
    try:
        async with request.app[HTTP].get(request.app[CFG].go2rtc_url + "/api", timeout=aiohttp.ClientTimeout(total=3)) as r:
            go2rtc_ok = r.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return web.json_response(
        {
            "live": go2rtc_ok,
            "alerts": bool(alarms and alarms.connected),
            "alerts_error": alarms.last_error if alarms else "alertas desligados",
            "last_event_at": alarms.last_event_at if alarms else None,
            "gate": request.app[GATE].enabled,
            "gate_error": request.app[CFG].gate_error,
            "time": time.time(),
        }
    )


def _int(request: web.Request, name: str, default: int | None = None) -> int | None:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise web.HTTPBadRequest(text=f"{name} inválido")


async def api_events(request: web.Request) -> web.Response:
    cfg = request.app[CFG]
    rows = request.app[STORE].events(
        limit=_int(request, "limit", 100),
        before_id=_int(request, "before"),
        camera=_int(request, "camera"),
        status=None if request.query.get("all") else "Start",
    )
    names = {c.number: c.name for c in cfg.cameras}
    for r in rows:
        r["name"] = names.get(r["camera"], f"Câmera {r['camera']}")
    return web.json_response(rows)


async def api_event_stream(request: web.Request) -> web.StreamResponse:
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    bus = request.app[BUS]
    q = bus.subscribe()
    try:
        await resp.write(b"retry: 5000\n\n")
        while True:
            try:
                item = await asyncio.wait_for(q.get(), 25)
                if item.get("type") == "device" and not (current_device(request) or {}).get("admin"):
                    continue
                await resp.write(f"data: {json.dumps(item, ensure_ascii=False)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        bus.unsubscribe(q)
    return resp


def _camera_or_404(request: web.Request, number: int | None):
    cam = request.app[CFG].camera(number or 0)
    if cam is None:
        raise web.HTTPNotFound(text="câmera não encontrada")
    return cam


async def api_recordings(request: web.Request) -> web.Response:
    cam = _camera_or_404(request, _int(request, "camera"))
    try:
        day = date.fromisoformat(request.query.get("date", date.today().isoformat()))
    except ValueError:
        raise web.HTTPBadRequest(text="data inválida (use AAAA-MM-DD)")
    begin = datetime.combine(day, datetime.min.time())
    end = begin + timedelta(days=1) - timedelta(seconds=1)
    try:
        items = await request.app[CLIPS].search(cam.number, begin.strftime(DVR_TIME), end.strftime(DVR_TIME))
    except Exception as exc:
        log.warning("busca de gravações falhou: %s", exc)
        raise web.HTTPBadGateway(text=f"Não consegui falar com o DVR: {exc}")
    items.sort(key=lambda x: x["begin"], reverse=True)
    return web.json_response(items)


def _dvr_time(value) -> str:
    try:
        return datetime.strptime(str(value), DVR_TIME).strftime(DVR_TIME)
    except ValueError:
        raise web.HTTPBadRequest(text="horário inválido")


async def api_clip_create(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="JSON inválido")
    cam = _camera_or_404(request, int(body.get("camera") or 0))
    filename = str(body.get("file", ""))
    # Nomes do DVR são caminhos como /idea0/2026-10-26/001/12.00.00-13.00.00[R][@1a2][0].h264
    if not filename.startswith("/") or ".." in filename or len(filename) > 256:
        raise web.HTTPBadRequest(text="arquivo inválido")
    size = body.get("size")
    job = request.app[CLIPS].request(cam.number, filename, _dvr_time(body.get("begin")), _dvr_time(body.get("end")), int(size) if size else None)
    return web.json_response(job.public())


async def api_clip_get(request: web.Request) -> web.Response:
    job = request.app[CLIPS].jobs.get(request.match_info["id"])
    if job is None:
        raise web.HTTPNotFound(text="clipe não encontrado")
    return web.json_response(job.public())


async def api_clip_video(request: web.Request) -> web.FileResponse:
    job_id = request.match_info["id"]
    if not re.fullmatch(r"[0-9a-f]{16}", job_id):
        raise web.HTTPNotFound()
    path = request.app[CLIPS].path(job_id)
    if not path.exists():
        raise web.HTTPNotFound(text="clipe não encontrado")
    disposition = "attachment" if request.query.get("download") else "inline"
    return web.FileResponse(path, headers={"Content-Disposition": f'{disposition}; filename="zoio-{job_id}.mp4"', "Cache-Control": "private, max-age=86400"})


async def api_push_subscribe(request: web.Request) -> web.Response:
    if NOTIFIER not in request.app:
        raise web.HTTPServiceUnavailable(text="notificações desligadas")
    body = await json_body(request)
    sub = body.get("subscription") or {}
    endpoint = str(sub.get("endpoint", ""))
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise web.HTTPBadRequest(text="inscrição inválida")
    try:
        cameras = [int(c) for c in body.get("cameras") or []] or None
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="câmeras inválidas")
    device = current_device(request)
    request.app[STORE].save_subscription(
        {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}},
        current_user(request),
        cameras,
        device["id"] if device else None,
    )
    return web.json_response({"ok": True})


async def api_push_unsubscribe(request: web.Request) -> web.Response:
    body = await json_body(request)
    request.app[STORE].delete_subscription(str(body.get("endpoint", "")))
    return web.json_response({"ok": True})


async def api_push_test(request: web.Request) -> web.Response:
    notifier = request.app.get(NOTIFIER)
    if notifier is None:
        raise web.HTTPServiceUnavailable(text="notificações desligadas")
    body = await json_body(request)
    endpoint = (body.get("subscription") or {}).get("endpoint")
    sub = next((s["sub"] for s in request.app[STORE].subscriptions() if s["sub"]["endpoint"] == endpoint), None)
    if sub is None:
        raise web.HTTPNotFound(text="este aparelho não está inscrito")
    payload = json.dumps({"title": "Zóio", "body": "Notificações funcionando neste aparelho.", "tag": "teste"}).encode()
    ok = await notifier.send(sub, payload)
    return web.json_response({"ok": ok})


# ---------------------------------------------------------------- aparelhos


def _devices(request: web.Request) -> Devices:
    if not request.app[CFG].device_approval:
        raise web.HTTPNotFound(text="aprovação de aparelhos desligada")
    return request.app[DEVICES]


async def api_device_me(request: web.Request) -> web.Response:
    device = current_device(request)
    return web.json_response(
        {
            "required": request.app[CFG].device_approval,
            "device": public_device(device, device["id"]) if device else None,
            "has_admin": request.app[DEVICES].admin_count() > 0 if request.app[CFG].device_approval else None,
        }
    )


async def api_device_register(request: web.Request) -> web.Response:
    devices = _devices(request)
    body = await json_body(request)
    try:
        device, created = devices.register(body.get("public_key"), str(body.get("name", "")), current_user(request), request.headers.get("User-Agent"))
    except DeviceError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    if device["status"] == REVOKED:
        return web.json_response({"status": REVOKED}, status=403)
    if created:
        log.info("aparelho novo pedindo acesso: %s (%s)", device["name"], device["code"])
        request.app[BUS].publish({"type": "device", "status": PENDING, "id": device["id"], "name": device["name"], "code": device["code"]})
        notifier = request.app.get(NOTIFIER)
        if notifier:
            task = asyncio.create_task(notifier.device_request(device["name"], device["code"]))
            request.app[BG_TASKS].add(task)
            task.add_done_callback(request.app[BG_TASKS].discard)
    return web.json_response({"id": device["id"], "code": device["code"], "status": device["status"]})


async def api_device_challenge(request: web.Request) -> web.Response:
    try:
        return web.json_response({"nonce": _devices(request).challenge()})
    except DeviceError as exc:
        raise web.HTTPTooManyRequests(text=str(exc))


async def api_device_login(request: web.Request) -> web.Response:
    devices = _devices(request)
    body = await json_body(request)
    try:
        device = devices.verify(str(body.get("id", "")), str(body.get("nonce", "")), str(body.get("signature", "")))
    except LookupError:
        return web.json_response({"status": "unknown"}, status=404)
    except DeviceError as exc:
        return web.json_response({"status": "invalid", "error": str(exc)}, status=401)
    if device["status"] != APPROVED:
        # Assinatura válida, mas sem acesso ainda: responde 200 sem sessão (o app consulta em loop).
        return web.json_response({"status": device["status"], "code": device["code"]})
    token = devices.open_session(device["id"])
    resp = web.json_response({"status": APPROVED, "device": public_device(device, device["id"])})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=devices.session_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )
    return resp


async def api_device_logout(request: web.Request) -> web.Response:
    session = request.get("session")
    if session:
        request.app[DEVICES].close_session(session.token_hash)
    resp = web.json_response({"ok": True})
    resp.del_cookie(SESSION_COOKIE, path="/")
    return resp


async def api_device_rename(request: web.Request) -> web.Response:
    device = current_device(request)
    if device is None:
        raise web.HTTPNotFound(text="aprovação de aparelhos desligada")
    body = await json_body(request)
    name = " ".join(str(body.get("name", "")).split())[:60]
    if not name:
        raise web.HTTPBadRequest(text="nome vazio")
    request.app[STORE].update_device(device["id"], name=name)
    return web.json_response({"ok": True, "name": name})


async def api_device_list(request: web.Request) -> web.Response:
    me = require_admin(request)
    order = {PENDING: 0, APPROVED: 1, REVOKED: 2}
    items = sorted(request.app[STORE].devices(), key=lambda d: (order.get(d["status"], 3), -d["created_at"]))
    return web.json_response([public_device(d, me["id"]) for d in items])


def _target_device(request: web.Request) -> dict:
    device = request.app[STORE].device(request.match_info["id"])
    if device is None:
        raise web.HTTPNotFound(text="aparelho não encontrado")
    return device


async def api_device_approve(request: web.Request) -> web.Response:
    """Aprova pelo código que aparece na tela do aparelho novo.

    Aprovar pelo código (e não por um item da lista) garante que quem aprova
    está olhando para o aparelho certo: um intruso que se cadastrar com o
    mesmo nome terá outro código.
    """
    me = require_admin(request)
    body = await json_body(request)
    code = normalize_code(str(body.get("code", "")))
    target = next((d for d in request.app[STORE].devices(status=PENDING) if normalize_code(d["code"]) == code), None) if code else None
    if target is None:
        raise web.HTTPNotFound(text="nenhum aparelho aguardando com esse código")
    device = request.app[DEVICES].approve(target["id"], admin=bool(body.get("admin")), by=me["id"], gate=bool(body["gate"]) if "gate" in body else None)
    request.app[BUS].publish({"type": "device", "status": APPROVED, "id": device["id"], "name": device["name"], "code": device["code"]})
    return web.json_response(public_device(device, me["id"]))


async def api_device_revoke(request: web.Request) -> web.Response:
    me = require_admin(request)
    target = _target_device(request)
    devices = request.app[DEVICES]
    if target["admin"] and target["status"] == APPROVED and devices.admin_count() <= 1:
        raise web.HTTPConflict(text="este é o único administrador; aprove outro antes (ou use o Pi)")
    device = devices.revoke(target["id"])
    request.app[BUS].publish({"type": "device", "status": REVOKED, "id": device["id"], "name": device["name"], "code": device["code"]})
    return web.json_response(public_device(device, me["id"]))


async def api_device_delete(request: web.Request) -> web.Response:
    require_admin(request)
    target = _target_device(request)
    if target["status"] == APPROVED:
        raise web.HTTPConflict(text="revogue o aparelho antes de excluir")
    request.app[DEVICES].delete(target["id"])
    return web.json_response({"ok": True})


async def api_device_gate(request: web.Request) -> web.Response:
    me = require_admin(request)
    target = _target_device(request)
    body = await json_body(request)
    try:
        device = request.app[DEVICES].set_gate(target["id"], bool(body.get("allow")))
    except DeviceError as exc:
        raise web.HTTPConflict(text=str(exc))
    return web.json_response(public_device(device, me["id"]))


# ---------------------------------------------------------------- portão


def _gate_device(request: web.Request) -> dict:
    """Aparelho com sessão e permissão para o portão (que precisa estar configurado)."""
    if not request.app[GATE].enabled:
        raise web.HTTPNotFound(text="portão não configurado")
    device = current_device(request)
    if device is None or not device.get("gate"):
        raise web.HTTPForbidden(text="este aparelho não tem permissão para abrir o portão")
    return device


async def api_gate_challenge(request: web.Request) -> web.Response:
    _gate_device(request)
    try:
        return web.json_response({"nonce": request.app[DEVICES].challenge()})
    except DeviceError as exc:
        raise web.HTTPTooManyRequests(text=str(exc))


async def api_gate_open(request: web.Request) -> web.Response:
    """Abre o portão. Além da sessão, o aparelho assina um desafio novo com a chave dele."""
    device = _gate_device(request)
    body = await json_body(request)
    try:
        request.app[DEVICES].verify(device["id"], str(body.get("nonce", "")), str(body.get("signature", "")), message=gate_message)
    except (DeviceError, LookupError) as exc:
        raise web.HTTPUnauthorized(text=f"confirmação do aparelho falhou: {exc}")
    try:
        event = await request.app[GATE].open(device["name"], device["id"], request.app[HTTP])
    except GateBusy as exc:
        raise web.HTTPTooManyRequests(text=str(exc))
    except GateError as exc:
        raise web.HTTPNotFound(text=str(exc))
    return web.json_response(event, status=502 if event["status"] == FAILED else 200)


async def api_gate_check(request: web.Request) -> web.Response:
    require_admin(request)
    try:
        return web.json_response(await request.app[GATE].check())
    except GateError as exc:
        raise web.HTTPNotFound(text=str(exc))
    except EspHomeError as exc:
        raise web.HTTPBadGateway(text=str(exc))


async def api_gate_photo(request: web.Request) -> web.FileResponse:
    raw = request.match_info["id"]
    if not raw.isdigit():
        raise web.HTTPNotFound()
    path = request.app[GATE].photo_path(int(raw))
    if not path.exists():
        raise web.HTTPNotFound(text="foto não encontrada")
    return web.FileResponse(path, headers={"Content-Type": "image/jpeg", "Cache-Control": "private, max-age=86400"})


# ---------------------------------------------------------------- ao vivo


def _live_target(request: web.Request) -> str:
    tail = request.match_info["tail"]
    path = LIVE_PATHS.get(tail) or ("/api/" + tail if LIVE_HLS.match(tail) else None)
    if path is None:
        raise web.HTTPNotFound()
    if tail in ("ws", "stream.m3u8"):
        allowed = {name for c in request.app[CFG].cameras for name in (c.main, c.sub)}
        if request.query.get("src") not in allowed:
            raise web.HTTPForbidden(text="câmera desconhecida")
    query = request.query_string
    return request.app[CFG].go2rtc_url + path + (f"?{query}" if query else "")


async def live_proxy(request: web.Request) -> web.StreamResponse:
    target = _live_target(request)
    if request.match_info["tail"] == "ws":
        return await _ws_proxy(request, target.replace("http", "ws", 1))
    session = request.app[HTTP]
    try:
        async with session.get(target) as upstream:
            resp = web.StreamResponse(status=upstream.status)
            for h in ("Content-Type", "Cache-Control"):
                if h in upstream.headers:
                    resp.headers[h] = upstream.headers[h]
            await resp.prepare(request)
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            return resp
    except aiohttp.ClientError:
        raise web.HTTPBadGateway(text="ao vivo indisponível (go2rtc parado?)")


async def _ws_proxy(request: web.Request, target: str) -> web.WebSocketResponse:
    client = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await client.prepare(request)

    async def pump(src, dst):
        async for msg in src:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await dst.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await dst.send_bytes(msg.data)
        await dst.close()

    try:
        async with request.app[HTTP].ws_connect(target, max_msg_size=0) as upstream:
            downstream = asyncio.create_task(pump(upstream, client))
            try:
                await pump(client, upstream)
            finally:
                downstream.cancel()
                await asyncio.gather(downstream, return_exceptions=True)
    except (aiohttp.ClientError, ConnectionResetError) as exc:
        log.debug("ws ao vivo encerrado: %s", exc)
    finally:
        await client.close()
    return client


# ---------------------------------------------------------------- montagem


async def _lifecycle(app: web.Application):
    cfg = app[CFG]
    app[HTTP] = aiohttp.ClientSession()
    tasks = []
    if cfg.alerts:
        tasks.append(asyncio.create_task(app[ALARMS].run()))

    async def housekeeping():
        while True:
            app[STORE].prune_events()
            app[STORE].prune_sessions()
            app[GATE].prune()
            await asyncio.sleep(6 * 3600)

    tasks.append(asyncio.create_task(housekeeping()))
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if NOTIFIER in app:
        await app[NOTIFIER].close()
    await app[HTTP].close()
    app[STORE].close()


def create_app(cfg: Config, enable_push: bool = True) -> web.Application:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    app = web.Application(middlewares=[security], client_max_size=64 * 1024)
    app[CFG] = cfg
    app[STORE] = store = Store(cfg.data_dir / "zoio.db")
    app[BUS] = bus = EventBus()
    app[CLIPS] = Clips(cfg)
    app[DEVICES] = Devices(store, cfg.session_days)
    app[BG_TASKS] = set()
    notifier = None
    if enable_push:
        app[NOTIFIER] = notifier = Notifier(store, cfg.push_contact, cfg.notify_cooldown, cfg.notify_channels, approved_only=cfg.device_approval)
    app[ALARMS] = AlarmListener(cfg, store, bus, notifier)
    app[GATE] = Gate(cfg, store, bus, notifier)
    app.cleanup_ctx.append(_lifecycle)

    r = app.router
    r.add_get("/", index)
    r.add_get("/sw.js", service_worker)
    r.add_get("/manifest.webmanifest", manifest)
    r.add_static("/web/", WEB_DIR)
    r.add_get("/api/config", api_config)
    r.add_get("/api/status", api_status)
    r.add_get("/api/events", api_events)
    r.add_get("/api/events/stream", api_event_stream)
    r.add_get("/api/recordings", api_recordings)
    r.add_post("/api/clips", api_clip_create)
    r.add_get("/api/clips/{id}", api_clip_get)
    r.add_get("/api/clips/{id}/video.mp4", api_clip_video)
    r.add_post("/api/push/subscribe", api_push_subscribe)
    r.add_post("/api/push/unsubscribe", api_push_unsubscribe)
    r.add_post("/api/push/test", api_push_test)
    r.add_get("/api/devices/me", api_device_me)
    r.add_post("/api/devices/register", api_device_register)
    r.add_post("/api/devices/challenge", api_device_challenge)
    r.add_post("/api/devices/login", api_device_login)
    r.add_post("/api/devices/logout", api_device_logout)
    r.add_post("/api/devices/rename", api_device_rename)
    r.add_get("/api/devices", api_device_list)
    r.add_post("/api/devices/approve", api_device_approve)
    r.add_post("/api/devices/{id}/revoke", api_device_revoke)
    r.add_post("/api/devices/{id}/delete", api_device_delete)
    r.add_post("/api/devices/{id}/gate", api_device_gate)
    r.add_post("/api/gate/challenge", api_gate_challenge)
    r.add_post("/api/gate/open", api_gate_open)
    r.add_post("/api/gate/check", api_gate_check)
    r.add_get("/api/gate/photos/{id}.jpg", api_gate_photo)
    r.add_get("/live/{tail:.+}", live_proxy)
    return app
