import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from zoio.config import Camera, Config  # noqa: E402


@pytest.fixture
def make_cfg(tmp_path):
    def make(port: int = 1, **kw) -> Config:
        return Config(
            dvr_host="127.0.0.1",
            dvr_user="admin",
            dvr_pass="segredo",
            dvr_port=port,
            cameras=[Camera(i, f"Câmera {i}") for i in range(1, 5)],
            data_dir=tmp_path / "data",
            **kw,
        )

    return make


@pytest.fixture(scope="session")
def hevc_es(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("media") / "t.hevc"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15", "-t", "2", "-c:v", "libx265", "-x265-params", "log-level=none", "-f", "hevc", str(out)],
        check=True,
    )
    return out.read_bytes()
