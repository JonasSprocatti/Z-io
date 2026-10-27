"""Gera a configuração do go2rtc, que repassa o RTSP do DVR ao navegador sem recodificar."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import Config


def build(cfg: Config) -> dict:
    streams: dict[str, list[str]] = {}
    for cam in cfg.cameras:
        streams[cam.main] = [cfg.rtsp_url(cam.number, sub=False)]
        streams[cam.sub] = [cfg.rtsp_url(cam.number, sub=True)]
    return {
        # Só escuta no próprio Pi; o acesso de fora passa pelo Zóio, que confere quem é.
        "api": {"listen": "127.0.0.1:1984", "origin": ""},
        "rtsp": {"listen": ""},
        "webrtc": {"listen": ""},
        "srtp": {"listen": ""},
        "rtmp": {"listen": ""},
        "log": {"level": "warn"},
        "streams": streams,
    }


def write(cfg: Config, path: str | Path) -> Path:
    """Grava o arquivo (JSON também é YAML válido) só com permissão do dono."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(build(cfg), f, indent=2, ensure_ascii=False)
    tmp.replace(path)
    return path
