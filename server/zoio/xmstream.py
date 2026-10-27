"""Leitura do formato de mídia privado da Xiongmai (gravações baixadas por DVRIP).

O fluxo é uma sequência de quadros, cada um com um cabeçalho que começa
por 00 00 01 + tipo:

    FC / FE  quadro I de vídeo / JPEG (16 bytes: mídia, fps, largura/8,
             altura/8, data/hora, tamanho)
    FD       quadro P de vídeo (8 bytes: tamanho)
    FA       áudio (8 bytes: mídia, taxa, tamanho u16)
    F9       informação extra (8 bytes: tipo, n, tamanho u16)

Depois do cabeçalho vem o quadro em Annex B (H.264/H.265), que pode ser
remuxado para MP4 pelo ffmpeg sem recodificar.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

KINDS = (0xF9, 0xFA, 0xFC, 0xFD, 0xFE)
CODECS = {1: "mpeg4", 2: "h264", 3: "hevc", 0x12: "h264", 0x13: "hevc"}


@dataclass
class StreamInfo:
    codec: str | None = None
    fps: int | None = None
    width: int | None = None
    height: int | None = None
    video_frames: int = 0
    audio_frames: int = 0
    skipped_bytes: int = 0


class Demuxer:
    """Recebe pedaços arbitrários e devolve só os bytes de vídeo."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.info = StreamInfo()

    def feed(self, chunk: bytes) -> bytes:
        self.buf.extend(chunk)
        out = bytearray()
        buf = self.buf
        pos = 0
        while True:
            if len(buf) - pos < 8:
                break
            if buf[pos : pos + 3] != b"\x00\x00\x01" or buf[pos + 3] not in KINDS:
                nxt = self._resync(pos + 1)
                if nxt is None:
                    keep = max(pos, len(buf) - 3)
                    self.info.skipped_bytes += keep - pos
                    pos = keep
                    break
                self.info.skipped_bytes += nxt - pos
                pos = nxt
                continue
            kind = buf[pos + 3]
            if kind in (0xFC, 0xFE):
                if len(buf) - pos < 16:
                    break
                media, fps, w, h, _dt, size = struct.unpack_from("<BBBBII", buf, pos + 4)
                hdr = 16
            elif kind == 0xFD:
                (size,) = struct.unpack_from("<I", buf, pos + 4)
                hdr = 8
                media = None
            else:
                _m, _n, size = struct.unpack_from("<BBH", buf, pos + 4)
                hdr = 8
                media = None
            if len(buf) - pos < hdr + size:
                break
            payload = buf[pos + hdr : pos + hdr + size]
            if kind == 0xFC:
                self.info.codec = CODECS.get(media, self.info.codec)
                self.info.fps = fps or self.info.fps
                self.info.width, self.info.height = w * 8, h * 8
            if kind in (0xFC, 0xFD):
                out += payload
                self.info.video_frames += 1
            elif kind == 0xFA:
                self.info.audio_frames += 1
            pos += hdr + size
        del buf[:pos]
        return bytes(out)

    def _resync(self, start: int) -> int | None:
        """Posição do próximo cabeçalho conhecido, ou None se ainda não chegou."""
        buf = self.buf
        while True:
            nxt = buf.find(b"\x00\x00\x01", start)
            if nxt == -1 or nxt + 3 >= len(buf):
                return None
            if buf[nxt + 3] in KINDS:
                return nxt
            start = nxt + 1
