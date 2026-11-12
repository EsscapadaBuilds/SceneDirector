"""Run one SceneDirector tool from the command line and print its JSON result.

This is how an outside director drives the pipeline without the built-in LLM
loop: Claude (through the `scenedirector` skill), a script, or you by hand.
It calls the same Toolbox the agent uses, on the same projects.

    python sd.py tools                          list tools and their arguments
    python sd.py projects                       list projects
    python sd.py new <name>                     create a project
    python sd.py setup                          install packages, Blender, Claude skill
    python sd.py <project> <tool> [key=value ...] [--json '{"key": "value"}']

Values are parsed as JSON when possible (numbers, true/false, lists), otherwise
taken as text. `key=@file` reads the value from a file, which is the reliable
way to pass Blender code or a document:

    python sd.py my_film blender_run code=@build.py note="block out the room"
    python sd.py my_film blender_screenshot
    python sd.py my_film generate_references kind=scene count=2 prompt="foggy forest at dawn"

Exit code is 0 when the tool reports ok, 1 otherwise.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .agent.tools import TOOLS, Toolbox
from .blender import Bridge
from .config import PROJECTS_DIR, Runtime, get_config
from .setup import SetupError, ensure_blender
from .state import Project, list_projects, slugify

ALIASES = {"status": "project_status", "screenshot": "blender_screenshot",
           "render": "blender_render", "run": "blender_run"}


def emit(payload: dict) -> int:
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return 0 if payload.get("ok", True) else 1


def describe_tools() -> dict:
    tools = []
    for tool in TOOLS:
        fn = tool["function"]
        params = fn["parameters"]
        required = set(params.get("required", []))
        args = {}
        for name, spec in params.get("properties", {}).items():
            text = spec.get("type", "any")
            if spec.get("enum"):
                text = " | ".join(spec["enum"])
            if "default" in spec:
                text += f" (default {spec['default']})"
            if spec.get("description"):
                text += f" - {spec['description']}"
            args[name + ("" if name in required else "?")] = text
        tools.append({"tool": fn["name"], "does": fn["description"], "args": args})
    return {"ok": True, "tools": tools}


def parse_args(raw: list[str]) -> dict:
    args: dict = {}
    items = iter(raw)
    for item in items:
        if item == "--json":
            args.update(json.loads(next(items, "{}")))
            continue
        if "=" not in item:
            raise ValueError(f"expected key=value, got: {item}")
        key, value = item.split("=", 1)
        if value.startswith("@"):
            args[key] = Path(value[1:]).read_text(encoding="utf-8")
            continue
        try:
            args[key] = json.loads(value)
        except json.JSONDecodeError:
            args[key] = value
    return args


def _last_number(folder: Path, prefixes: tuple[str, ...]) -> int:
    """Highest NNN among files like `verify-NNN.png`, so reruns never overwrite."""
    best = 0
    if folder.is_dir():
        for path in folder.iterdir():
            match = re.match(rf"(?:{'|'.join(prefixes)})-(\d+)\.", path.name)
            if match:
                best = max(best, int(match.group(1)))
    return best


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    if argv[0] == "tools":
        return emit(describe_tools())
    if argv[0] == "projects":
        return emit({"ok": True, "projects": list_projects(), "projects_dir": str(PROJECTS_DIR)})
    if argv[0] == "new":
        if len(argv) < 2 or not re.search(r"[a-zA-Z0-9]", argv[1]):
            return emit({"ok": False, "error": "usage: new <name>"})
        name = slugify(argv[1])
        existed = (PROJECTS_DIR / name).is_dir()
        project = Project(name, create=True)
        project.save()
        return emit({"ok": True, "project": name, "created": not existed,
                     "root": str(project.root)})

    if len(argv) < 2:
        return emit({"ok": False, "error": "usage: <project> <tool> [key=value ...]"})
    name = slugify(argv[0])
    if not (PROJECTS_DIR / name).is_dir():
        return emit({"ok": False, "error": f"No project named '{name}'. Create it with: new {name}",
                     "projects": list_projects()})
    tool = ALIASES.get(argv[1], argv[1])
    try:
        args = parse_args(argv[2:])
    except (ValueError, OSError) as exc:
        return emit({"ok": False, "error": f"Bad arguments: {exc}"})

    cfg = get_config()
    project = Project(name, create=True)
    blender = Runtime(cfg).blender
    needs_blender = tool.startswith("blender_") or tool == "analyze_model"
    if blender is None and needs_blender \
            and os.environ.get("SCENEDIRECTOR_NO_AUTO_INSTALL") != "1":
        # First Blender call on a machine without Blender: fetch a portable copy.
        try:
            blender = ensure_blender()
        except SetupError as exc:
            return emit({"ok": False, "error": str(exc)})
    bridge = Bridge(project, blender)
    box = Toolbox(project, bridge, cfg,
                  progress=lambda message: print(str(message).strip(), file=sys.stderr, flush=True))
    # Each call is a fresh process, so continue numbering from the files on disk.
    box._shot_counter = _last_number(project.root / "renders", ("verify",))
    box._render_counter = max(_last_number(project.root / "renders", ("preview",)),
                              _last_number(project.root / "output", ("final", "animation")))
    return emit(box.execute(tool, args))


if __name__ == "__main__":
    raise SystemExit(main())
