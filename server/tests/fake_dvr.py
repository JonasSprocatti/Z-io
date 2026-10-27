"""DVR simulado que fala o protocolo DVRIP o suficiente para os testes."""

from __future__ import annotations

import asyncio
import json
import struct
from datetime import datetime

from zoio import dvrip

H = dvrip.HEADER


def frame(kind: int, payload: bytes, codec: int = 3, fps: int = 15) -> bytes:
    if kind == 0xFC:
        return b"\x00\x00\x01\xfc" + struct.pack("<BBBBII", codec, fps, 1280 // 8, 720 // 8, 0, len(payload)) + payload
    if kind == 0xFD:
        return b"\x00\x00\x01\xfd" + struct.pack("<I", len(payload)) + payload
    return bytes([0, 0, 1, kind]) + struct.pack("<BBH", 0x0E, 2, len(payload)) + payload


def wrap_annexb(es: bytes, codec: int = 3, fps: int = 15) -> bytes:
    """Converte um Annex B em quadros no formato privado (um NAL por quadro, com áudio no meio)."""
    out = bytearray()
    parts = es.split(b"\x00\x00\x00\x01")
    for i, nal in enumerate(p for p in parts if p):
        nal = b"\x00\x00\x00\x01" + nal
        out += frame(0xFC if i % 30 == 0 else 0xFD, nal, codec, fps)
        if i % 5 == 0:
            out += frame(0xFA, b"\xd5" * 320)
    return bytes(out)


class FakeDVR:
    def __init__(self, user="admin", password="segredo", files=None, recording: bytes = b""):
        self.user, self.password = user, password
        self.files = files or []
        self.recording = recording
        self.writers: list[asyncio.StreamWriter] = []
        self.alarm_writers: list[asyncio.StreamWriter] = []
        self.server: asyncio.Server | None = None
        self.port = 0
        self.queries = 0
        self.clock = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async def start(self) -> "FakeDVR":
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for w in self.writers:
            w.close()
        self.server.close()
        await self.server.wait_closed()

    def _send(self, w: asyncio.StreamWriter, msgid: int, body: bytes | dict, session: int = 0x2A) -> None:
        if isinstance(body, dict):
            body = json.dumps(body).encode() + b"\n\x00"
        w.write(H.pack(0xFF, 1, session, 0, 0, 0, msgid, len(body)) + body)

    async def push_alarm(self, channel: int, status: str, event: str = "VideoMotion") -> None:
        for w in self.alarm_writers:
            self._send(w, dvrip.ALARM_INFO, {"AlarmInfo": {"Channel": channel, "Event": event, "StartTime": "2026-10-26 12:00:00", "Status": status}, "Name": "AlarmInfo"})
            await w.drain()

    async def _client(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.writers.append(w)
        try:
            while True:
                head = await r.readexactly(H.size)
                *_, msgid, length = H.unpack(head)
                req = json.loads((await r.readexactly(length)).rstrip(b"\x00\n"))
                await self._handle(w, msgid, req)
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass

    async def _handle(self, w, msgid: int, req: dict) -> None:
        if msgid == dvrip.LOGIN:
            ok = req["UserName"] == self.user and req["PassWord"] == dvrip.sofia_hash(self.password)
            self._send(w, msgid + 1, {"Ret": 100, "SessionID": "0x0000002A", "AliveInterval": 1} if ok else {"Ret": 203})
        elif msgid == dvrip.KEEPALIVE:
            self._send(w, msgid + 1, {"Ret": 100, "Name": "KeepAlive"})
        elif msgid == dvrip.ALARM_SET:
            self.alarm_writers.append(w)
            self._send(w, msgid + 1, {"Ret": 100, "Name": ""})
        elif msgid == dvrip.TIME_QUERY:
            self._send(w, msgid + 1, {"Ret": 100, "Name": "OPTimeQuery", "OPTimeQuery": self.clock})
        elif msgid == dvrip.FILE_QUERY:
            self.queries += 1
            q = req["OPFileQuery"]
            files = [f for f in self.files if f["BeginTime"] >= q["BeginTime"] and f["Channel"] == q["Channel"]][:64]
            files = [{k: v for k, v in f.items() if k != "Channel"} for f in files]
            self._send(w, msgid + 1, {"Ret": 100 if files else 119, "OPFileQuery": files})
        elif msgid == dvrip.PLAYBACK_CLAIM:
            self._send(w, msgid + 1, {"Ret": 100, "Name": "OPPlayBack"})
        elif msgid == dvrip.PLAYBACK and req["OPPlayBack"]["Action"] == "DownloadStart":
            self._send(w, msgid + 1, {"Ret": 100, "Name": "OPPlayBack"})
            data = self.recording
            for i in range(0, len(data), 8192):  # pedaços que cortam quadros no meio, como o DVR real
                self._send(w, dvrip.PLAYBACK_DATA, data[i : i + 8192])
            self._send(w, dvrip.PLAYBACK_DATA, b"")
        elif msgid == dvrip.PLAYBACK:
            self._send(w, msgid + 1, {"Ret": 100, "Name": "OPPlayBack"})
