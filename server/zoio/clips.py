"""Gravações: busca no DVR, download e conversão para MP4 (sem recodificar).

Downloads são lentos (a rede do DVR entrega perto de 1 MB/s), então cada
pedido vira uma tarefa com progresso, e o resultado fica num cache em disco.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import dvrip
from .config import Config
from .xmstream import Demuxer

log = logging.getLogger(__name__)


def file_size_bytes(entry: dict) -> int | None:
    """FileLength vem em hexadecimal, em KB."""
    try:
        return int(str(entry.get("FileLength", "")), 16) * 1024
    except ValueError:
        return None


@dataclass
class Job:
    id: str
    camera: int
    filename: str
    begin: str
    end: str
    total: int | None = None
    received: int = 0
    state: str = "na fila"  # na fila, baixando, convertendo, pronto, erro
    error: str | None = None
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict:
        d = asdict(self)
        d["url"] = f"/api/clips/{self.id}/video.mp4" if self.state == "pronto" else None
        return d


class Clips:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.data_dir / "clips"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self._download_slot = asyncio.Semaphore(1)  # um download por vez poupa o DVR e o Wi-Fi
        self._query_slot = asyncio.Semaphore(2)
        self._tasks: set[asyncio.Task] = set()

    def _connection(self) -> dvrip.Connection:
        return dvrip.Connection(self.cfg.dvr_host, self.cfg.dvr_port, self.cfg.dvr_user, self.cfg.dvr_pass)

    async def search(self, camera: int, begin: str, end: str) -> list[dict]:
        async with self._query_slot, self._connection() as conn:
            files = await conn.query_files(camera - 1, begin, end)
        return [
            {
                "camera": camera,
                "file": f["FileName"],
                "begin": f["BeginTime"],
                "end": f["EndTime"],
                "size": file_size_bytes(f),
                # arquivos de alarme/movimento costumam ter [M] ou _M no nome
                "motion": any(tag in f["FileName"] for tag in ("[M]", "_M", "[A]")),
            }
            for f in files
        ]

    def path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.mp4"

    def request(self, camera: int, filename: str, begin: str, end: str, size: int | None) -> Job:
        job_id = hashlib.sha1(f"{camera}|{filename}|{begin}|{end}".encode()).hexdigest()[:16]
        job = self.jobs.get(job_id)
        if job and job.state != "erro":
            return job
        job = Job(job_id, camera, filename, begin, end, total=size)
        self.jobs[job_id] = job
        if self.path(job_id).exists():
            job.state = "pronto"
            self.path(job_id).touch()
        else:
            task = asyncio.create_task(self._run(job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: Job) -> None:
        raw = self.dir / f"{job.id}.es"
        out = self.path(job.id)
        try:
            async with self._download_slot:
                job.state = "baixando"
                demux = Demuxer()
                with raw.open("wb") as f:
                    async with self._connection() as conn:
                        async for chunk in conn.download(job.filename, job.begin, job.end):
                            job.received += len(chunk)
                            f.write(demux.feed(chunk))
                info = demux.info
                if info.video_frames == 0:
                    raise RuntimeError("o DVR não enviou vídeo para esse trecho")
                job.state = "convertendo"
                await remux(raw, out, info.codec or "hevc", info.fps or 15, self.cfg.video_aspect)
                job.state = "pronto"
                self.prune()
        except Exception as exc:
            log.warning("falha no clipe %s: %s", job.filename, exc)
            job.state, job.error = "erro", str(exc) or exc.__class__.__name__
            out.unlink(missing_ok=True)
        finally:
            raw.unlink(missing_ok=True)

    def prune(self) -> None:
        files = sorted(self.dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        limit = self.cfg.cache_max_mb * 1024 * 1024
        while files and total > limit:
            victim = files.pop(0)
            total -= victim.stat().st_size
            victim.unlink(missing_ok=True)
            self.jobs.pop(victim.stem, None)


async def remux(src: Path, dst: Path, codec: str, fps: int, aspect: str | None = None) -> None:
    fmt = "hevc" if codec == "hevc" else "h264"
    tmp = dst.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-fflags", "+genpts", "-r", str(fps), "-f", fmt, "-i", str(src), "-c", "copy"]
    if fmt == "hevc":
        cmd += ["-tag:v", "hvc1"]  # exigido pelo Safari/iOS para tocar H.265
    if aspect:
        cmd += ["-aspect", aspect]  # só marca no MP4 como mostrar; o VLC e o celular respeitam
    cmd += ["-movflags", "+faststart", str(tmp)]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg falhou: {err.decode(errors='replace')[-300:]}")
    tmp.replace(dst)
