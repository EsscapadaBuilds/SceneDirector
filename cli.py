"""SceneDirector CLI.

    python cli.py                 # pick/create a project interactively
    python cli.py my_project      # open or create a project
    python cli.py --new demo      # create a fresh project
    python cli.py --doctor        # environment diagnostics
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Enable ANSI colours in Windows terminals that need the hint.
if os.name == "nt":
    os.system("")

from directorc.agent import Agent
from directorc import assets as asset_lib
from directorc import ai3d
from directorc.blender import Bridge, BlenderError, open_in_viewer
from directorc.config import PROJECTS_DIR, Runtime, get_config
from directorc.llm import LLM, LLMError
from directorc.state import Project, list_projects, slugify

BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

HELP = f"""
{BOLD}Commands{RESET}
  /help                     show this help
  /status                   project brief, stage and gates
  /refs [open]              list reference images (and open the folder)
  /assets [open]            list downloaded/imported assets
  /inbox [import]           list files in assets/inbox (or register them)
  /attach [kind] <path>     attach a reference image to your next message
                            kind: scene | character | style (default: style)
  /open <path>              open a file in the default application
  /approve <stage> [notes]  manually approve a gate
  /new <name>               create and switch to a new project
  /projects                 list projects
  /doctor                   check LLM, Blender, network
  /quit                     exit (progress is saved)

{BOLD}Flow{RESET}: describe your scene -> answer questions -> approve references ->
approve the shot plan -> watch it build -> approve preview -> get final render.
"""


def c(text: str, color: str = "") -> str:
    if color and sys.stdout.isatty():
        return f"{color}{text}{RESET}"
    return text


def split_args(raw: str) -> list[str]:
    """Split a command line, preserving Windows backslashes and quoted paths."""
    return [m.group(0).strip('"').strip("'")
            for m in re.finditer(r'"[^"]*"|\'[^\']*\'|\S+', raw)]


def pick_project(name: str | None, create: bool) -> Project:
    if name:
        project = Project(name, create=True)
    else:
        existing = list_projects()
        if existing:
            print(c("Projects:", BOLD))
            for i, item in enumerate(existing, 1):
                print(f"  {i}. {item}")
        default = existing[0] if existing else "untitled"
        raw = input(f"Project name {DIM}[{default}]{RESET}: ").strip()
        project = Project(raw or default, create=True)
    if create and name:
        print(c(f"created project '{project.name}'", GREEN))
    return project


def doctor(cfg, runtime: Runtime, project: Project | None) -> int:
    print(c("\nSceneDirector doctor", BOLD))
    print(f"  python          {sys.version.split()[0]}")
    print(f"  workspace       {Path.cwd()}")
    print(f"  projects dir    {PROJECTS_DIR}")

    print(f"  llm endpoint    {cfg.llm_base_url} ({cfg.llm_model})")
    print(f"  max output      {cfg.llm_max_tokens} tokens")
    print(f"  max input       {cfg.llm_max_input_tokens or 'no cap'} tokens")
    print(f"  vision          {'on' if cfg.llm_vision else 'off'}")
    try:
        llm = LLM(cfg)
        reply = llm.chat([{"role": "user", "content": "Reply with the single word: ok"}],
                         temperature=0)
        word = (reply.message.get("content") or "").strip().splitlines()[0][:40]
        print(c(f"  llm test        OK ({word})", GREEN))
    except LLMError as exc:
        print(c(f"  llm test        FAILED: {exc}", RED))

    print(f"  blender path    {runtime.blender or 'not found'}")
    if runtime.blender:
        try:
            bridge = Bridge(project, runtime.blender) if project else None
            if bridge:
                probe = bridge.probe()
                if probe.get("ok"):
                    print(c(f"  blender test    OK ({probe.get('version')})", GREEN))
                else:
                    print(c(f"  blender test    FAILED: {probe.get('error')}", RED))
        except BlenderError as exc:
            print(c(f"  blender test    FAILED: {exc}", RED))
    else:
        print(c("  blender test    install: https://www.blender.org/download/ "
                "or winget install BlenderFoundation.Blender", YELLOW))

    try:
        import requests
        requests.get("https://image.pollinations.ai", timeout=10)
        mode = "key" if cfg.pollinations_api_key else "free tier: 1 image / 45s"
        print(c(f"  image service   reachable ({mode})", GREEN))
    except Exception as exc:  # noqa: BLE001
        print(c(f"  image service   unreachable: {exc}", YELLOW))

    try:
        import requests
        r = requests.get("https://api.polyhaven.com/types", timeout=10)
        print(c(f"  polyhaven       reachable (HTTP {r.status_code})", GREEN))
    except Exception as exc:  # noqa: BLE001
        print(c(f"  polyhaven       unreachable: {exc}", YELLOW))

    configured = ai3d.available_providers(cfg)
    if configured:
        print(c(f"  image-to-3D     {', '.join(configured)} configured", GREEN))
    else:
        print(c("  image-to-3D     no API key (manual assets/inbox workflow)", YELLOW))

    if project:
        try:
            test = project.root / ".write-test"
            test.write_text("ok", encoding="utf-8")
            test.unlink()
            print(c(f"  project write   OK ({project.root})", GREEN))
        except OSError as exc:
            print(c(f"  project write   FAILED: {exc}", RED))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Conversational AI director for Blender")
    parser.add_argument("project", nargs="?", help="project name (created if missing)")
    parser.add_argument("--new", action="store_true", help="force create a new project")
    parser.add_argument("--doctor", action="store_true", help="diagnostics only")
    args = parser.parse_args()

    cfg = get_config()
    runtime = Runtime(cfg)

    if args.doctor:
        project = Project(args.project, create=True) if args.project else None
        return doctor(cfg, runtime, project)

    project = pick_project(args.project, create=args.new)

    blender = runtime.blender
    bridge = Bridge(project, blender)

    print(c("\nSceneDirector", BOLD) + c("  —  conversational Blender director", DIM))
    print(f"  project   {project.root}")
    print(f"  llm       {cfg.model_label()}")
    print(f"  blender   {blender or c('NOT FOUND (set BLENDER_PATH)', YELLOW)}")
    if not cfg.llm_vision:
        print(c("  warning: VISION=0 — the agent cannot see images, including its "
                "own screenshots.", YELLOW))

    history = project.load_messages()
    if history:
        print(c(f"  resumed   {len(history)} messages of history", DIM))

    def printer(text: str) -> None:
        print(f"\n{c('director', CYAN)}: {text}")

    def tool_printer(text: str) -> None:
        print(c(f"  · {text}", DIM))

    agent = Agent(project, bridge, cfg, printer=printer, tool_printer=tool_printer)
    pending_images: list[str] = []

    print(c("\nType /help for commands. Describe the scene or character you want.", DIM))

    while True:
        new_files = project.inbox_new()
        if new_files:
            print(c(f"\n+ {len(new_files)} new file(s) in assets/inbox:", CYAN))
            for item in new_files[:6]:
                print(f"    {item['name']}  ({item['size'] / 1e6:.1f} MB)")
            print(c("  say \"import it\" or run /inbox import", DIM))
            project.mark_inbox_seen()
        try:
            raw = input(c("\nyou> ", BOLD)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if not raw:
            continue
        if raw.startswith("/"):
            parts = split_args(raw)
            cmd = parts[0].lower()
            try:
                if cmd in {"/quit", "/exit", "/q"}:
                    print("bye")
                    break
                elif cmd == "/help":
                    print(HELP)
                elif cmd == "/status":
                    print(project.brief())
                elif cmd == "/refs":
                    refs = project.references()
                    if not refs:
                        print(c("no references yet", YELLOW))
                    for ref in refs:
                        mark = c("approved", GREEN) if ref.get("approved") else c("pending", YELLOW)
                        print(f"  [{mark}] {ref['kind']:9s} {ref['path']}")
                    if len(parts) > 1 and parts[1] == "open":
                        open_in_viewer(project.root / "references")
                elif cmd == "/assets":
                    assets = project.assets()
                    if not assets:
                        print(c("no assets yet", YELLOW))
                    for asset in assets:
                        print(f"  {asset['kind']:8s} {asset['name']:24s} {asset['path']}"
                              f"  [{asset['license']}]")
                    if len(parts) > 1 and parts[1] == "open":
                        open_in_viewer(project.root / "assets")
                elif cmd == "/inbox":
                    files = project.inbox_files()
                    if not files:
                        print(c("inbox is empty", YELLOW))
                    for item in files:
                        print(f"  {item['name']:40s} {item['size'] / 1e6:8.2f} MB")
                    if len(parts) > 1 and parts[1].lower() == "import":
                        registered = asset_lib.import_inbox(project)
                        project.mark_inbox_seen()
                        print(c(f"registered {len(registered)} file(s)", GREEN))
                    elif files:
                        print(c("  /inbox import to register them", DIM))
                elif cmd == "/attach":
                    if len(parts) < 2:
                        print(c("usage: /attach [scene|character|style] <path>", YELLOW))
                        continue
                    kind = "style"
                    remainder = parts[1:]
                    if remainder[0].lower() in {"scene", "character", "style"}:
                        kind = remainder.pop(0).lower()
                    path = Path(" ".join(remainder))
                    if not path.is_absolute():
                        candidate = Path.cwd() / path
                        path = candidate if candidate.exists() else project.absolute(path)
                    if not path.exists():
                        print(c(f"file not found: {path}", RED))
                        continue
                    entry = project.add_reference(path, kind=kind, source="upload")
                    abs_path = str(project.absolute(entry["path"]))
                    pending_images.append(abs_path)
                    print(c(f"attached {kind} reference: {entry['path']}", GREEN))
                elif cmd == "/open":
                    if len(parts) < 2:
                        print(c("usage: /open <path>", YELLOW))
                        continue
                    target = project.absolute(" ".join(parts[1:]))
                    if target.exists():
                        open_in_viewer(target)
                    else:
                        print(c(f"not found: {target}", RED))
                elif cmd == "/approve":
                    if len(parts) < 2:
                        print(c("usage: /approve <stage> [notes]", YELLOW))
                        continue
                    stage = parts[1]
                    notes = " ".join(parts[2:]) or None
                    project.set_gate(stage, True, notes)
                    print(c(f"approved: {stage}", GREEN))
                elif cmd == "/new":
                    if len(parts) < 2:
                        print(c("usage: /new <name>", YELLOW))
                        continue
                    name = slugify(parts[1])
                    project = Project(name, create=True)
                    bridge = Bridge(project, blender)
                    agent = Agent(project, bridge, cfg, printer=printer,
                                  tool_printer=tool_printer)
                    pending_images.clear()
                    print(c(f"switched to project '{name}'", GREEN))
                elif cmd == "/projects":
                    for name in list_projects():
                        print(f"  {name}")
                elif cmd == "/doctor":
                    doctor(cfg, runtime, project)
                else:
                    print(c(f"unknown command: {cmd} (try /help)", YELLOW))
            except Exception as exc:  # noqa: BLE001
                print(c(f"command failed: {exc}", RED))
            continue

        text = raw
        if pending_images:
            text += (
                f"\n\n[{len(pending_images)} reference image(s) attached by the user. "
                "They are also registered in the project.]"
            )
        try:
            agent.send(text, attachments=pending_images)
        except LLMError as exc:
            print(c(f"\nLLM error: {exc}", RED))
            print(c("Check /doctor, your .env endpoint/key/model, and that the provider is running.", DIM))
        except BlenderError as exc:
            print(c(f"\nBlender error: {exc}", RED))
        except KeyboardInterrupt:
            print(c("\n[cancelled]", YELLOW))
        finally:
            pending_images.clear()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
