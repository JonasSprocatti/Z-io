import asyncio
import json
import subprocess

import pytest
from fake_dvr import FakeDVR, wrap_annexb

from zoio import dvrip
from zoio.alarms import AlarmListener, EventBus
from zoio.clips import Clips
from zoio.store import Store
from zoio.xmstream import Demuxer


def test_sofia_hash_known_value():
    # valor de referência da biblioteca python-dvr para senha vazia
    assert dvrip.sofia_hash("") == "tlJwpbo6"


async def test_login_rejects_wrong_password():
    dvr = await FakeDVR().start()
    try:
        with pytest.raises(dvrip.DVRIPError, match="203"):
            async with dvrip.Connection("127.0.0.1", dvr.port, "admin", "errada"):
                pass
    finally:
        await dvr.stop()


async def test_query_files_paginates():
    files = [
        {"Channel": 0, "BeginTime": f"2026-10-26 {h:02d}:{m:02d}:00", "EndTime": f"2026-10-26 {h:02d}:{m:02d}:59", "FileLength": "0x00000400", "FileName": f"/idea0/{h:02d}{m:02d}.h264"}
        for h in range(3) for m in range(50)
    ]
    dvr = await FakeDVR(files=files).start()
    try:
        async with dvrip.Connection("127.0.0.1", dvr.port, "admin", "segredo") as conn:
            got = await conn.query_files(0, "2026-10-26 00:00:00", "2026-10-26 23:59:59")
        assert [f["FileName"] for f in got] == [f["FileName"] for f in files]
        assert dvr.queries >= 3
    finally:
        await dvr.stop()


def test_demuxer_handles_split_chunks(hevc_es):
    data = b"lixo" + wrap_annexb(hevc_es)
    demux = Demuxer()
    out = b"".join(demux.feed(data[i : i + 777]) for i in range(0, len(data), 777))
    assert out == hevc_es
    assert demux.info.codec == "hevc" and demux.info.fps == 15
    assert demux.info.audio_frames > 0
    assert demux.info.skipped_bytes == 4


async def test_download_becomes_playable_mp4(make_cfg, hevc_es):
    dvr = await FakeDVR(recording=wrap_annexb(hevc_es)).start()
    try:
        clips = Clips(make_cfg(dvr.port))
        job = clips.request(1, "/idea0/x.h264", "2026-10-26 12:00:00", "2026-10-26 12:00:02", len(hevc_es))
        for _ in range(200):
            if job.state in ("pronto", "erro"):
                break
            await asyncio.sleep(0.05)
        assert job.state == "pronto", job.error
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_tag_string,nb_frames", "-of", "json", str(clips.path(job.id))],
            capture_output=True, check=True, text=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        assert stream["codec_name"] == "hevc" and stream["codec_tag_string"] == "hvc1"
        assert int(stream["nb_frames"]) == 30
        # pedir de novo usa o cache
        assert clips.request(1, "/idea0/x.h264", "2026-10-26 12:00:00", "2026-10-26 12:00:02", None).state == "pronto"
    finally:
        await dvr.stop()


async def test_alarms_reach_store_bus_and_notifier(make_cfg, tmp_path):
    dvr = await FakeDVR().start()
    cfg = make_cfg(dvr.port)
    cfg.data_dir.mkdir(parents=True)
    store = Store(cfg.data_dir / "z.db")
    bus = EventBus()
    q = bus.subscribe()

    class Notifier:
        calls = []

        def should_notify(self, camera):
            return True

        async def motion(self, camera, name, event_id):
            self.calls.append(camera)

    notifier = Notifier()
    listener = AlarmListener(cfg, store, bus, notifier)
    task = asyncio.create_task(listener.run())
    try:
        for _ in range(100):
            if listener.connected:
                break
            await asyncio.sleep(0.02)
        assert listener.connected
        await dvr.push_alarm(1, "Start")
        await dvr.push_alarm(1, "Start")  # repetido: ignorado
        await dvr.push_alarm(1, "Stop")
        events = [await asyncio.wait_for(q.get(), 2) for _ in range(2)]
        assert [(e["camera"], e["status"]) for e in events] == [(2, "Start"), (2, "Stop")]
        await asyncio.sleep(0.05)
        assert notifier.calls == [2]
        assert [e["camera"] for e in store.events()] == [2]
        # o keepalive (1s no DVR simulado) mantém a conexão de pé
        await asyncio.sleep(1.5)
        assert listener.connected
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await dvr.stop()
