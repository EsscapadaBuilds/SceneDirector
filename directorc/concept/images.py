"""Concept reference image generation (free providers).

Default provider: Pollinations.ai — free, keyless, URL-based generation.

The anonymous tier allows 1 image every 45 seconds per IP. Multi-image requests
are paced automatically and 402 FREE_LIMIT responses are retried using the
server's wait hint. Set POLLINATIONS_API_KEY to use the authenticated endpoint.
"""
from __future__ import annotations

import math
import random
import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode

import requests

from ..config import Config

FREE_IMAGE_INTERVAL = 45.0
MAX_RATE_LIMIT_RETRIES = 2
_last_request_at = 0.0


class ImageGenError(RuntimeError):
    pass


def generate_images(
    cfg: Config,
    prompt: str,
    count: int = 3,
    out_dir: Path | None = None,
    seed: int | None = None,
    width: int = 1024,
    height: int = 1024,
    progress: Callable[[str], None] | None = None,
) -> list[Path]:
    """Generate `count` images for `prompt` and return their saved paths."""
    out_dir = out_dir or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    if cfg.image_provider == "none":
        raise ImageGenError(
            "Image generation is disabled (IMAGE_PROVIDER=none). "
            "Ask the user to upload reference images instead."
        )

    paths: list[Path] = []
    for i in range(max(1, count)):
        if i:
            _wait_for_free_slot(cfg, progress)
        img_seed = seed if seed is not None else random.randint(1, 2_000_000_000)
        if seed is not None:
            img_seed = seed + i
        path = out_dir / f"gen-{int(time.time())}-{i}-{img_seed}.jpg"
        _pollinations(cfg, prompt, path, img_seed, width, height, progress)
        paths.append(path)
    return paths


def _notify(progress: Callable[[str], None] | None, message: str) -> None:
    if progress:
        progress(message)


def _wait_for_free_slot(cfg: Config,
                        progress: Callable[[str], None] | None = None) -> None:
    """Space anonymous requests out so the next one is not rejected."""
    global _last_request_at
    if cfg.pollinations_api_key or not _last_request_at:
        return
    wait = FREE_IMAGE_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        _notify(
            progress,
            f"free image tier: waiting {math.ceil(wait)}s before the next image "
            f"(1 per {int(FREE_IMAGE_INTERVAL)}s)",
        )
        time.sleep(wait)


def _image_url(cfg: Config, prompt: str, seed: int, w: int, h: int) -> str:
    query = urlencode({
        "width": w,
        "height": h,
        "seed": seed,
        "nologo": "true",
        "model": cfg.pollinations_model,
    })
    if cfg.pollinations_api_key:
        return f"https://gen.pollinations.ai/image/{quote(prompt, safe='')}?{query}"
    return f"https://image.pollinations.ai/prompt/{quote(prompt, safe='')}?{query}"


def _error_message(resp: requests.Response) -> str:
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        return ""
    if isinstance(error, dict):
        return str(error.get("message") or "")[:300]
    return str(error)[:300]


def _is_free_limit(resp: requests.Response) -> bool:
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        return True  # a 402 without details is most likely this free limit
    return not isinstance(error, dict) or error.get("code") == "FREE_LIMIT"


def _free_limit_wait(resp: requests.Response) -> float:
    """Seconds to wait from Retry-After or the JSON 'Retry after Ns' hint."""
    header = resp.headers.get("retry-after", "")
    try:
        wait = float(header)
    except (TypeError, ValueError):
        match = re.search(r"retry after\s+([\d.]+)\s*s", resp.text, re.IGNORECASE)
        wait = float(match.group(1)) if match else FREE_IMAGE_INTERVAL
    return min(max(wait + 1.0, 1.0), 120.0)


def _pollinations(cfg: Config, prompt: str, dest: Path, seed: int, w: int, h: int,
                  progress: Callable[[str], None] | None = None) -> None:
    global _last_request_at

    url = _image_url(cfg, prompt, seed, w, h)
    headers = (
        {"Authorization": f"Bearer {cfg.pollinations_api_key}"}
        if cfg.pollinations_api_key else {}
    )

    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=120)
        except requests.RequestException as exc:
            raise ImageGenError(f"Concept image request failed: {exc}") from exc
        _last_request_at = time.monotonic()

        if resp.status_code == 402 and not _is_free_limit(resp):
            raise ImageGenError(
                "Pollinations rejected the image request (HTTP 402). "
                "If POLLINATIONS_API_KEY is set, check that it is valid and has "
                "credit; otherwise remove it to use the free tier. You can also "
                "upload a reference image instead."
            )

        if resp.status_code == 402 and attempt < MAX_RATE_LIMIT_RETRIES:
            wait = _free_limit_wait(resp)
            _notify(
                progress,
                f"free image tier is busy; retrying in {math.ceil(wait)}s "
                f"({attempt + 1}/{MAX_RATE_LIMIT_RETRIES})",
            )
            time.sleep(wait)
            continue

        if resp.status_code >= 400 or not resp.content:
            detail = _error_message(resp)
            if resp.status_code == 402:
                hint = ("The free tier allows 1 image per 45s per IP; wait a minute "
                        "and retry, upload a reference, or set POLLINATIONS_API_KEY.")
            else:
                hint = "Try again or upload a reference."
            raise ImageGenError(
                f"Pollinations returned HTTP {resp.status_code}"
                f"{': ' + detail if detail else ''}. {hint}"
            )

        ctype = resp.headers.get("content-type", "")
        if "image" not in ctype:
            raise ImageGenError(
                "Pollinations did not return an image "
                f"(content-type: {ctype}). Response: {resp.text[:300]}"
            )
        dest.write_bytes(resp.content)
        return


def build_prompt(subject: str, kind: str, style: str | None = None,
                 extra: str | None = None) -> str:
    """Compose a consistent concept-art prompt."""
    parts = [subject.strip()]
    if kind == "scene":
        parts.append("wide establishing shot, cinematic concept art")
    elif kind == "character":
        parts.append("full body character design, clear silhouette, neutral background")
    elif kind == "style":
        parts.append("style reference, mood board")
    if style:
        parts.append(style)
    if extra:
        parts.append(extra)
    parts.append("high detail, coherent composition")
    return ", ".join(parts)
