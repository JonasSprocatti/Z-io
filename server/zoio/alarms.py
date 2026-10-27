"""Escuta os alarmes de movimento do DVR e repassa ao app (tela ao vivo e notificações)."""

from __future__ import annotations

import asyncio
import logging
import time

from . import dvrip
from .config import Config
from .store import Store

log = logging.getLogger(__name__)


class EventBus:
    """Distribui eventos para as telas abertas (Server-Sent Events)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def publish(self, item: dict) -> None:
        for q in list(self._queues):
            if q.full():  # cliente lento: descarta o mais antigo
                q.get_nowait()
            q.put_nowait(item)


class AlarmListener:
    def __init__(self, cfg: Config, store: Store, bus: EventBus, notifier=None):
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.notifier = notifier
        self.connected = False
        self.last_error: str | None = None
        self.last_event_at: float | None = None
        self._active: dict[tuple[int, str], bool] = {}
        self._tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        delay = 2
        while True:
            try:
                conn = dvrip.Connection(self.cfg.dvr_host, self.cfg.dvr_port, self.cfg.dvr_user, self.cfg.dvr_pass)
                await conn.open()
                try:
                    await conn.subscribe_alarms(self.handle)
                    self.connected, self.last_error, delay = True, None, 2
                    log.info("inscrito nos alarmes do DVR")
                    await conn.closed.wait()
                finally:
                    await conn.close()
                    self.connected = False
                    self._active.clear()
                self.last_error = "conexão com o DVR caiu"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc) or exc.__class__.__name__
                log.warning("alarmes: %s; nova tentativa em %ss", self.last_error, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def handle(self, info: dict) -> None:
        try:
            camera = int(info.get("Channel", 0)) + 1  # o DVR conta a partir de 0
        except (TypeError, ValueError):
            return
        kind = str(info.get("Event", "?"))
        status = str(info.get("Status", "?"))
        cam = self.cfg.camera(camera)
        if cam is None:
            return
        # O DVR repete "Start" enquanto há movimento; só registramos a transição.
        key = (camera, kind)
        if status == "Start" and self._active.get(key):
            return
        if status == "Stop" and not self._active.get(key, True):
            return
        self._active[key] = status == "Start"
        event = self.store.add_event(camera, kind, status, info.get("StartTime"))
        self.last_event_at = time.time()
        event["name"] = cam.name
        self.bus.publish({"type": "motion", **event})
        if status == "Start" and kind == "VideoMotion" and self.notifier and self.notifier.should_notify(camera):
            task = asyncio.create_task(self.notifier.motion(camera, cam.name, event["id"]))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
