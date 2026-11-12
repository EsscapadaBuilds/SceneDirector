"""Free asset providers: Poly Haven and ambientCG (both CC0), plus local inbox.

Poly Haven  - https://api.polyhaven.com   (models, HDRIs, textures, CC0)
ambientCG   - https://ambientcg.com/api/v2 (PBR materials, CC0)
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

from ..state import Project

POLYHAVEN = "https://api.polyhaven.com"
AMBIENTCG = "https://ambientcg.com/api/v2/full_json"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "SceneDirector/0.1 (local tooling)"})


class AssetError(RuntimeError):
    pass


# --------------------------------------------------------------------- helpers
def _get_json(url: str, params: dict | None = None) -> Any:
    resp = SESSION.get(url, params=params, timeout=30)
    if resp.status_code >= 400:
        raise AssetError(f"HTTP {resp.status_code} from {url}: {resp.text[:300]}")
    return resp.json()


def _download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with SESSION.get(url, stream=True, timeout=120) as resp:
        if resp.status_code >= 400:
            raise AssetError(f"Download failed (HTTP {resp.status_code}): {url}")
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
        tmp.replace(dest)
    return dest


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1]


# ---------------------------------------------------------------- poly haven
def _polyhaven_catalog(kind: str) -> dict:
    type_map = {"model": "models", "hdri": "hdris", "texture": "textures"}
    return _get_json(f"{POLYHAVEN}/assets", params={"t": type_map[kind]})


def search_polyhaven(query: str, kind: str, limit: int = 8) -> list[dict]:
    catalog = _polyhaven_catalog(kind)
    words = _tokens(query)
    scored = []
    for asset_id, meta in catalog.items():
        haystack = " ".join(
            [asset_id, meta.get("name", ""), " ".join(meta.get("categories", [])),
             " ".join(meta.get("tags", [])), meta.get("description", "")[:300]]
        ).lower()
        score = sum(3 if w in asset_id.lower() else 1 for w in words if w in haystack)
        if score <= 0 and words:
            continue
        scored.append((score, asset_id, meta))
    scored.sort(key=lambda x: (-x[0], x[1]))
    results = []
    for score, asset_id, meta in scored[:limit]:
        results.append({
            "provider": "polyhaven",
            "id": asset_id,
            "name": meta.get("name", asset_id),
            "kind": kind,
            "tags": meta.get("tags", [])[:10],
            "categories": meta.get("categories", []),
            "dimensions": meta.get("dimensions"),
            "polycount": meta.get("polycount"),
            "license": "CC0",
        })
    return results


def download_polyhaven(project: Project, asset_id: str, kind: str,
                       resolution: str = "1k") -> dict:
    files = _get_json(f"{POLYHAVEN}/files/{asset_id}")

    if kind == "model":
        gltf = _pick_gltf(files, resolution)
        folder = project.root / "assets" / "models" / asset_id
        folder.mkdir(parents=True, exist_ok=True)
        main = folder / f"{asset_id}.gltf"
        _download(gltf["url"], main)
        for rel, info in (gltf.get("include") or {}).items():
            if isinstance(info, dict) and info.get("url"):
                _download(info["url"], folder / rel)
        entry = project.add_asset(
            name=asset_id, kind="model", source="polyhaven",
            path=folder.relative_to(project.root).as_posix(),
            license="CC0", url=f"https://polyhaven.com/a/{asset_id}",
            meta={"import": "gltf", "resolution": resolution},
        )
        return entry

    if kind == "hdri":
        node = files.get("hdri") or {}
        candidates = node.get(resolution) or node.get("1k") or {}
        url = _dig_url(candidates, ["hdr", "exr"])
        if not url:
            raise AssetError(f"No HDRI file found for {asset_id}")
        dest = project.root / "assets" / "hdris" / f"{asset_id}_{resolution}.hdr"
        _download(url, dest)
        return project.add_asset(
            name=asset_id, kind="hdri", source="polyhaven",
            path=dest.relative_to(project.root).as_posix(), license="CC0",
            url=f"https://polyhaven.com/a/{asset_id}", meta={"resolution": resolution},
        )

    if kind == "texture":
        maps = {"Diffuse": "diffuse", "nor_gl": "normal", "Rough": "roughness",
                "arm": "arm", "Displacement": "displacement"}
        folder = project.root / "assets" / "textures" / asset_id
        folder.mkdir(parents=True, exist_ok=True)
        saved = []
        for map_name, label in maps.items():
            node = files.get(map_name)
            if not node:
                continue
            res_node = node.get(resolution) or node.get("1k") or {}
            url = _dig_url(res_node, ["jpg", "png", "exr"])
            if url:
                dest = folder / f"{asset_id}_{label}{Path(url).suffix}"
                _download(url, dest)
                saved.append(label)
        if not saved:
            raise AssetError(f"No texture maps found for {asset_id}")
        return project.add_asset(
            name=asset_id, kind="texture", source="polyhaven",
            path=folder.relative_to(project.root).as_posix(), license="CC0",
            url=f"https://polyhaven.com/a/{asset_id}", meta={"maps": saved},
        )

    raise AssetError(f"Unknown Poly Haven kind: {kind}")


def _pick_gltf(files: dict, resolution: str) -> dict:
    node = files.get("gltf") or {}
    for res in (resolution, "1k", "2k", "4k"):
        if res in node:
            inner = node[res].get("gltf")
            if isinstance(inner, dict) and inner.get("url"):
                return inner
    # fallback: search any nested 'gltf' dict with a url
    for res_node in node.values():
        for key, value in (res_node or {}).items():
            if key == "gltf" and isinstance(value, dict) and value.get("url"):
                return value
    raise AssetError("No glTF file found for this model")


def _dig_url(node: Any, preferred_keys: list[str]) -> str | None:
    """Find a download URL inside nested Poly Haven file structures."""
    if isinstance(node, dict):
        for key in preferred_keys:
            if key in node:
                found = _dig_url(node[key], preferred_keys)
                if found:
                    return found
        if node.get("url"):
            return node["url"]
        for value in node.values():
            if isinstance(value, (dict, list)):
                found = _dig_url(value, preferred_keys)
                if found:
                    return found
    elif isinstance(node, list):
        for value in node:
            found = _dig_url(value, preferred_keys)
            if found:
                return found
    return None


# ------------------------------------------------------------------ ambientcg
def search_ambientcg(query: str, limit: int = 8) -> list[dict]:
    try:
        data = _get_json(AMBIENTCG, params={
            "type": "Material", "limit": limit, "q": query,
            "include": "downloadData,previewData",
        })
    except (AssetError, ValueError) as exc:
        raise AssetError(f"ambientCG search failed: {exc}") from exc
    results = []
    for item in (data.get("foundAssets") or [])[:limit]:
        results.append({
            "provider": "ambientcg",
            "id": item.get("assetId"),
            "name": item.get("displayName") or item.get("assetId"),
            "kind": "texture",
            "tags": item.get("tags", [])[:10],
            "license": "CC0",
        })
    return results


def download_ambientcg(project: Project, asset_id: str, resolution: str = "1K") -> dict:
    data = _get_json(AMBIENTCG, params={
        "type": "Material", "q": asset_id, "limit": 6,
        "include": "downloadData",
    })
    match = None
    for item in data.get("foundAssets") or []:
        if item.get("assetId", "").lower() == asset_id.lower():
            match = item
            break
    if not match:
        raise AssetError(f"ambientCG asset '{asset_id}' not found")

    folders = (match.get("downloadFolders") or {}).get("default", {})
    zips = ((folders.get("downloadFiletypeCategories") or {}).get("zip") or {}).get("downloads", [])
    wanted = f"{resolution}-JPG".upper()
    download = next(
        (d for d in zips if d.get("attribute", "").upper() == wanted),
        zips[0] if zips else None,
    )
    if not download:
        raise AssetError(f"No zip download available for {asset_id}")

    folder = project.root / "assets" / "textures" / asset_id
    folder.mkdir(parents=True, exist_ok=True)
    zip_path = folder / download["fileName"]
    _download(download["fullDownloadPath"], zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(folder)
    zip_path.unlink(missing_ok=True)

    return project.add_asset(
        name=asset_id, kind="texture", source="ambientcg",
        path=folder.relative_to(project.root).as_posix(), license="CC0",
        url=f"https://ambientcg.com/view?id={asset_id}",
        meta={"resolution": resolution},
    )


# --------------------------------------------------------------------- generic
def download_url(project: Project, url: str, filename: str, kind: str,
                 license: str = "unknown") -> dict:
    dest_dir = {
        "model": project.root / "assets" / "models",
        "hdri": project.root / "assets" / "hdris",
        "texture": project.root / "assets" / "textures",
    }.get(kind, project.root / "assets" / "models")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    _download(url, dest)
    return project.add_asset(
        name=dest.stem, kind=kind, source="url", path=dest.relative_to(project.root).as_posix(),
        license=license, url=url,
    )


def scan_inbox(project: Project) -> list[dict]:
    """List files the user dropped into assets/inbox, ready to import."""
    inbox = project.root / "assets" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(inbox.iterdir()):
        if p.is_file():
            out.append({"name": p.name, "path": p.relative_to(project.root).as_posix(),
                        "size": p.stat().st_size})
    return out


def import_inbox(project: Project) -> list[dict]:
    """Register inbox files as assets (user-provided, license must be confirmed)."""
    entries = []
    for item in scan_inbox(project):
        path = project.absolute(item["path"])
        kind = _guess_kind(path)
        entries.append(project.add_asset(
            name=path.stem, kind=kind, source="user-upload",
            path=item["path"], license="user-provided (confirm rights)",
        ))
    return entries


def _guess_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".hdr", ".exr"}:
        return "hdri"
    if ext in {".glb", ".gltf", ".fbx", ".obj", ".blend", ".usd", ".usdz", ".stl"}:
        return "model"
    if ext in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}:
        return "texture"
    return "model"


def search_all(query: str, kind: str, limit: int = 8) -> list[dict]:
    """Search every applicable free provider."""
    results: list[dict] = []
    errors: list[str] = []
    if kind in {"model", "hdri", "texture"}:
        try:
            results += search_polyhaven(query, kind, limit=limit)
        except Exception as exc:  # noqa: BLE001 - surface provider issues to the agent
            errors.append(f"polyhaven: {exc}")
    if kind == "texture":
        try:
            results += search_ambientcg(query, limit=limit)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ambientcg: {exc}")
    if errors:
        for res in results:
            res["provider_errors"] = errors
    return results[: limit * 2]
