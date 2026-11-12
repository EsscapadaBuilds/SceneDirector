"""Project state: folders, manifest, references, assets, approval gates, chat log."""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import PROJECTS_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, max_len: int = 40) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return (text[:max_len] or "item").strip("-")


class Project:
    """A single scene-building project on disk."""

    SUBDIRS = [
        "references",
        "assets/models",
        "assets/hdris",
        "assets/textures",
        "assets/inbox",
        "scripts",
        "renders",
        "output",
        "characters",
    ]

    def __init__(self, name: str, create: bool = True):
        self.name = slugify(name)
        self.root = PROJECTS_DIR / self.name
        self.blend_path = self.root / "scene.blend"
        self.manifest_path = self.root / "manifest.json"
        self.chat_path = self.root / "chat.json"
        if create:
            self._init_dirs()
        self.manifest = self._load_manifest()

    # ------------------------------------------------------------------ setup
    def _init_dirs(self) -> None:
        for sub in self.SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    def _load_manifest(self) -> dict[str, Any]:
        if self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                data.setdefault("inbox_seen", [])
                return data
            except json.JSONDecodeError:
                pass
        manifest = {
            "name": self.name,
            "created": _now(),
            "updated": _now(),
            "stage": "intake",
            "gates": {},
            "references": [],
            "assets": [],
            "characters": [],
            "notes": [],
            "inbox_seen": [],
        }
        self.manifest = manifest
        if self.root.exists():
            self.save()
        return manifest

    def save(self) -> None:
        self.manifest["updated"] = _now()
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2), encoding="utf-8"
        )

    # -------------------------------------------------------------- references
    def add_reference(
        self,
        source_path: Path,
        kind: str,
        source: str,
        prompt: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Copy an image into the project and register it."""
        source_path = Path(source_path)
        ref_dir = self.root / "references"
        ref_dir.mkdir(parents=True, exist_ok=True)
        ext = source_path.suffix.lower() or ".png"
        stem = f"{kind}-{slugify(prompt or source_path.stem, 32)}-{int(time.time())}"
        dest = ref_dir / f"{stem}{ext}"
        counter = 1
        while dest.exists():
            dest = ref_dir / f"{stem}-{counter}{ext}"
            counter += 1
        shutil.copy2(source_path, dest)

        entry = {
            "id": dest.stem,
            "path": dest.relative_to(self.root).as_posix(),
            "kind": kind,
            "source": source,
            "prompt": prompt,
            "notes": notes,
            "approved": False,
            "created": _now(),
        }
        self.manifest["references"].append(entry)
        self.save()
        return entry

    def references(self, kind: str | None = None, approved_only: bool = False) -> list[dict]:
        refs = self.manifest["references"]
        if kind:
            refs = [r for r in refs if r["kind"] == kind]
        if approved_only:
            refs = [r for r in refs if r.get("approved")]
        return refs

    def approve_references(self, kind: str | None, ids: list[str] | None = None) -> int:
        count = 0
        for ref in self.manifest["references"]:
            if kind and ref["kind"] != kind:
                continue
            if ids and ref["id"] not in ids:
                continue
            ref["approved"] = True
            count += 1
        if count:
            self.save()
        return count

    def absolute(self, relative: str | Path) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self.root / p)

    # ------------------------------------------------------------------ assets
    def add_asset(
        self,
        name: str,
        kind: str,
        source: str,
        path: Path | str,
        license: str,
        url: str | None = None,
        meta: dict | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": slugify(name, 50),
            "name": name,
            "kind": kind,
            "source": source,
            "path": str(path),
            "license": license,
            "url": url,
            "meta": meta or {},
            "added": _now(),
        }
        # de-duplicate by path
        self.manifest["assets"] = [
            a for a in self.manifest["assets"] if a.get("path") != entry["path"]
        ]
        self.manifest["assets"].append(entry)
        self.save()
        return entry

    def assets(self, kind: str | None = None) -> list[dict]:
        assets = self.manifest["assets"]
        return [a for a in assets if a["kind"] == kind] if kind else assets

    # ------------------------------------------------------------------- inbox
    def inbox_dir(self) -> Path:
        path = self.root / "assets" / "inbox"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def inbox_files(self) -> list[dict]:
        """Files waiting in assets/inbox, with a change signature."""
        files = []
        for path in sorted(self.inbox_dir().iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            stat = path.stat()
            files.append({
                "name": path.name,
                "path": path.relative_to(self.root).as_posix(),
                "size": stat.st_size,
                "signature": f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}",
            })
        return files

    def inbox_new(self) -> list[dict]:
        """Files the user has dropped in since we last notified."""
        seen = set(self.manifest.get("inbox_seen", []))
        return [
            item for item in self.inbox_files()
            if item["signature"] not in seen and item["name"] not in seen
        ]

    def mark_inbox_seen(self, names: list[str] | None = None) -> None:
        seen = set(self.manifest.get("inbox_seen", []))
        for item in self.inbox_files():
            if names and item["name"] not in names:
                continue
            seen.add(item["signature"])
        self.manifest["inbox_seen"] = sorted(seen)[-200:]
        self.save()

    # ------------------------------------------------------------------- gates
    def set_gate(self, stage: str, approved: bool, notes: str | None = None) -> None:
        self.manifest["gates"][stage] = {
            "approved": approved,
            "notes": notes,
            "at": _now(),
        }
        if approved and stage.endswith("_reference"):
            kind = stage.replace("_reference", "")
            self.approve_references(kind)
        self.save()

    def gate(self, stage: str) -> dict | None:
        return self.manifest["gates"].get(stage)

    def is_approved(self, stage: str) -> bool:
        gate = self.gate(stage)
        return bool(gate and gate.get("approved"))

    # -------------------------------------------------------------------- chat
    def load_messages(self) -> list[dict]:
        if self.chat_path.exists():
            try:
                return json.loads(self.chat_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return []
        return []

    def save_messages(self, messages: list[dict]) -> None:
        self.chat_path.write_text(json.dumps(messages, indent=1), encoding="utf-8")

    # ----------------------------------------------------------------- summary
    def brief(self) -> str:
        """Compact project state for the agent's system prompt."""
        refs = self.manifest["references"]
        lines = [f"Project: {self.name}", f"Stage: {self.manifest['stage']}"]
        if refs:
            lines.append("References:")
            for r in refs[-12:]:
                status = "APPROVED" if r.get("approved") else "pending"
                lines.append(f"  - [{status}] {r['kind']}: {r['path']} ({r['source']})")
        else:
            lines.append("References: none yet")
        assets = self.manifest["assets"]
        if assets:
            lines.append(f"Assets ({len(assets)}):")
            for a in assets[-15:]:
                lines.append(f"  - {a['kind']}: {a['name']} -> {a['path']} [{a['license']}]")
        else:
            lines.append("Assets: none yet")
        gates = self.manifest["gates"]
        if gates:
            lines.append("Approval gates:")
            for stage, data in gates.items():
                lines.append(
                    f"  - {stage}: {'approved' if data.get('approved') else 'rejected'}"
                    + (f" ({data['notes']})" if data.get("notes") else "")
                )
        return "\n".join(lines)


def list_projects() -> list[str]:
    if not PROJECTS_DIR.exists():
        return []
    return sorted(p.name for p in PROJECTS_DIR.iterdir() if p.is_dir())
