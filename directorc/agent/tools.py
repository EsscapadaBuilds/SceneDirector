"""Tool definitions and execution for the agent."""
from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path
from typing import Any

from .. import assets as asset_lib
from .. import ai3d
from ..blender.bridge import BlenderError, Bridge, open_in_viewer
from ..concept import build_prompt, generate_images
from ..config import Config
from ..state import Project, slugify

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_references",
            "description": (
                "Generate concept reference images for a scene or character and register "
                "them in the project. Always show them to the user and wait for approval "
                "before building."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["scene", "character", "style"]},
                    "prompt": {"type": "string", "description": "Detailed concept-art prompt"},
                    "count": {
                        "type": "integer", "minimum": 1, "maximum": 4, "default": 3,
                        "description": (
                            "How many options. The anonymous image tier allows one "
                            "image every 45s, so count=1 is fastest."
                        ),
                    },
                    "seed": {"type": "integer", "description": "Optional fixed seed"},
                },
                "required": ["kind", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_to_user",
            "description": "Open one or more images/videos for the user to review.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["paths"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_upload",
            "description": (
                "Copy a reference image the user mentioned (by file path) into the project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or workspace-relative path"},
                    "kind": {"type": "string", "enum": ["scene", "character", "style"]},
                    "notes": {"type": "string"},
                },
                "required": ["path", "kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_references",
            "description": "List all reference images in the project with approval status.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_approval",
            "description": (
                "Record that the user approved a gate: scene_reference, character_reference, "
                "shot_plan, preview. Only call after the user explicitly approves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {
                        "type": "string",
                        "enum": ["scene_reference", "character_reference", "shot_plan",
                                 "assets", "preview", "final"],
                    },
                    "notes": {"type": "string"},
                },
                "required": ["stage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_assets",
            "description": "Search free CC0 asset libraries (Poly Haven, ambientCG) for models, HDRIs, textures.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "kind": {"type": "string", "enum": ["model", "hdri", "texture"]},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["query", "kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "download_asset",
            "description": "Download a free asset into the project (Poly Haven or ambientCG), or any direct URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["polyhaven", "ambientcg", "url"]},
                    "id": {"type": "string", "description": "Asset id from search results"},
                    "kind": {"type": "string", "enum": ["model", "hdri", "texture"]},
                    "resolution": {"type": "string", "default": "1k"},
                    "url": {"type": "string"},
                    "filename": {"type": "string"},
                    "license": {"type": "string"},
                },
                "required": ["provider"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "import_inbox",
            "description": "Register files the user dropped into assets/inbox/ as usable assets.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_mesh",
            "description": (
                "Generate a 3D mesh from an approved reference image using Tripo or Meshy. "
                "Only works when the matching API key is configured; otherwise use the "
                "manual assets/inbox workflow. Automatically analyzes the mesh health."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reference_path": {
                        "type": "string",
                        "description": "Project-relative reference image path",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "tripo", "meshy"],
                        "default": "auto",
                    },
                    "target_height": {
                        "type": "number",
                        "description": "Expected height in meters (e.g. 1.8 for a human)",
                    },
                },
                "required": ["reference_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_model",
            "description": (
                "Inspect a model file in a throwaway Blender session: dimensions, scale, "
                "materials, textures, non-manifold edges, loose parts. Use after importing "
                "or generating a mesh."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Project-relative model path"},
                    "target_height": {"type": "number"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "blender_status",
            "description": "Check that Blender is installed and reachable.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "blender_run",
            "description": (
                "Execute Python inside Blender (headless) with scene helpers preloaded. "
                "Saves scene.blend. Returns a scene report and stdout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "bpy Python code"},
                    "note": {"type": "string", "description": "Short description of this step"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "blender_screenshot",
            "description": (
                "Render a fast verification screenshot of the current scene "
                "and return its path. Use after every build step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "camera": {"type": "string"},
                    "frame": {"type": "integer"},
                    "engine": {"type": "string", "enum": ["BLENDER_WORKBENCH", "BLENDER_EEVEE_NEXT"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "blender_render",
            "description": "Render a preview image, final image, or animation to the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["preview", "final", "animation"]},
                    "resolution": {"type": "string", "description": "e.g. 1280x720"},
                    "samples": {"type": "integer"},
                    "frame": {"type": "integer"},
                    "frame_start": {"type": "integer"},
                    "frame_end": {"type": "integer"},
                    "fps": {"type": "integer"},
                    "camera": {"type": "string"},
                    "engine": {"type": "string"},
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_document",
            "description": "Save a markdown document (shot plan, notes) in the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_status",
            "description": "Current project state: stage, references, assets, gates, Blender.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _parse_resolution(value: str | None, default: tuple[int, int]) -> tuple[int, int]:
    if not value:
        return default
    try:
        w, h = value.lower().split("x")
        return max(64, int(w)), max(64, int(h))
    except Exception:
        return default


class Toolbox:
    def __init__(self, project: Project, bridge: Bridge, cfg: Config,
                 progress: Any = None):
        self.project = project
        self.bridge = bridge
        self.cfg = cfg
        self.progress = progress or (lambda *a, **k: None)
        self._shot_counter = 0
        self._render_counter = 0
        self._probe: dict | None = None
        self.generated_images: list[str] = []

    # ------------------------------------------------------------------ execute
    def execute(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        try:
            # Models sometimes send empty or unexpected keys; keep only real params.
            params = inspect.signature(handler).parameters
            clean = {k: v for k, v in (args or {}).items() if k and k in params}
            return handler(**clean)
        except TypeError as exc:
            return {"ok": False, "error": f"Bad arguments for {name}: {exc}"}
        except (BlenderError, asset_lib.AssetError, ai3d.AI3DError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - return to the agent for self-correction
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.project.root).as_posix()
        except ValueError:
            return str(path)

    def _stage(self, stage: str) -> None:
        self.project.manifest["stage"] = stage
        self.project.save()

    # --------------------------------------------------------------- references
    def tool_generate_references(self, kind: str, prompt: str, count: int = 3,
                                 seed: int | None = None) -> dict:
        count = max(1, min(4, int(count)))
        composed = build_prompt(prompt, kind)
        with tempfile.TemporaryDirectory() as tmp:
            paths = generate_images(self.cfg, composed, count=count,
                                    out_dir=Path(tmp), seed=seed,
                                    progress=self.progress)
            entries = [
                self.project.add_reference(p, kind=kind, source="generated", prompt=prompt)
                for p in paths
            ]
        self._stage("concept")
        abs_paths = [str(self.project.absolute(e["path"])) for e in entries]
        self.generated_images.extend(abs_paths)
        return {
            "ok": True,
            "references": entries,
            "absolute_paths": abs_paths,
            "images": abs_paths,
            "next": (
                "Show these to the user with show_to_user, describe the differences, "
                "and ask which one to use. Do not build until the user approves."
            ),
        }

    def tool_show_to_user(self, paths: list[str]) -> dict:
        opened = []
        images = []
        for raw in paths:
            p = self.project.absolute(raw)
            if not p.exists():
                continue
            open_in_viewer(p)
            opened.append(self._rel(p))
            if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
                images.append(str(p))
        if images:
            self.generated_images.extend(images)
        return {"ok": bool(opened), "opened": opened, "images": images,
                "note": "Opened in the user's default viewer." if opened else "No matching files."}

    def tool_register_upload(self, path: str, kind: str, notes: str | None = None) -> dict:
        p = Path(path)
        if not p.is_absolute():
            p = (self.project.root / path).resolve()
        if not p.exists():
            return {"ok": False, "error": f"File not found: {path}"}
        entry = self.project.add_reference(p, kind=kind, source="upload", notes=notes)
        return {"ok": True, "reference": entry}

    def tool_list_references(self) -> dict:
        refs = self.project.references()
        return {"ok": True, "references": refs, "count": len(refs)}

    def tool_record_approval(self, stage: str, notes: str | None = None) -> dict:
        if stage in {"scene_reference", "character_reference"}:
            kind = stage.replace("_reference", "")
            refs = self.project.references(kind)
            if not refs:
                return {
                    "ok": False,
                    "error": (
                        f"No {kind} references exist yet. Generate or register references "
                        "and get the user's approval first."
                    ),
                }
        self.project.set_gate(stage, True, notes)
        stage_map = {
            "scene_reference": "references_approved",
            "character_reference": "references_approved",
            "shot_plan": "shot_plan_approved",
            "assets": "assets_ready",
            "preview": "preview_approved",
            "final": "delivered",
        }
        self._stage(stage_map.get(stage, stage))
        return {"ok": True, "stage": stage, "gate": self.project.gate(stage),
                "project_stage": self.project.manifest["stage"]}

    # ------------------------------------------------------------------- assets
    def tool_search_assets(self, query: str, kind: str, limit: int = 8) -> dict:
        results = asset_lib.search_all(query, kind, limit=max(1, min(20, int(limit))))
        return {"ok": True, "query": query, "kind": kind, "results": results,
                "count": len(results)}

    def tool_download_asset(self, provider: str, id: str | None = None, kind: str = "model",
                            resolution: str = "1k", url: str | None = None,
                            filename: str | None = None,
                            license: str = "unknown") -> dict:
        if provider == "polyhaven":
            if not id:
                return {"ok": False, "error": "id is required for polyhaven"}
            entry = asset_lib.download_polyhaven(self.project, id, kind, resolution)
        elif provider == "ambientcg":
            if not id:
                return {"ok": False, "error": "id is required for ambientcg"}
            entry = asset_lib.download_ambientcg(self.project, id, resolution.upper())
        elif provider == "url":
            if not url or not filename:
                return {"ok": False, "error": "url and filename are required"}
            entry = asset_lib.download_url(self.project, url, filename, kind, license)
        else:
            return {"ok": False, "error": f"Unknown provider: {provider}"}
        self._stage("assets")
        return {"ok": True, "asset": entry}

    def tool_import_inbox(self) -> dict:
        entries = asset_lib.import_inbox(self.project)
        self.project.mark_inbox_seen()
        return {"ok": True, "imported": entries, "count": len(entries),
                "note": "Registered. Use analyze_model on mesh files before placing them."}

    def tool_generate_mesh(self, reference_path: str, provider: str = "auto",
                           target_height: float | None = None) -> dict:
        reference = self.project.absolute(reference_path)
        if not reference.exists():
            return {"ok": False, "error": f"Reference not found: {reference_path}"}
        match = next(
            (r for r in self.project.references()
             if self.project.absolute(r["path"]).resolve() == reference.resolve()),
            None,
        )
        if match is None:
            return {"ok": False, "error": (
                f"{reference_path} is not a registered reference. Use register_upload "
                "first so the user can approve it."
            )}
        if not match.get("approved"):
            return {"ok": False, "error": (
                "That reference is not approved yet. Show it with show_to_user and call "
                "record_approval after the user agrees; generating costs credits."
            )}
        target = None
        if target_height not in (None, "", 0, "0"):
            try:
                target = float(target_height)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"Invalid target_height: {target_height}"}

        self.progress("  [ai3d] requesting mesh from "
                      f"{provider if provider != 'auto' else 'configured provider'}")
        result = ai3d.generate_mesh(
            self.project, self.cfg, reference, provider=provider,
            progress=lambda msg: self.progress(f"  [ai3d] {msg}"),
        )
        self._stage("assets")
        rel = self._rel(result.path)
        asset = next((a for a in self.project.assets() if a["path"] == rel), None)

        out: dict = {
            "ok": True,
            "provider": result.provider,
            "task_id": result.task_id,
            "path": rel,
            "absolute_path": str(result.path),
            "asset": asset,
            "next": (
                "Run analyze_model if needed, then import this GLB with import_asset, "
                "call scale_to_height(obj, target) and place it on the ground."
            ),
        }
        if result.thumbnail and result.thumbnail.exists():
            out["thumbnail"] = self._rel(result.thumbnail)
            out["images"] = [str(result.thumbnail)]

        if self.bridge.available:
            try:
                out["analysis"] = self.bridge.analyze(result.path, target)
            except BlenderError as exc:
                out["analysis_error"] = str(exc)
        else:
            out["analysis_error"] = "Blender not available; mesh health check skipped."
        return out

    def tool_analyze_model(self, path: str, target_height: float | None = None) -> dict:
        p = self.project.absolute(path)
        if not p.exists():
            return {"ok": False, "error": f"Model not found: {path}"}
        target = None
        if target_height not in (None, "", 0, "0"):
            try:
                target = float(target_height)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"Invalid target_height: {target_height}"}
        analysis = self.bridge.analyze(p, target)
        return {"ok": True, "analysis": analysis}

    # ------------------------------------------------------------------ blender
    def tool_blender_status(self) -> dict:
        if self._probe is None:
            if not self.bridge.available:
                return {
                    "ok": False,
                    "error": (
                        "Blender not found. Install from blender.org or set BLENDER_PATH "
                        "in .env, then restart."
                    ),
                    "blender": None,
                }
            self._probe = self.bridge.probe()
        return {"ok": bool(self._probe and self._probe.get("ok")), **self._probe}

    def tool_blender_run(self, code: str, note: str | None = None) -> dict:
        self.progress(f"  [blender] {note or 'run'} ({len(code)} chars of bpy)")
        result = self.bridge.python(code)
        self._stage("building")
        data = result.data
        if not result.ok:
            return {
                "ok": False,
                "error": data.get("error"),
                "traceback": data.get("traceback"),
                "stdout": data.get("stdout"),
                "duration_s": round(result.duration, 1),
            }
        return {
            "ok": True,
            "note": note,
            "report": data.get("report"),
            "stdout": (data.get("stdout") or "")[-3000:],
            "duration_s": round(result.duration, 1),
        }

    def tool_blender_screenshot(self, camera: str | None = None,
                                frame: int | None = None,
                                engine: str = "BLENDER_WORKBENCH") -> dict:
        self._shot_counter += 1
        out = self.project.root / "renders" / f"verify-{self._shot_counter:03d}.png"
        result_path = self.bridge.screenshot(out, camera=camera, frame=frame,
                                             engine=engine, resolution=(960, 540))
        self._stage("verifying")
        return {
            "ok": True,
            "path": self._rel(result_path),
            "absolute_path": str(result_path),
            "images": [str(result_path)],
            "instruction": (
                "Inspect this screenshot. Compare it with the approved reference. "
                "Fix geometry, scale, framing, or lighting if needed."
            ),
        }

    def tool_blender_render(self, mode: str = "preview", resolution: str | None = None,
                            samples: int | None = None, frame: int | None = None,
                            frame_start: int | None = None, frame_end: int | None = None,
                            fps: int | None = None, camera: str | None = None,
                            engine: str | None = None) -> dict:
        self._render_counter += 1
        renders = self.project.root / "renders"
        output = self.project.root / "output"

        if mode == "animation":
            res = _parse_resolution(resolution, (1280, 720))
            path = output / f"animation-{self._render_counter:03d}.mp4"
            result = self.bridge.render(
                path, camera=camera, engine=engine or "BLENDER_EEVEE_NEXT",
                samples=samples or 16, resolution=res, animation=True,
                frame_start=frame_start, frame_end=frame_end, fps=fps,
                timeout=None,
            )
        elif mode == "final":
            res = _parse_resolution(resolution, (1920, 1080))
            path = output / f"final-{self._render_counter:03d}.png"
            result = self.bridge.render(
                path, camera=camera, engine=engine or self.cfg.final_engine,
                samples=samples or 128, resolution=res, frame=frame, timeout=None,
            )
        else:
            res = _parse_resolution(resolution, (960, 540))
            path = renders / f"preview-{self._render_counter:03d}.png"
            engine_preview = engine or self.cfg.preview_engine
            if engine_preview == "auto":
                engine_preview = "BLENDER_EEVEE_NEXT"
            result = self.bridge.render(
                path, camera=camera, engine=engine_preview,
                samples=samples or 24, resolution=res, frame=frame, timeout=900,
            )

        self._stage("preview" if mode != "final" else "delivering")
        images = [str(result)] if result.suffix.lower() in {".png", ".jpg"} else []
        return {
            "ok": True,
            "mode": mode,
            "path": self._rel(result),
            "absolute_path": str(result),
            "images": images,
        }

    # ------------------------------------------------------------------- misc
    def tool_save_document(self, name: str, content: str) -> dict:
        docs = self.project.root / "docs"
        docs.mkdir(exist_ok=True)
        filename = slugify(name, 50)
        if not filename.endswith(".md"):
            filename += ".md"
        path = docs / filename
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "path": self._rel(path)}

    def tool_project_status(self) -> dict:
        return {
            "ok": True,
            "brief": self.project.brief(),
            "stage": self.project.manifest["stage"],
            "blender": str(self.bridge.blender) if self.bridge.available else None,
            "reference_count": len(self.project.references()),
            "asset_count": len(self.project.assets()),
            "ai3d": {
                "preference": self.cfg.ai3d_provider,
                "configured": ai3d.available_providers(self.cfg),
            },
            "inbox_new": [f["name"] for f in self.project.inbox_new()],
            "files": {
                "blend": self._rel(self.project.blend_path),
                "manifest": self._rel(self.project.manifest_path),
            },
        }


def truncate_tool_payload(payload: dict, limit: int = 12000) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text
