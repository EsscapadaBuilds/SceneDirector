"""Bridge to a headless Blender install.

Runs `blender -b --factory-startup --python runtime.py -- payload.json` and
parses the JSON result the runtime prints. No Blender addons or MCP servers needed.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import detect_blender
from ..state import Project

RUNTIME = Path(__file__).resolve().parent / "runtime.py"
MARKER = "SCENEDIRECTOR_RESULT:"


class BlenderError(RuntimeError):
    pass


@dataclass
class BlenderResult:
    ok: bool
    data: dict[str, Any]
    stdout: str
    stderr: str
    duration: float

    def summary(self, max_chars: int = 6000) -> str:
        payload = dict(self.data)
        text = json.dumps(payload, indent=1)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return text


class Bridge:
    def __init__(self, project: Project, blender: Path | None = None):
        self.project = project
        self.blender = blender or detect_blender()
        self._version: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.blender and Path(self.blender).exists())

    def _require(self) -> Path:
        if not self.available:
            raise BlenderError(
                "Blender was not found. Run `python sd.py setup` to download a portable copy, "
                "install it from https://www.blender.org/download/, or set BLENDER_PATH in .env."
            )
        return Path(self.blender)

    # ------------------------------------------------------------------- runner
    def run(self, payload: dict, timeout: float | None = 600) -> BlenderResult:
        blender = self._require()
        payload = dict(payload)
        payload.setdefault("blend", str(self.project.blend_path))

        scripts = self.project.root / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        payload_path = scripts / f"payload-{int(time.time() * 1000)}.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")

        cmd = [
            str(blender),
            "-b",
            "--factory-startup",
            "--python",
            str(RUNTIME),
            "--",
            str(payload_path),
        ]

        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=str(self.project.root),
            )
        except subprocess.TimeoutExpired as exc:
            raise BlenderError(
                f"Blender timed out after {timeout}s. Try a smaller resolution or check the scene."
            ) from exc
        finally:
            payload_path.unlink(missing_ok=True)

        duration = time.time() - started
        stdout, stderr = proc.stdout or "", proc.stderr or ""
        data: dict[str, Any] = {}
        for line in stdout.splitlines():
            if line.startswith(MARKER):
                try:
                    data = json.loads(line[len(MARKER):])
                except json.JSONDecodeError:
                    data = {"ok": False, "error": "Could not parse Blender result JSON"}

        if not data:
            tail = "\n".join((stdout + "\n" + stderr).splitlines()[-40:])
            raise BlenderError(
                f"Blender produced no result (exit code {proc.returncode}).\n{tail}"
            )

        return BlenderResult(
            ok=bool(data.get("ok")),
            data=data,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
        )

    # --------------------------------------------------------------- operations
    def probe(self) -> dict:
        result = self.run({"mode": "probe"}, timeout=120)
        if result.ok:
            self._version = result.data.get("version")
        return {"ok": result.ok, "version": self._version,
                "blender": str(self.blender), "error": result.data.get("error")}

    def python(self, code: str, save: bool = True, timeout: float = 600) -> BlenderResult:
        return self.run({"mode": "python", "code": code, "save": save}, timeout=timeout)

    def analyze(self, path: Path, target_height: float | None = None) -> dict:
        """Inspect a model in a throwaway Blender session (never touches scene.blend)."""
        temp = self.project.root / "scripts" / "analyze.blend"
        temp.unlink(missing_ok=True)
        try:
            result = self.run({
                "mode": "analyze",
                "path": str(path),
                "target_height": target_height,
                "blend": str(temp),
                "save": False,
            }, timeout=300)
        finally:
            temp.unlink(missing_ok=True)
        if not result.ok:
            raise BlenderError(
                f"Model analysis failed: {result.data.get('error')}"
                f"\n{result.data.get('traceback', '')}"
            )
        return result.data.get("analysis") or {}

    def screenshot(
        self,
        path: Path,
        camera: str | None = None,
        resolution: tuple[int, int] = (960, 540),
        engine: str = "BLENDER_WORKBENCH",
        samples: int = 16,
        frame: int | None = None,
    ) -> Path:
        result = self.run({
            "mode": "screenshot",
            "path": str(path),
            "camera": camera,
            "resolution": list(resolution),
            "engine": engine,
            "samples": samples,
            "frame": frame,
        }, timeout=300)
        if not result.ok:
            raise BlenderError(f"Screenshot failed: {result.data.get('error')}")
        return Path(result.data.get("path", path))

    def render(
        self,
        path: Path,
        camera: str | None = None,
        engine: str | None = None,
        samples: int = 64,
        resolution: tuple[int, int] = (1280, 720),
        frame: int | None = None,
        animation: bool = False,
        frame_start: int | None = None,
        frame_end: int | None = None,
        fps: int | None = None,
        timeout: float | None = None,
    ) -> Path:
        result = self.run({
            "mode": "render",
            "path": str(path),
            "camera": camera,
            "engine": engine,
            "samples": samples,
            "resolution": list(resolution),
            "frame": frame,
            "animation": animation,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "fps": fps,
        }, timeout=timeout)
        if not result.ok:
            raise BlenderError(f"Render failed: {result.data.get('error')}")
        return Path(result.data.get("path", path))


def open_in_viewer(path: str | Path) -> None:
    """Open a file with the OS default application (GUI only)."""
    p = str(path)
    try:
        if os.name == "nt":
            os.startfile(p)  # type: ignore[attr-defined]
        elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
            subprocess.Popen(["open", p])
        else:
            subprocess.Popen(["xdg-open", p])
    except Exception:
        pass
