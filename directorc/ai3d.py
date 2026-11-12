"""AI image-to-3D providers (Tripo and Meshy).

Both are credit-based services with official APIs:

  Tripo  POST /v3/files (multipart) -> file_token
         POST /v3/generation/image-to-model {"input": token, "texture": true}
         GET  /v3/tasks/{id} -> status/output.model_url
  Meshy  POST /openapi/v1/image-to-3d {"image_url": data-uri}
         GET  /openapi/v1/image-to-3d/{id} -> model_urls.glb

Only enabled when the matching API key is configured. Without a key the agent
falls back to the manual inbox workflow.
"""
from __future__ import annotations

import base64
import mimetypes
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

from .config import Config
from .state import Project

MESHY_BASE = "https://api.meshy.ai/openapi/v1"
TRIPO_BASE = "https://openapi.tripo3d.ai/v3"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SceneDirector/0.1"})


class AI3DError(RuntimeError):
    pass


@dataclass
class MeshResult:
    provider: str
    path: Path
    task_id: str
    thumbnail: Path | None = None
    meta: dict = field(default_factory=dict)


def available_providers(cfg: Config) -> list[str]:
    providers = []
    if cfg.tripo_api_key:
        providers.append("tripo")
    if cfg.meshy_api_key:
        providers.append("meshy")
    return providers


def pick_provider(cfg: Config, requested: str | None = None) -> str:
    requested = (requested or cfg.ai3d_provider or "auto").lower()
    if requested == "manual":
        raise AI3DError(
            "AI 3D generation is set to manual. Drop a generated .glb/.fbx into "
            "assets/inbox and import it."
        )
    if requested not in {"auto", "tripo", "meshy"}:
        raise AI3DError(f"Unknown AI3D provider: {requested}")
    if requested != "auto":
        key = cfg.tripo_api_key if requested == "tripo" else cfg.meshy_api_key
        if not key:
            raise AI3DError(
                f"{requested} selected but {requested.upper()}_API_KEY is not set in .env."
            )
        return requested
    providers = available_providers(cfg)
    if not providers:
        raise AI3DError(
            "No image-to-3D provider configured. Set TRIPO_API_KEY or MESHY_API_KEY "
            "in .env, or keep using the manual inbox workflow."
        )
    return providers[0]


def generate_mesh(
    project: Project,
    cfg: Config,
    image_path: Path,
    provider: str | None = None,
    progress: Callable[[str], None] | None = None,
    timeout: int | None = None,
) -> MeshResult:
    """Generate a textured GLB from a reference image and register it."""
    image_path = Path(image_path).resolve()
    if not image_path.exists():
        raise AI3DError(f"Reference image not found: {image_path}")
    chosen = pick_provider(cfg, provider)
    progress = progress or (lambda _msg: None)
    deadline = time.time() + (timeout or cfg.ai3d_timeout)

    folder = project.root / "assets" / "models" / f"{chosen}-{time.time_ns()}"
    folder.mkdir(parents=True, exist_ok=True)

    if chosen == "tripo":
        try:
            result = _tripo(cfg, image_path, folder, progress, deadline)
        except requests.RequestException as exc:
            raise AI3DError(f"Tripo request failed: {exc}") from exc
    else:
        try:
            result = _meshy(cfg, image_path, folder, progress, deadline)
        except requests.RequestException as exc:
            raise AI3DError(f"Meshy request failed: {exc}") from exc

    project.add_asset(
        name=f"{chosen}-generated-{int(time.time())}",
        kind="model",
        source=f"{chosen}-api",
        path=result.path.relative_to(project.root).as_posix(),
        license="AI-generated (check provider terms)",
        url=None,
        meta={"task_id": result.task_id, "reference": image_path.name,
              **result.meta},
    )
    return result


# --------------------------------------------------------------------- helpers
def _data_uri(image_path: Path) -> str:
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _download(url: str, dest: Path) -> Path:
    with SESSION.get(url, stream=True, timeout=300) as resp:
        if resp.status_code >= 400:
            raise AI3DError(f"Download failed (HTTP {resp.status_code}) for {url[:120]}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(1024 * 256):
                if chunk:
                    fh.write(chunk)
    return dest


def _check_deadline(deadline: float, task_id: str) -> None:
    if time.time() > deadline:
        raise AI3DError(
            f"Timed out waiting for task {task_id}. The provider may still finish; "
            "check its dashboard, then download the model into assets/inbox."
        )


# ---------------------------------------------------------------------- meshy
def _meshy(cfg: Config, image_path: Path, folder: Path,
           progress: Callable[[str], None], deadline: float) -> MeshResult:
    headers = {"Authorization": f"Bearer {cfg.meshy_api_key}"}
    size_mb = image_path.stat().st_size / 1e6
    if size_mb > 20:
        raise AI3DError(f"Meshy accepts images up to 20 MB (this one is {size_mb:.1f} MB)")

    payload = {
        "image_url": _data_uri(image_path),
        "target_formats": ["glb"],
        "enable_pbr": True,
        "should_remesh": True,
        "target_polycount": 30000,
        "ai_model": "latest",
    }
    progress("meshy: creating image-to-3D task")
    resp = SESSION.post(f"{MESHY_BASE}/image-to-3d", headers=headers,
                        json=payload, timeout=120)
    if resp.status_code == 402:
        raise AI3DError(
            "Meshy rejected the task: the free plan cannot create API tasks. "
            "Use Tripo, upgrade Meshy, or generate manually and drop the file in assets/inbox."
        )
    if resp.status_code >= 400:
        raise AI3DError(f"Meshy error HTTP {resp.status_code}: {resp.text[:500]}")
    task_id = resp.json().get("result")
    if not task_id:
        raise AI3DError(f"Meshy did not return a task id: {resp.text[:300]}")

    progress(f"meshy: task {task_id} queued")
    while True:
        _check_deadline(deadline, task_id)
        poll = SESSION.get(f"{MESHY_BASE}/image-to-3d/{task_id}",
                           headers=headers, timeout=60)
        if poll.status_code >= 400:
            raise AI3DError(f"Meshy poll failed HTTP {poll.status_code}: {poll.text[:300]}")
        data = poll.json()
        status = str(data.get("status", "")).upper()
        progress(f"meshy: {status.lower()} {data.get('progress', 0)}%")
        if status == "SUCCEEDED":
            break
        if status in {"FAILED", "CANCELED"}:
            raise AI3DError(f"Meshy task {task_id} {status.lower()}: {data.get('task_error')}")
        time.sleep(5)

    glb_url = (data.get("model_urls") or {}).get("glb")
    if not glb_url:
        raise AI3DError(f"Meshy task {task_id} succeeded without a GLB url")
    model_path = _download(glb_url, folder / "model.glb")

    thumbnail = None
    thumb_url = data.get("thumbnail_url")
    if thumb_url:
        try:
            thumbnail = _download(thumb_url, folder / "preview.png")
        except (AI3DError, requests.RequestException):
            thumbnail = None

    return MeshResult(
        provider="meshy", path=model_path, task_id=task_id, thumbnail=thumbnail,
        meta={"consumed_credits": data.get("consumed_credits")},
    )


# ---------------------------------------------------------------------- tripo
def _tripo(cfg: Config, image_path: Path, folder: Path,
           progress: Callable[[str], None], deadline: float) -> MeshResult:
    headers = {"Authorization": f"Bearer {cfg.tripo_api_key}"}
    mime = mimetypes.guess_type(image_path.name)[0] or "image/png"

    progress("tripo: uploading reference image")
    with image_path.open("rb") as fh:
        upload = SESSION.post(
            f"{TRIPO_BASE}/files", headers=headers,
            files={"file": (image_path.name, fh, mime)}, timeout=120,
        )
    if upload.status_code >= 400:
        raise AI3DError(f"Tripo upload failed HTTP {upload.status_code}: {upload.text[:400]}")
    token = (upload.json().get("data") or {}).get("file_token")
    if not token:
        raise AI3DError(f"Tripo did not return a file_token: {upload.text[:300]}")

    payload = {"input": token, "texture": True, "pbr": True}
    if cfg.tripo_model:
        payload["model"] = cfg.tripo_model

    progress("tripo: creating image-to-model task")
    create = SESSION.post(f"{TRIPO_BASE}/generation/image-to-model",
                          headers=headers, json=payload, timeout=120)
    if create.status_code >= 400:
        raise AI3DError(f"Tripo error HTTP {create.status_code}: {create.text[:500]}")
    body = create.json()
    if body.get("code") not in (0, None):
        raise AI3DError(f"Tripo error: {body}")
    task_id = (body.get("data") or {}).get("task_id") or body.get("task_id")
    if not task_id:
        raise AI3DError(f"Tripo did not return a task id: {create.text[:300]}")

    progress(f"tripo: task {task_id} queued")
    while True:
        _check_deadline(deadline, task_id)
        poll = SESSION.get(f"{TRIPO_BASE}/tasks/{task_id}", headers=headers, timeout=60)
        if poll.status_code >= 400:
            raise AI3DError(f"Tripo poll failed HTTP {poll.status_code}: {poll.text[:300]}")
        body = poll.json()
        data = body.get("data") or {}
        status = str(data.get("status", "")).lower()
        progress(f"tripo: {status} {data.get('progress', 0)}%")
        if status == "success":
            break
        if status in {"failed", "cancelled", "banned", "expired"}:
            raise AI3DError(f"Tripo task {task_id} {status}: {data.get('error_code') or body}")
        time.sleep(3)

    output = data.get("output") or {}
    url = output.get("model_url") or output.get("pbr_model_url")
    if not url:
        raise AI3DError(f"Tripo task {task_id} succeeded without a model url")
    model_path = _download(url, folder / "model.glb")

    thumbnail = None
    thumb_url = output.get("rendered_image_url")
    if thumb_url:
        try:
            thumbnail = _download(thumb_url, folder / "preview.png")
        except (AI3DError, requests.RequestException):
            thumbnail = None

    return MeshResult(provider="tripo", path=model_path, task_id=task_id,
                      thumbnail=thumbnail, meta={"model": cfg.tripo_model or "default"})
