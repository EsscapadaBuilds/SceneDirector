"""Environment-driven configuration.

All values can be set in a `.env` file next to `cli.py`, or in the environment.
The README lists every setting.
"""
from __future__ import annotations

import glob as globmod
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"


def load_env(path: Path | None = None) -> None:
    """Minimal .env loader (no dependency)."""
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    # LLM (OpenAI-compatible endpoint: Ollama, Groq, Gemini compat, OpenRouter, ...)
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5vl:7b"
    llm_timeout: int = 180
    llm_max_tokens: int = 800
    llm_max_input_tokens: int = 0
    llm_vision: bool = True

    # Blender
    blender_path: str | None = None

    # Concept image generation
    image_provider: str = "pollinations"  # pollinations | none
    pollinations_model: str = "flux"
    pollinations_api_key: str = ""

    # AI image-to-3D (optional; both are paid/credit services)
    ai3d_provider: str = "auto"  # auto | tripo | meshy | manual
    tripo_api_key: str = ""
    meshy_api_key: str = ""
    tripo_model: str = ""
    ai3d_timeout: int = 900

    # Rendering defaults
    preview_engine: str = "auto"
    final_engine: str = "CYCLES"

    project_name: str = "untitled"

    def model_label(self) -> str:
        return f"{self.llm_model} @ {self.llm_base_url}"


def get_config() -> Config:
    load_env()
    return Config(
        llm_base_url=os.environ.get("LLM_BASE_URL", Config.llm_base_url),
        llm_api_key=os.environ.get("LLM_API_KEY", Config.llm_api_key),
        llm_model=os.environ.get("LLM_MODEL", Config.llm_model),
        llm_timeout=int(os.environ.get("LLM_TIMEOUT", "180")),
        llm_max_tokens=int(os.environ.get("LLM_MAX_TOKENS", "800")),
        llm_max_input_tokens=int(os.environ.get("LLM_MAX_INPUT_TOKENS", "0")),
        llm_vision=os.environ.get("VISION", "1").strip().lower() not in {"0", "false", "no"},
        blender_path=os.environ.get("BLENDER_PATH") or None,
        image_provider=os.environ.get("IMAGE_PROVIDER", "pollinations"),
        pollinations_model=os.environ.get("POLLINATIONS_MODEL", "flux"),
        pollinations_api_key=os.environ.get("POLLINATIONS_API_KEY", ""),
        ai3d_provider=os.environ.get("AI3D_PROVIDER", "auto"),
        tripo_api_key=os.environ.get("TRIPO_API_KEY", ""),
        meshy_api_key=os.environ.get("MESHY_API_KEY", ""),
        tripo_model=os.environ.get("TRIPO_MODEL", ""),
        ai3d_timeout=int(os.environ.get("AI3D_TIMEOUT", "900")),
        preview_engine=os.environ.get("PREVIEW_ENGINE", "auto"),
        final_engine=os.environ.get("FINAL_ENGINE", "CYCLES"),
    )


def detect_blender(explicit: str | None = None) -> Path | None:
    """Find a Blender executable across common install locations."""
    candidates: list[Path] = []

    if explicit:
        candidates.append(Path(explicit))

    env_path = os.environ.get("BLENDER_PATH")
    if env_path:
        candidates.append(Path(env_path))

    which = shutil.which("blender")
    if which:
        candidates.append(Path(which))

    home = Path.home()
    system_drive = Path(os.environ.get("SystemDrive", "C:"))
    patterns = [
        str(ROOT / "tools" / "blender*" / "blender.exe"),
        str(ROOT / "tools" / "blender" / "blender.exe"),
        str(ROOT / "tools" / "blender*" / "blender"),
        str(system_drive / "Program Files" / "Blender Foundation" / "Blender *" / "blender.exe"),
        str(system_drive / "Program Files" / "Steam" / "steamapps" / "common" / "Blender" / "blender.exe"),
        str(home / "AppData" / "Local" / "Programs" / "Blender Foundation" / "Blender *" / "blender.exe"),
        str(home / "scoop" / "apps" / "blender" / "current" / "blender.exe"),
        "/Applications/Blender.app/Contents/MacOS/Blender",
        "/usr/bin/blender",
        "/usr/local/bin/blender",
        "/snap/bin/blender",
    ]
    for pattern in patterns:
        if any(ch in pattern for ch in "*?"):
            candidates.extend(Path(p) for p in globmod.glob(pattern))
        else:
            candidates.append(Path(pattern))

    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


@dataclass
class Runtime:
    cfg: Config
    blender: Path | None = field(default=None)

    def __post_init__(self) -> None:
        self.blender = detect_blender(self.cfg.blender_path)
