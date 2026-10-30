"""Configuração lida de variáveis de ambiente e de um arquivo .env opcional."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .esphome import EspHomeError, decode_key

DEFAULT_ENV_FILE = "/etc/zoio/zoio.env"


def read_env_file(path: str | Path) -> dict[str, str]:
    """Lê linhas KEY=VALOR (aceita `export`, aspas e comentários)."""
    values: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return values
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, raw = line.partition("=")
        values[key.strip()] = _env_value(raw.strip())
    return values


def _env_value(raw: str) -> str:
    """Valor entre aspas segue as regras do shell. Sem aspas, vale o resto da
    linha (com espaços, como `Câmera 1,Câmera 2`), até um ` #` de comentário."""
    if raw[:1] in ("'", '"'):
        try:
            parts = shlex.split(raw, comments=True)
            return parts[0] if parts else ""
        except ValueError:
            pass
    return re.split(r"\s+#", raw, maxsplit=1)[0].strip()


@dataclass
class Camera:
    number: int  # 1..N, como aparece no DVR e na URL RTSP
    name: str
    hidden: bool = False  # escondida pelo app (canal sem câmera, por exemplo)
    default_name: str = ""  # nome do zoio.env, que volta quando o nome do app é apagado

    def __post_init__(self):
        self.default_name = self.default_name or self.name

    @property
    def main(self) -> str:
        return f"cam{self.number}"

    @property
    def sub(self) -> str:
        return f"cam{self.number}_sub"


@dataclass
class Config:
    dvr_host: str
    dvr_user: str
    dvr_pass: str
    dvr_port: int = 34567
    rtsp_port: int = 554
    cameras: list[Camera] = field(default_factory=list)
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    go2rtc_url: str = "http://127.0.0.1:1984"
    data_dir: Path = Path("/var/lib/zoio")
    allowed_users: set[str] = field(default_factory=set)
    device_approval: bool = True
    session_days: int = 30
    alerts: bool = True
    notify_channels: set[int] = field(default_factory=set)  # vazio = todas
    notify_cooldown: int = 300
    cache_max_mb: int = 2048
    # Proporção da imagem na tela. O DVR manda o sub (352x240) e os canais em
    # 1080N (960x1080) "espremidos", e a câmera é 16:9. None = como chega.
    video_aspect: str | None = "16:9"
    push_contact: str = "mailto:zoio@localhost"
    # Porteiro: placa ESPHome do portão (vazio = sem portaria)
    gate_host: str | None = None
    gate_port: int = 6053
    gate_audio_port: int = 6054  # UDP da conversa (componente zoio_conversa)
    gate_key: str | None = None
    gate_name: str = "zoio-porteiro"
    gate_camera: int | None = None
    garage_cameras: list[int | None] = field(default_factory=list)  # na ordem das garagens
    garage_names: list[str] = field(default_factory=list)
    bell_names: list[str] = field(default_factory=list)  # uma campainha por casa, na ordem
    garage_travel: int = 30  # segundos que o motor leva para abrir ou fechar
    gate_error: str | None = None  # configuração do porteiro com problema

    @property
    def gate_enabled(self) -> bool:
        """A portaria só funciona com a aprovação de aparelhos ligada."""
        return bool(self.gate_host and self.gate_key and self.device_approval)

    def garage_name(self, n: int) -> str:
        return self.garage_names[n - 1] if n - 1 < len(self.garage_names) and self.garage_names[n - 1] else f"Garagem {n}"

    def bell_name(self, n: int) -> str:
        return self.bell_names[n - 1] if n - 1 < len(self.bell_names) and self.bell_names[n - 1] else f"Casa {n}"

    def garage_camera(self, n: int) -> int | None:
        return self.garage_cameras[n - 1] if n - 1 < len(self.garage_cameras) else None

    @property
    def rtsp_base(self) -> str:
        return f"rtsp://{self.dvr_host}:{self.rtsp_port}"

    def rtsp_url(self, number: int, sub: bool) -> str:
        return (
            f"{self.rtsp_base}/user={self.dvr_user}&password={self.dvr_pass}"
            f"&channel={number}&stream={1 if sub else 0}.sdp?real_stream"
        )

    def camera(self, number: int) -> Camera | None:
        return next((c for c in self.cameras if c.number == number), None)


def _flag(raw: str) -> bool:
    return raw.strip().lower() not in ("0", "false", "no", "nao", "não", "off")


def _int_set(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def _aspect(raw: str) -> str | None:
    raw = raw.strip().lower()
    if raw == "original":
        return None
    w, sep, h = raw.partition(":")
    if not (sep and w.isdigit() and h.isdigit() and int(w) and int(h)):
        raise SystemExit(f"ZOIO_VIDEO_ASPECT inválido: {raw!r} (use 16:9, 4:3 ou original)")
    return f"{int(w)}:{int(h)}"


def load(env_file: str | None = None) -> Config:
    env = read_env_file(env_file or os.environ.get("ZOIO_ENV_FILE", DEFAULT_ENV_FILE))
    env.update({k: v for k, v in os.environ.items() if k.startswith(("DVR_", "ZOIO_", "GO2RTC_"))})

    missing = [k for k in ("DVR_HOST", "DVR_USER", "DVR_PASS") if not env.get(k)]
    if missing:
        raise SystemExit(f"Configuração incompleta, faltam: {', '.join(missing)}")

    count = int(env.get("ZOIO_CHANNELS", "8"))
    names = [n.strip() for n in env.get("ZOIO_CHANNEL_NAMES", "").split(",")]
    hidden = _int_set(env.get("ZOIO_HIDE_CHANNELS", ""))
    cameras = [
        Camera(i, names[i - 1] if i - 1 < len(names) and names[i - 1] else f"Câmera {i}")
        for i in range(1, count + 1)
        if i not in hidden
    ]
    host, _, port = env.get("ZOIO_LISTEN", "127.0.0.1:8080").rpartition(":")
    gate_key, gate_error = env.get("ZOIO_GATE_KEY") or None, None
    if gate_key:
        try:
            decode_key(gate_key)
        except EspHomeError as exc:
            gate_key, gate_error = None, f"ZOIO_GATE_KEY: {exc}"
    numbers = {c.number for c in cameras}

    def camera_ref(raw: str) -> int | None:  # câmera que não existe é ignorada
        raw = raw.strip()
        return int(raw) if raw.isdigit() and int(raw) in numbers else None

    garage_cameras = [camera_ref(x) for x in env["ZOIO_GARAGE_CAMERAS"].split(",")] if env.get("ZOIO_GARAGE_CAMERAS", "").strip() else []
    return Config(
        dvr_host=env["DVR_HOST"],
        dvr_user=env["DVR_USER"],
        dvr_pass=env["DVR_PASS"],
        dvr_port=int(env.get("DVR_PORT", "34567")),
        rtsp_port=int(env.get("DVR_RTSP_PORT", "554")),
        cameras=cameras,
        listen_host=host or "127.0.0.1",
        listen_port=int(port),
        go2rtc_url=env.get("GO2RTC_URL", "http://127.0.0.1:1984").rstrip("/"),
        data_dir=Path(env.get("ZOIO_DATA_DIR", "/var/lib/zoio")),
        allowed_users={u.strip().lower() for u in env.get("ZOIO_ALLOWED_USERS", "").split(",") if u.strip()},
        device_approval=_flag(env.get("ZOIO_DEVICE_APPROVAL", "1")),
        session_days=int(env.get("ZOIO_SESSION_DAYS", "30")),
        alerts=_flag(env.get("ZOIO_ALERTS", "1")),
        notify_channels=_int_set(env.get("ZOIO_NOTIFY_CHANNELS", "")),
        notify_cooldown=int(env.get("ZOIO_NOTIFY_COOLDOWN", "300")),
        cache_max_mb=int(env.get("ZOIO_CACHE_MAX_MB", "2048")),
        video_aspect=_aspect(env.get("ZOIO_VIDEO_ASPECT") or "16:9"),
        push_contact=env.get("ZOIO_PUSH_CONTACT", "mailto:zoio@localhost"),
        gate_host=env.get("ZOIO_GATE_HOST") or None,
        gate_port=int(env.get("ZOIO_GATE_PORT", "6053")),
        gate_audio_port=int(env.get("ZOIO_GATE_AUDIO_PORT", "6054")),
        gate_key=gate_key,
        gate_name=env.get("ZOIO_GATE_NAME", "zoio-porteiro"),
        gate_camera=camera_ref(env.get("ZOIO_GATE_CAMERA", "")),
        garage_cameras=garage_cameras,
        garage_names=[n.strip() for n in env.get("ZOIO_GARAGE_NAMES", "").split(",")] if env.get("ZOIO_GARAGE_NAMES", "").strip() else [],
        bell_names=[n.strip() for n in env.get("ZOIO_BELL_NAMES", "").split(",")] if env.get("ZOIO_BELL_NAMES", "").strip() else [],
        garage_travel=int(env.get("ZOIO_GARAGE_TRAVEL", "30")),
        gate_error=gate_error,
    )
