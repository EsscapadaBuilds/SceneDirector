"""One-step setup: Python packages, Blender, and the Claude skill.

    python sd.py setup                 everything below
    python sd.py setup --no-skill      skip installing the Claude skill
    python sd.py setup --no-blender    skip the Blender download

Blender: if none is found, a portable copy is downloaded from blender.org into
`tools/` (about 430 MB, no administrator rights, nothing installed system-wide)
and checked against the SHA-256 that blender.org publishes. Remove it by
deleting the folder.

This module uses only the standard library, so it runs before `requests` exists.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .config import ROOT, detect_blender, load_env

BLENDER_VERSION = os.environ.get("BLENDER_VERSION", "5.3.0")
SKILL_SOURCE = ROOT / ".claude" / "skills" / "scenedirector" / "SKILL.md"
ROOT_SENTENCE = "ROOT is the SceneDirector folder, the one that contains `sd.py` and"


class SetupError(RuntimeError):
    pass


def _say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _fetch(url: str, timeout: int = 60):
    request = urllib.request.Request(url, headers={"User-Agent": "SceneDirector-setup"})
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - fixed https host


def blender_archive() -> str | None:
    """File name of the portable build for this machine, or None if there is none."""
    machine = platform.machine().lower()
    arm = machine in {"arm64", "aarch64"}
    if sys.platform == "win32":
        return f"blender-{BLENDER_VERSION}-windows-{'arm64' if arm else 'x64'}.zip"
    if sys.platform.startswith("linux") and not arm:
        return f"blender-{BLENDER_VERSION}-linux-x64.tar.xz"
    return None  # macOS ships a disk image, handled separately


def ensure_blender(progress: Callable[[str], None] = _say,
                   tools: Path | None = None) -> Path:
    """Return a Blender executable, downloading a portable copy if none is found."""
    tools = tools or (ROOT / "tools")
    load_env()
    found = detect_blender()
    if found:
        return found

    name = blender_archive()
    if name is None:
        if sys.platform == "darwin" and shutil.which("brew"):
            progress("Blender not found. Installing it with Homebrew ...")
            subprocess.run(["brew", "install", "--cask", "blender"], check=False)
            found = detect_blender()
            if found:
                return found
        raise SetupError("Blender was not found and cannot be installed automatically on "
                         "this system. Install it from https://www.blender.org/download/ "
                         "or set BLENDER_PATH in .env.")

    series = ".".join(BLENDER_VERSION.split(".")[:2])
    base = f"https://download.blender.org/release/Blender{series}"
    tools.mkdir(parents=True, exist_ok=True)
    archive = tools / name

    try:
        with _fetch(f"{base}/blender-{BLENDER_VERSION}.sha256") as resp:
            sums = dict(reversed(line.split()) for line in resp.read().decode().splitlines()
                        if len(line.split()) == 2)
        expected = sums.get(name)
        if not expected:
            raise SetupError(f"blender.org lists no checksum for {name}")

        with _fetch(f"{base}/{name}") as resp, archive.open("wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            progress(f"Blender not found. Downloading Blender {BLENDER_VERSION} "
                     f"({total / 1e6:.0f} MB) from blender.org into {tools} ...")
            digest = hashlib.sha256()
            done, next_mark = 0, 10
            while chunk := resp.read(1024 * 1024):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if total and done * 100 // total >= next_mark:
                    progress(f"  {next_mark}%")
                    next_mark += 10
        if digest.hexdigest() != expected:
            raise SetupError("The Blender download does not match blender.org's checksum. "
                             "Nothing was installed; try again.")

        progress("Unpacking ...")
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(tools)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(tools, filter="tar")
    except OSError as exc:
        raise SetupError(f"Could not download Blender: {exc}") from exc
    finally:
        archive.unlink(missing_ok=True)

    found = detect_blender()
    if not found:
        raise SetupError(f"Blender was unpacked into {tools} but could not be located.")
    progress(f"Blender ready: {found}")
    return found


def ensure_requests(progress: Callable[[str], None] = _say) -> None:
    try:
        import requests  # noqa: F401
        return
    except ImportError:
        pass
    progress("Installing Python packages (requests) ...")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                             str(ROOT / "requirements.txt")], check=False)
    if result.returncode:
        raise SetupError("pip could not install the Python packages. "
                         "Run: python -m pip install -r requirements.txt")


def install_skill(progress: Callable[[str], None] = _say) -> Path:
    """Copy the Claude skill to ~/.claude/skills with this folder's path filled in."""
    text = SKILL_SOURCE.read_text(encoding="utf-8")
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.startswith(ROOT_SENTENCE)), None)
    if start is not None:
        end = next(i for i in range(start, len(lines)) if "is one command" in lines[i])
        lines[start:end + 1] = [
            f"SceneDirector is installed at `{ROOT}`. Call that folder ROOT. Every",
            "action is one command, which prints a JSON result:",
        ]
    target = Path.home() / ".claude" / "skills" / "scenedirector" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    progress(f"Claude skill installed: {target}")
    return target


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    if sys.version_info < (3, 10):
        _say(f"Python 3.10 or newer is required (this is {platform.python_version()}).")
        return 1
    failed = False
    steps: list[tuple[str, Callable[[], object]]] = [("Python packages", ensure_requests)]
    if "--no-blender" not in argv:
        steps.append(("Blender", ensure_blender))
    if "--no-skill" not in argv:
        steps.append(("Claude skill", install_skill))
    for label, step in steps:
        try:
            step()
            _say(f"ok    {label}")
        except SetupError as exc:
            failed = True
            _say(f"FAIL  {label}: {exc}")
    if shutil.which("ffmpeg"):
        _say("ok    FFmpeg")
    else:
        _say("note  FFmpeg is not on PATH. Stills work; MP4 animations need it "
             "(Windows: winget install Gyan.FFmpeg).")
    if not (ROOT / ".env").exists():
        _say("note  No .env file yet. The Claude skill works without one; the built-in "
             "agent (CLI and desktop app) needs LLM settings, see the README.")
    _say("Setup finished." if not failed else "Setup finished with problems (see FAIL above).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
