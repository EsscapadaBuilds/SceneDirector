"""Local HTTP backend for the SceneDirector desktop app.

    python -m directorc.server [--port 8765] [--parent-watch]

The Tauri shell in ``ui/`` starts this process and talks to it over
127.0.0.1. It wraps the same Project / Agent / Bridge objects the CLI uses,
so both front ends share projects on disk. Standard library only.

Routes
    GET  /api/health
    GET  /api/info                         model, Blender, providers
    GET  /api/doctor[?project=name]        environment checks
    GET  /api/events[?after=seq]           server-sent events for agent turns
    GET  /api/projects                     list
    POST /api/projects                     {name} -> create
    GET  /api/projects/<name>              manifest, media, conversation
    POST /api/projects/<name>/chat         {message, attachments[]}
    POST /api/projects/<name>/gates        {stage, approved, notes}
    POST /api/projects/<name>/references   raw image body, ?kind=&name=
    POST /api/projects/<name>/inbox        register files from assets/inbox
    POST /api/projects/<name>/open         {path, app}
    POST /api/projects/<name>/delete       move the project to the Recycle Bin
    GET  /files/<name>/<path>              project files (images, video)
"""
from __future__ import annotations

import argparse
import itertools
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import ai3d
from . import assets as asset_lib
from .agent import Agent
from .blender import BlenderError, Bridge, open_in_viewer
from .config import PROJECTS_DIR, Runtime, get_config
from .llm import LLM, LLMError
from .state import Project, list_projects, slugify

DEFAULT_PORT = 8765
MAX_UPLOAD = 40 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov"}

# Origins the UI is served from: the Vite dev server and the Tauri webview.
ALLOWED_ORIGINS = {
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
}

OBSERVATION_PREFIX = "Latest rendered image(s) from the tools."
ATTACH_NOTE = re.compile(r"\n*\[\d+ reference image\(s\) attached by the user\.[^\]]*\]")

CFG = get_config()
RUNTIME = Runtime(CFG)
_ids = itertools.count(1)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------- events
class Hub:
    """In-memory event log with blocking reads, feeding the SSE stream."""

    def __init__(self, keep: int = 2000):
        self._cond = threading.Condition()
        self._events: list[dict] = []
        self._seq = 0
        self._keep = keep

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, project: str, kind: str, **data) -> int:
        with self._cond:
            self._seq += 1
            self._events.append({"seq": self._seq, "project": project, "kind": kind, **data})
            del self._events[:-self._keep]
            self._cond.notify_all()
            return self._seq

    def wait(self, after: int, timeout: float) -> list[dict]:
        with self._cond:
            if after > self._seq:  # client outlived a previous server process
                after = 0
            if self._seq <= after:
                self._cond.wait(timeout)
            return [e for e in self._events if e["seq"] > after]


HUB = Hub()


# ------------------------------------------------------------------- sessions
def _tool_summary(args: dict) -> str:
    for key in ("note", "prompt", "query", "id", "name", "stage", "mode",
                "reference_path", "path", "paths", "kind"):
        value = args.get(key)
        if value:
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            return str(value).replace("\n", " ")[:140]
    return ""


class Session:
    """One open project: its agent, and the items of the turn in flight."""

    def __init__(self, name: str):
        self.project = Project(name, create=False)
        if not self.project.root.is_dir():
            raise ApiError(404, f"No project named '{name}'")
        self.name = self.project.name
        self.bridge = Bridge(self.project, RUNTIME.blender)
        self.lock = threading.Lock()
        self.busy = False
        self.live: list[dict] = []
        self.agent: Agent | None = None
        self._running_tool: dict | None = None

    # ---------------------------------------------------------------- items
    def _add(self, item: dict) -> dict:
        item["id"] = f"t{next(_ids)}"
        self.live.append(item)
        HUB.publish(self.name, "item", item=dict(item))
        return item

    def _update(self, item: dict, **patch) -> None:
        item.update(patch)
        HUB.publish(self.name, "update", id=item["id"], patch=patch)

    def media(self, raw) -> dict | None:
        """Describe a project file for the UI, or None if it is outside the project."""
        path = Path(str(raw))
        if not path.is_absolute():
            path = self.project.root / path
        try:
            resolved = path.resolve()
            rel = resolved.relative_to(self.project.root.resolve()).as_posix()
            stat = resolved.stat()
        except (ValueError, OSError):
            return None
        if not resolved.is_file():
            return None
        return {"path": rel, "name": resolved.name, "v": int(stat.st_mtime),
                "size": stat.st_size}

    def digest(self, result) -> dict:
        """Reduce a tool result to what the conversation view shows."""
        if not isinstance(result, dict):
            return {"ok": True, "media": []}
        found: dict[str, dict] = {}
        candidates = list(result.get("images") or []) + list(result.get("opened") or [])
        if isinstance(result.get("path"), str):
            candidates.append(result["path"])
        for raw in candidates:
            entry = self.media(raw)
            if entry and Path(entry["path"]).suffix.lower() in IMAGE_EXTS | VIDEO_EXTS | {".md"}:
                found.setdefault(entry["path"], entry)
        out = {"ok": bool(result.get("ok", True)), "media": list(found.values())}
        if result.get("error"):
            out["error"] = str(result["error"])[:600]
        return out

    # ---------------------------------------------------------------- agent
    def _get_agent(self) -> Agent:
        if self.agent is not None:
            return self.agent
        agent = Agent(self.project, self.bridge, CFG,
                      printer=self._on_text, tool_printer=self._on_progress)
        box = agent.box
        run_tool = box.execute

        def execute(name: str, args: dict) -> dict:
            item = self._add({"type": "tool", "name": name, "running": True,
                              "summary": _tool_summary(args or {})})
            self._running_tool = item
            try:
                result = run_tool(name, args)
            finally:
                self._running_tool = None
            self._update(item, running=False, progress=None, **self.digest(result))
            HUB.publish(self.name, "state")
            return result

        def show_to_user(paths: list[str]) -> dict:
            # The CLI opens the OS viewer; the app shows files in the conversation.
            shown, images = [], []
            for raw in paths:
                p = self.project.absolute(raw)
                if not p.exists():
                    continue
                shown.append(str(p))
                if p.suffix.lower() in IMAGE_EXTS:
                    images.append(str(p))
            return {"ok": bool(shown), "opened": shown, "images": images,
                    "note": "Shown to the user inside the app." if shown
                    else "No matching files."}

        box.execute = execute
        box.tool_show_to_user = show_to_user
        self.agent = agent
        return agent

    def _on_text(self, text: str) -> None:
        self._add({"type": "assistant", "text": text})

    def _on_progress(self, text: str) -> None:
        # Outside a tool call this is the CLI's "name(args)" echo; the tool item covers it.
        if self._running_tool is not None:
            self._update(self._running_tool, progress=text.strip())

    # ----------------------------------------------------------------- turn
    def start_turn(self, text: str, attachments: list[str]) -> None:
        with self.lock:
            if self.busy:
                raise ApiError(409, "The director is still working on the previous message.")
            self.busy = True
            self.live = []
        shown = [m for m in (self.media(a) for a in attachments) if m]
        self._add({"type": "user", "text": text, "media": shown})
        HUB.publish(self.name, "busy", busy=True)
        threading.Thread(target=self._run_turn, args=(text, [m["path"] for m in shown]),
                         daemon=True).start()

    def _run_turn(self, text: str, attachments: list[str]) -> None:
        failure: dict | None = None
        agent: Agent | None = None
        try:
            agent = self._get_agent()
            paths = [str(self.project.absolute(p)) for p in attachments]
            if paths:
                text += (f"\n\n[{len(paths)} reference image(s) attached by the user. "
                         "They are also registered in the project.]")
            agent.send(text, attachments=paths)
        except LLMError as exc:
            failure = {"type": "error", "title": "The language model request failed",
                       "text": str(exc),
                       "hint": "Check the endpoint, key and model in .env, and that the "
                               "provider is running. Diagnostics has a live test."}
        except BlenderError as exc:
            failure = {"type": "error", "title": "Blender failed", "text": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            failure = {"type": "error", "title": "Something went wrong",
                       "text": f"{type(exc).__name__}: {exc}"}
        if failure:
            if agent is not None:
                try:  # keep the user's message so the next attempt has context
                    self.project.save_messages(agent._serialize())
                except Exception:  # noqa: BLE001
                    pass
            self._add(failure)
        with self.lock:
            self.busy = False
            # History on disk now covers the turn; only an error has no saved form.
            self.live = [failure] if failure else []
        HUB.publish(self.name, "done", failed=bool(failure))

    # ---------------------------------------------------------------- views
    def history(self) -> list[dict]:
        items: list[dict] = []
        calls: dict[str, dict] = {}
        for i, msg in enumerate(self.project.load_messages()):
            role = msg.get("role")
            content = msg.get("content") or ""
            if not isinstance(content, str):
                content = ""
            if role == "user":
                if content.startswith(OBSERVATION_PREFIX):
                    continue
                attached = "[image attachment]" in content or bool(ATTACH_NOTE.search(content))
                text = ATTACH_NOTE.sub("", content.replace("[image attachment]", "")).strip()
                items.append({"id": f"h{i}", "type": "user", "text": text,
                              "media": [], "attached": attached})
            elif role == "assistant":
                if content.strip():
                    items.append({"id": f"h{i}", "type": "assistant", "text": content.strip()})
                for n, call in enumerate(msg.get("tool_calls") or []):
                    fn = call.get("function") or {}
                    try:
                        raw = fn.get("arguments") or "{}"
                        args = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        args = {}
                    item = {"id": f"h{i}-{n}", "type": "tool", "name": fn.get("name", "tool"),
                            "summary": _tool_summary(args if isinstance(args, dict) else {}),
                            "running": False, "ok": True, "media": []}
                    calls[call.get("id", "")] = item
                    items.append(item)
            elif role == "tool":
                item = calls.get(msg.get("tool_call_id", ""))
                if item is None:
                    continue
                try:
                    item.update(self.digest(json.loads(content)))
                except json.JSONDecodeError:  # long results are stored truncated
                    item["ok"] = not content.lstrip().startswith('{"ok": false')
        return items

    def files(self, sub: str, exts: set[str]) -> list[dict]:
        folder = self.project.root / sub
        if not folder.is_dir():
            return []
        found = [self.media(p) for p in folder.iterdir()
                 if p.is_file() and p.suffix.lower() in exts]
        return sorted((f for f in found if f), key=lambda f: f["v"], reverse=True)

    def detail(self) -> dict:
        project = self.project
        seq = HUB.seq  # read before the items so no event is skipped
        new = {f["name"] for f in project.inbox_new()}
        refs = []
        for ref in project.references():
            info = self.media(ref["path"])
            refs.append({**ref, "v": info["v"] if info else 0, "missing": info is None})
        return {
            "name": self.name,
            "root": str(project.root),
            "stage": project.manifest.get("stage", "intake"),
            "gates": project.manifest.get("gates", {}),
            "references": refs,
            "assets": project.assets(),
            "inbox": [{**f, "new": f["name"] in new} for f in project.inbox_files()],
            "renders": self.files("renders", IMAGE_EXTS),
            "output": self.files("output", IMAGE_EXTS | VIDEO_EXTS),
            "docs": self.files("docs", {".md"}),
            "has_blend": project.blend_path.exists(),
            "busy": self.busy,
            "messages": self.history() + [dict(i) for i in self.live],
            "seq": seq,
        }


_sessions: dict[str, Session] = {}
_sessions_lock = threading.Lock()


def get_session(name: str) -> Session:
    key = slugify(name)
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None or not session.project.root.is_dir():
            session = Session(key)
            _sessions[key] = session
        return session


# ------------------------------------------------------------------ api logic
def project_summaries() -> list[dict]:
    out = []
    for name in list_projects():
        root = PROJECTS_DIR / name
        stage, updated = "intake", ""
        try:
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            stage = manifest.get("stage", stage)
            updated = manifest.get("updated", "")
        except (OSError, json.JSONDecodeError):
            pass
        cover = None
        for sub in ("output", "renders", "references"):
            folder = root / sub
            if not folder.is_dir():
                continue
            images = [p for p in folder.iterdir()
                      if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
            if images:
                best = max(images, key=lambda p: p.stat().st_mtime)
                cover = {"path": f"{sub}/{best.name}", "v": int(best.stat().st_mtime)}
                break
        session = _sessions.get(name)
        out.append({"name": name, "stage": stage, "updated": updated, "cover": cover,
                    "busy": bool(session and session.busy)})
    return sorted(out, key=lambda p: p["updated"], reverse=True)


def info() -> dict:
    return {
        "model": CFG.llm_model,
        "endpoint": CFG.llm_base_url,
        "vision": CFG.llm_vision,
        "blender": str(RUNTIME.blender) if RUNTIME.blender else None,
        "image_provider": CFG.image_provider,
        "ai3d": ai3d.available_providers(CFG),
        "projects_dir": str(PROJECTS_DIR),
    }


def doctor(project_name: str | None) -> list[dict]:
    """The same checks as `python cli.py --doctor`, as data."""
    import requests

    checks: list[dict] = []

    def add(label: str, status: str, detail: str) -> None:
        checks.append({"label": label, "status": status, "detail": detail})

    add("Python", "ok", sys.version.split()[0])
    try:
        reply = LLM(CFG).chat(
            [{"role": "user", "content": "Reply with the single word: ok"}], temperature=0)
        word = ((reply.message.get("content") or "").strip().splitlines() or [""])[0][:40]
        add("Language model", "ok", f"{CFG.model_label()} replied \"{word}\"")
    except LLMError as exc:
        add("Language model", "fail", f"{CFG.model_label()}: {exc}")
    if not CFG.llm_vision:
        add("Vision", "warn", "VISION=0, so the director cannot see references or its own renders")

    if RUNTIME.blender:
        if project_name:
            try:
                probe = get_session(project_name).bridge.probe()
                if probe.get("ok"):
                    add("Blender", "ok", f"{probe.get('version')} at {RUNTIME.blender}")
                else:
                    add("Blender", "fail", str(probe.get("error")))
            except (BlenderError, ApiError) as exc:
                add("Blender", "fail", str(exc))
        else:
            add("Blender", "ok", f"found at {RUNTIME.blender} (open a project to test it)")
    else:
        add("Blender", "fail", "not found. Install Blender or set BLENDER_PATH in .env")

    try:
        requests.get("https://image.pollinations.ai", timeout=10)
        mode = "with key" if CFG.pollinations_api_key else "free tier, 1 image every 45s"
        add("Concept images", "ok", f"Pollinations reachable ({mode})")
    except Exception as exc:  # noqa: BLE001
        add("Concept images", "warn", f"Pollinations unreachable: {exc}")
    try:
        r = requests.get("https://api.polyhaven.com/types", timeout=10)
        add("Free assets", "ok", f"Poly Haven reachable (HTTP {r.status_code})")
    except Exception as exc:  # noqa: BLE001
        add("Free assets", "warn", f"Poly Haven unreachable: {exc}")

    providers = ai3d.available_providers(CFG)
    if providers:
        add("Image-to-3D", "ok", f"{', '.join(providers)} configured")
    else:
        add("Image-to-3D", "warn", "no API key, so models come through assets/inbox")
    return checks


def save_upload(session: Session, kind: str, filename: str, body: bytes) -> dict:
    if kind not in {"scene", "character", "style"}:
        raise ApiError(400, "kind must be scene, character or style")
    ext = Path(filename).suffix.lower()
    if ext not in IMAGE_EXTS:
        raise ApiError(400, "Only PNG, JPG, WEBP or BMP images can be attached")
    if not body:
        raise ApiError(400, "The file is empty")
    stem = slugify(Path(filename).stem, 32)
    with tempfile.TemporaryDirectory() as tmp:
        temp = Path(tmp) / f"{stem}{ext}"
        temp.write_bytes(body)
        entry = session.project.add_reference(temp, kind=kind, source="upload")
    return entry


def open_path(session: Session, raw: str, app: str | None) -> None:
    project = session.project
    target = (project.root / raw).resolve() if raw else project.root.resolve()
    if project.root.resolve() not in [target, *target.parents]:
        raise ApiError(400, "Path is outside the project")
    if not target.exists():
        raise ApiError(404, f"Not found: {raw}")
    if app == "blender":
        if not RUNTIME.blender:
            raise ApiError(400, "Blender was not found")
        if session.busy:
            raise ApiError(409, "Wait for the director to finish before opening the scene")
        subprocess.Popen([str(RUNTIME.blender), str(project.blend_path)],
                         cwd=str(project.root))
    else:
        open_in_viewer(target)


def _recycle(path: Path) -> None:
    """Send a folder to the Recycle Bin on Windows; delete it outright elsewhere."""
    if os.name != "nt":
        import shutil
        shutil.rmtree(path)
        return
    import ctypes
    from ctypes import wintypes

    class FileOp(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p)]

    source = ctypes.create_unicode_buffer(str(path) + "\0")  # list ends with two NULs
    op = FileOp()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = ctypes.cast(source, ctypes.c_wchar_p)
    op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # allow undo, no prompt, silent, no error UI
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted or path.exists():
        raise ApiError(500, "Could not move the project to the Recycle Bin. "
                            "Close anything that has its files open (Blender, a viewer) "
                            "and try again.")


def delete_project(name: str) -> None:
    session = get_session(name)  # 404 when there is no such project
    root = session.project.root.resolve()
    if root.parent != PROJECTS_DIR.resolve():
        raise ApiError(400, "Not a project folder")
    with session.lock:
        if session.busy:
            raise ApiError(409, "The director is working in this project. "
                                "Wait for it to finish before deleting.")
        session.busy = True  # block new turns while the folder goes away
    try:
        _recycle(root)
    except BaseException:
        session.busy = False
        raise
    with _sessions_lock:
        _sessions.pop(session.name, None)
    HUB.publish(session.name, "deleted")


# ----------------------------------------------------------------------- http
PROJECT_ROUTE = re.compile(r"^/api/projects/([^/]+)(?:/([a-z]+))?$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SceneDirector"
    verbose = False

    def log_message(self, fmt, *args):  # noqa: A003
        if self.verbose:
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- plumbing
    def _origin_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"}:
            return False  # DNS-rebinding guard
        origin = self.headers.get("Origin")
        return origin is None or origin in ALLOWED_ORIGINS

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            self.close_connection = True
            raise ApiError(413, "File is too large (40 MB limit)")
        return self.rfile.read(length) if length else b""

    def _payload(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "Request body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "Request body must be a JSON object")
        return data

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = unquote(url.path)
        try:
            if not self._origin_ok():
                raise ApiError(403, "Origin not allowed")
            if method == "GET" and path.startswith("/files/"):
                self._file(path[len("/files/"):])
            elif method == "GET" and path == "/api/events":
                self._events(query)
            else:
                self._json(self._route(method, path, query))
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except (BrokenPipeError, ConnectionError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204 if self._origin_ok() else 403)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --------------------------------------------------------------- routes
    def _route(self, method: str, path: str, query: dict):
        if method == "GET":
            if path == "/api/health":
                return {"ok": True, "service": "scenedirector"}
            if path == "/api/info":
                return info()
            if path == "/api/doctor":
                return {"checks": doctor(query.get("project") or None)}
            if path == "/api/projects":
                return {"projects": project_summaries()}
        if method == "POST" and path == "/api/projects":
            raw = str(self._payload().get("name") or "").strip()
            if not re.search(r"[a-zA-Z0-9]", raw):
                raise ApiError(400, "Give the project a name with letters or numbers")
            name = slugify(raw)
            if (PROJECTS_DIR / name).is_dir():
                raise ApiError(409, f"A project named '{name}' already exists")
            Project(name, create=True).save()
            return {"name": name}

        match = PROJECT_ROUTE.match(path)
        if not match:
            raise ApiError(404, "Not found")
        action = match.group(2)
        if method == "POST" and action == "delete":
            self._body()
            delete_project(match.group(1))
            return {"ok": True}
        session = get_session(match.group(1))

        if method == "GET" and action is None:
            return session.detail()
        if method != "POST":
            raise ApiError(405, "Method not allowed")

        if action == "references":
            entry = save_upload(session, query.get("kind", "style"),
                                query.get("name", "upload.png"), self._body())
            return {"reference": {**entry, **(session.media(entry["path"]) or {})}}

        data = self._payload()
        if action == "chat":
            text = str(data.get("message") or "").strip()
            if not text:
                raise ApiError(400, "Message is empty")
            attachments = [str(a) for a in data.get("attachments") or []]
            session.start_turn(text, attachments)
            return {"ok": True}
        if action == "gates":
            stage = slugify(str(data.get("stage") or "")).replace("-", "_")
            if not data.get("stage"):
                raise ApiError(400, "stage is required")
            session.project.set_gate(stage, bool(data.get("approved", True)),
                                     str(data["notes"]) if data.get("notes") else None)
            HUB.publish(session.name, "state")
            return {"ok": True, "gate": session.project.gate(stage)}
        if action == "inbox":
            registered = asset_lib.import_inbox(session.project)
            session.project.mark_inbox_seen()
            HUB.publish(session.name, "state")
            return {"ok": True, "registered": len(registered)}
        if action == "open":
            open_path(session, str(data.get("path") or ""), data.get("app"))
            return {"ok": True}
        raise ApiError(404, "Not found")

    # ---------------------------------------------------------------- files
    def _file(self, rest: str) -> None:
        name, _, rel = rest.partition("/")
        session = get_session(name)
        root = session.project.root.resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            raise ApiError(404, "File not found")
        size = target.stat().st_size
        start, end = 0, size - 1
        status = 200
        match = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), end) if match.group(2) else end
            else:
                start = max(0, size - int(match.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self._cors()
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type",
                         mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=31536000, immutable"
                         if "v=" in self.path else "no-cache")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with target.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    # --------------------------------------------------------------- events
    def _events(self, query: dict) -> None:
        try:
            after = int(query.get("after") or self.headers.get("Last-Event-ID") or HUB.seq)
        except ValueError:
            after = HUB.seq
        self.close_connection = True
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b": connected\n\n")
        self.wfile.flush()
        while True:
            events = HUB.wait(after, timeout=15)
            if not events:
                self.wfile.write(b": ping\n\n")
            for event in events:
                after = event["seq"]
                data = json.dumps(event, ensure_ascii=False)
                self.wfile.write(f"id: {after}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def _watch_parent() -> None:
    """Exit when the app that started us goes away (its end of stdin closes)."""
    try:
        while sys.stdin.buffer.read(1):
            pass
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


def main() -> int:
    parser = argparse.ArgumentParser(description="SceneDirector desktop backend")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--parent-watch", action="store_true",
                        help="exit when stdin closes (used by the desktop app)")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args()

    Handler.verbose = args.verbose
    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1
    if args.parent_watch:
        threading.Thread(target=_watch_parent, daemon=True).start()
    print(f"SceneDirector backend on http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
