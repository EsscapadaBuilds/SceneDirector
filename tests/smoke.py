"""Offline smoke tests + optional network checks.

    python tests/smoke.py
    python tests/smoke.py --net   # also calls Poly Haven and Pollinations
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from directorc.agent.tools import TOOLS, Toolbox  # noqa: E402
from directorc.blender import Bridge  # noqa: E402
from directorc.config import Config, Runtime  # noqa: E402
from directorc.state import Project, slugify  # noqa: E402

# 1x1 transparent PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d4944415478da63fcffff3f0300050001a5f64540000000004945"
    "4e44ae426082"
)

PASSED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        raise SystemExit(1)


def test_blender(runtime) -> None:
    """Optional end-to-end Blender build (fast)."""
    import shutil

    if not runtime.blender:
        print("  skip blender build (Blender not found)")
        return
    project = Project("blender-test", create=True)
    box = Toolbox(project, Bridge(project, runtime.blender), Config(),
                  progress=lambda *_: None)
    code = """
clear_scene()
bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 1))
add_ground(size=10)
add_sun(energy=4)
auto_frame_camera()
print("built")
"""
    result = box.execute("blender_run", {"code": code, "note": "smoke build"})
    check("blender builds a scene", result.get("ok") is True, str(result.get("error"))[:200])
    shot = box.execute("blender_screenshot", {})
    check("blender writes a screenshot",
          shot.get("ok") is True and project.absolute(shot["path"]).exists())

    export = project.root / "renders" / "test-cube.glb"
    result = box.execute("blender_run", {"code": f"""
clear_scene()
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
bpy.ops.export_scene.gltf(filepath=r"{export.as_posix()}", export_format="GLB")
"""})
    check("blender exports a cube glb", export.exists(),
          f"{result.get('error')} {result.get('stdout', '')[-300:]}")
    analysis = box.execute("analyze_model",
                           {"path": "renders/test-cube.glb", "target_height": 2})
    check("analyze_model reports height",
          analysis.get("ok") is True and
          abs(analysis["analysis"]["height"] - 2) < 0.01, str(analysis)[:300])
    check("analyze_model suggests scale",
          abs((analysis["analysis"].get("suggested_scale") or 0) - 1) < 0.01,
          str(analysis)[:300])
    shutil.rmtree(project.root, ignore_errors=True)


def test_agent_loop() -> None:
    """Drive the agent against a mock OpenAI-compatible server."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from directorc.agent import Agent
    from directorc.blender import Bridge
    from directorc.config import Config

    calls = {"count": 0}
    payloads: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            payloads.append(json.loads(self.rfile.read(length) or b"{}"))
            calls["count"] += 1
            if calls["count"] == 1:
                message = {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "project_status", "arguments": "{}"},
                    }],
                }
            else:
                message = {"role": "assistant", "content": "All set."}
            body = json.dumps({
                "id": "mock", "object": "chat.completion",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    project = Project("agent-loop-test", create=True)
    cfg = Config(
        llm_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        llm_api_key="test", llm_model="mock", llm_vision=False, llm_timeout=30,
        llm_max_tokens=700,
    )
    bridge = Bridge(project, None)
    printed: list[str] = []
    agent = Agent(project, bridge, cfg, printer=printed.append,
                  tool_printer=lambda *_: None)
    final = agent.send("hello")
    server.shutdown()

    check("agent runs tool then answers", final == "All set.")
    check("agent made two LLM calls", calls["count"] == 2)
    check("llm request sends max_tokens",
          all(p.get("max_tokens") == 700 for p in payloads),
          str([p.get("max_tokens") for p in payloads]))
    tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
    check("tool result recorded", bool(tool_messages) and "stage" in tool_messages[0]["content"])
    reopened = Project(project.name, create=False)
    check("chat history persisted", len(reopened.load_messages()) >= 3)

    import shutil
    shutil.rmtree(project.root, ignore_errors=True)


def test_ai3d() -> None:
    """Exercise the Tripo/Meshy connectors against a local mock API."""
    import json
    import shutil
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import directorc.ai3d as ai3d

    state = {
        "meshy_polls": 0,
        "tripo_polls": 0,
        "meshy_payload": None,
        "tripo_upload": False,
        "auth": set(),
    }

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            state["auth"].add(self.headers.get("Authorization", ""))
            if self.path == "/meshy/image-to-3d":
                state["meshy_payload"] = json.loads(raw or b"{}")
                return self._json(200, {"result": "task-m1"})
            if self.path == "/tripo/files":
                state["tripo_upload"] = "multipart/form-data" in \
                    self.headers.get("Content-Type", "")
                return self._json(200, {"code": 0, "data": {"file_token": "tok-1"}})
            if self.path == "/tripo/generation/image-to-model":
                return self._json(200, {"code": 0, "data": {"task_id": "task-t1"}})
            return self._json(404, {"error": "not found"})

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/files/model.glb":
                body = b"mock-glb"
                self.send_response(200)
                self.send_header("Content-Type", "model/gltf-binary")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/meshy/image-to-3d/task-m1":
                state["meshy_polls"] += 1
                if state["meshy_polls"] == 1:
                    return self._json(200, {"status": "IN_PROGRESS", "progress": 40})
                port = self.server.server_port
                return self._json(200, {
                    "status": "SUCCEEDED", "progress": 100,
                    "model_urls": {"glb": f"http://127.0.0.1:{port}/files/model.glb"},
                })
            if self.path == "/tripo/tasks/task-t1":
                state["tripo_polls"] += 1
                if state["tripo_polls"] == 1:
                    return self._json(200, {"code": 0, "data": {
                        "status": "running", "progress": 30}})
                port = self.server.server_port
                return self._json(200, {"code": 0, "data": {
                    "status": "success", "progress": 100,
                    "output": {
                        "model_url": f"http://127.0.0.1:{port}/files/model.glb",
                    },
                }})
            return self._json(404, {"error": "not found"})

        def log_message(self, *args) -> None:  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port

    project = Project("ai3d-test", create=True)
    source = project.root / "references" / "ref.png"
    source.write_bytes(PNG)
    ref = project.add_reference(source, kind="character", source="upload")
    project.approve_references("character")
    image = project.absolute(ref["path"])
    cfg = Config(
        ai3d_provider="auto", ai3d_timeout=30,
        tripo_api_key="test-key", meshy_api_key="test-key",
    )

    old_bases = (ai3d.MESHY_BASE, ai3d.TRIPO_BASE)
    old_sleep = ai3d.time.sleep
    ai3d.MESHY_BASE = f"http://127.0.0.1:{port}/meshy"
    ai3d.TRIPO_BASE = f"http://127.0.0.1:{port}/tripo"
    ai3d.time.sleep = lambda _seconds: None

    try:
        check("ai3d lists configured providers",
              ai3d.available_providers(cfg) == ["tripo", "meshy"])
        check("ai3d auto picks tripo", ai3d.pick_provider(cfg) == "tripo")

        progress: list[str] = []
        tripo = ai3d.generate_mesh(project, cfg, image, provider="tripo",
                                   progress=progress.append)
        check("tripo polls running then success", state["tripo_polls"] == 2)
        check("tripo uploads multipart", state["tripo_upload"])
        check("tripo downloads the model",
              tripo.path.exists() and tripo.path.read_bytes() == b"mock-glb")
        check("tripo task id", tripo.task_id == "task-t1")
        check("tripo progress reported", any("queued" in p for p in progress))

        meshy = ai3d.generate_mesh(project, cfg, image, provider="meshy",
                                   progress=lambda *_: None)
        check("meshy polls in-progress then success", state["meshy_polls"] == 2)
        payload = state["meshy_payload"] or {}
        check("meshy sends a data uri",
              str(payload.get("image_url", "")).startswith("data:image/png;base64,"))
        check("meshy downloads the model",
              meshy.path.exists() and meshy.path.read_bytes() == b"mock-glb")
        check("ai3d registers assets",
              len([a for a in project.assets() if a["source"].endswith("-api")]) == 2)
        check("ai3d sends bearer auth",
              state["auth"] == {"Bearer test-key"})

        box = Toolbox(project, Bridge(project, None), cfg,
                      progress=lambda *_: None)
        pending = project.add_reference(source, kind="scene", source="upload")
        pending_result = box.execute("generate_mesh", {
            "reference_path": pending["path"], "provider": "meshy",
        })
        check("generate_mesh requires approval",
              pending_result.get("ok") is False and
              "not approved" in pending_result.get("error", ""))
        tool_result = box.execute("generate_mesh", {
            "reference_path": ref["path"], "provider": "meshy",
        })
        check("generate_mesh tool returns an asset",
              tool_result.get("ok") is True and
              tool_result.get("asset", {}).get("source") == "meshy-api",
              str(tool_result)[:300])
        check("generate_mesh tool skips analysis without Blender",
              "analysis_error" in tool_result)
        missing = box.execute("analyze_model", {"path": "references/ref.png"})
        check("analyze_model reports missing Blender", missing.get("ok") is False)

        try:
            ai3d.generate_mesh(project, Config(), image)
            check("ai3d requires a key", False)
        except ai3d.AI3DError as exc:
            check("ai3d requires a key", "No image-to-3D provider" in str(exc))

        try:
            ai3d.generate_mesh(project, cfg, image, provider="manual")
            check("ai3d manual mode refuses", False)
        except ai3d.AI3DError as exc:
            check("ai3d manual mode refuses", "manual" in str(exc))
    finally:
        ai3d.MESHY_BASE, ai3d.TRIPO_BASE = old_bases
        ai3d.time.sleep = old_sleep
        server.shutdown()
        shutil.rmtree(project.root, ignore_errors=True)


def main() -> int:
    net = "--net" in sys.argv
    cfg = Config()
    runtime = Runtime(cfg)

    with tempfile.TemporaryDirectory() as tmp:
        # start clean so repeated runs are deterministic
        import shutil as _shutil
        stale = ROOT / "projects" / "smoke-test-offline"
        _shutil.rmtree(stale, ignore_errors=True)

        # project creation
        project = Project("smoke-test-offline")
        check("project name slug", project.name == "smoke-test-offline")
        check("project dirs", all((project.root / d).exists() for d in Project.SUBDIRS))

        img = Path(tmp) / "ref.png"
        img.write_bytes(PNG)
        ref = project.add_reference(img, kind="scene", source="upload")
        check("reference registered", ref["path"].startswith("references"))
        check("reference copied", project.absolute(ref["path"]).exists())

        bridge = Bridge(project, runtime.blender)
        box = Toolbox(project, bridge, cfg)

        check("unknown tool errors", box.execute("nope", {})["ok"] is False)

        # approval gate requires references
        result = box.execute("record_approval", {"stage": "character_reference"})
        check("gate blocks missing refs", result["ok"] is False)

        result = box.execute("record_approval", {"stage": "scene_reference"})
        check("gate passes with refs", result["ok"] is True)
        check("gate persisted", project.is_approved("scene_reference"))
        check("ref approved", project.references("scene")[0]["approved"] is True)

        # blender missing is reported, not crashed
        result = box.execute("blender_screenshot", {})
        if runtime.blender:
            check("blender screenshot works",
                  result.get("ok") is True and
                  project.absolute(result["path"]).exists())
        else:
            check("blender absence reported", result["ok"] is False)

        # manifest reload
        reloaded = Project(project.name, create=False)
        check("manifest reload", len(reloaded.references()) == 1)
        check("slugify", slugify("A Knight!") == "a-knight")

        # input-token budget for free-tier providers
        from directorc.llm import estimate_request_tokens, fit_to_budget

        history = [{"role": "system", "content": "system"}] + [
            {"role": "user", "content": "x" * 4000} for _ in range(4)
        ]
        fitted = fit_to_budget(history, None, 2500)
        check("input budget trims history",
              len(fitted) < len(history) and fitted[0]["role"] == "system" and
              fitted[-1] is history[-1])
        check("input budget respects estimate",
              estimate_request_tokens(fitted, None) <= 3000,
              str(estimate_request_tokens(fitted, None)))

        # Pollinations free-tier 402 parsing
        import requests
        from directorc.concept.images import _free_limit_wait, _is_free_limit

        def fake_402(body: bytes, retry_after: str | None = None):
            resp = requests.Response()
            resp.status_code = 402
            resp.encoding = "utf-8"
            resp._content = body
            resp.headers["Content-Type"] = "application/json"
            if retry_after:
                resp.headers["Retry-After"] = retry_after
            return resp

        resp = fake_402(
            b'{"error":{"code":"FREE_LIMIT","message":"Retry after 27s"}}'
        )
        check("free limit detected", _is_free_limit(resp))
        check("free limit wait parsed", 27.0 <= _free_limit_wait(resp) <= 29.0,
              str(_free_limit_wait(resp)))
        resp = fake_402(
            b'{"error":{"code":"FREE_LIMIT","message":"Retry after 27s"}}', "7"
        )
        check("free limit retry-after header", 7.0 <= _free_limit_wait(resp) <= 9.0)
        resp = fake_402(b'{"error":{"code":"BILLING","message":"no credit"}}')
        check("non-free 402 not retried", not _is_free_limit(resp))

        # tools schema sanity
        names = [t["function"]["name"] for t in TOOLS]
        for required in {"generate_references", "blender_run", "blender_screenshot",
                         "record_approval", "search_assets", "download_asset",
                         "import_inbox", "generate_mesh", "analyze_model"}:
            check(f"tool {required}", required in names)

        # inbox notification tracking
        (project.inbox_dir() / "hero.glb").write_bytes(b"glTF")
        check("inbox detects new files",
              any(f["name"] == "hero.glb" for f in project.inbox_new()))
        project.mark_inbox_seen()
        check("inbox marks files seen", not project.inbox_new())

        if net:
            from directorc import assets
            results = assets.search_all("armchair", "model", limit=3)
            check("polyhaven search", bool(results))
            from directorc.concept import generate_images

            out = Path(tmp) / "gen"
            paths = generate_images(cfg, "a red chair", count=1, out_dir=out)
            check("pollinations image", paths[0].exists() and paths[0].stat().st_size > 1000)

    # cleanup the smoke project from the real projects dir
    import shutil

    project_root = ROOT / "projects" / "smoke-test-offline"
    if project_root.exists():
        shutil.rmtree(project_root, ignore_errors=True)
    shutil.rmtree(ROOT / "projects" / "agent-loop-test", ignore_errors=True)

    test_ai3d()
    test_agent_loop()

    if "--blender" in sys.argv:
        test_blender(runtime)

    print(f"\n{len(PASSED)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

