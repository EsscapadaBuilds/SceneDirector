"""Provider-agnostic LLM client for OpenAI-compatible chat endpoints.

Works with Ollama (http://localhost:11434/v1), Groq, OpenRouter, Gemini's
OpenAI-compatible endpoint, LM Studio, and any other /v1/chat/completions server.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import requests

from .config import Config

LLM_MAX_RETRIES = 2
RETRY_WAIT_CAP = 30.0


class LLMError(RuntimeError):
    pass


@dataclass
class LLMReply:
    message: dict
    raw: dict


class LLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.endpoint = cfg.llm_base_url.rstrip("/") + "/chat/completions"

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.4) -> LLMReply:
        payload: dict = {
            "model": self.cfg.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.cfg.llm_max_tokens > 0:
            payload["max_tokens"] = self.cfg.llm_max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self.cfg.llm_api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                self.endpoint, headers=headers, json=payload, timeout=self.cfg.llm_timeout
            )
        except requests.RequestException as exc:
            raise LLMError(
                f"LLM request to {self.endpoint} failed: {exc}. "
                "Check the endpoint, API key, and that the provider is reachable."
            ) from exc

        for attempt in range(LLM_MAX_RETRIES):
            if resp.status_code != 429:
                break
            wait = _retry_wait(resp)
            print(f"  · rate limited; retrying in {wait:.0f}s "
                  f"({attempt + 1}/{LLM_MAX_RETRIES})")
            time.sleep(wait)
            try:
                resp = requests.post(
                    self.endpoint, headers=headers, json=payload, timeout=self.cfg.llm_timeout
                )
            except requests.RequestException as exc:
                raise LLMError(
                    f"LLM request to {self.endpoint} failed: {exc}. "
                    "Check the endpoint, API key, and that the provider is reachable."
                ) from exc

        if resp.status_code >= 400:
            detail = resp.text[:1200]
            hint = ""
            if resp.status_code == 429:
                hint = (
                    "\n\nRate limit: if the message mentions output tokens per minute, "
                    f"lower LLM_MAX_TOKENS in .env (currently {self.cfg.llm_max_tokens}) "
                    "to 512-800 and retry. If it mentions requests or input tokens, wait "
                    "a minute or switch to another model."
                )
            raise LLMError(
                f"LLM returned HTTP {resp.status_code} from {self.endpoint}.\n{detail}{hint}"
            )

        try:
            data = resp.json()
            choice = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected LLM response: {resp.text[:800]}") from exc

        message = {
            "role": "assistant",
            "content": choice.get("content") or "",
        }
        if choice.get("tool_calls"):
            message["tool_calls"] = [
                {
                    "id": tc.get("id") or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"].get("arguments") or "{}",
                    },
                }
                for i, tc in enumerate(choice["tool_calls"])
            ]
        return LLMReply(message=message, raw=data)

    @staticmethod
    def user_message(text: str, images: list[str] | None = None) -> dict:
        """Build a user message, optionally with vision attachments (file paths)."""
        if not images:
            return {"role": "user", "content": text}
        parts: list[dict] = [{"type": "text", "text": text}]
        for path in images:
            parts.append({
                "type": "image_url",
                "image_url": {"url": image_to_data_url(path)},
            })
        return {"role": "user", "content": parts}


def _retry_wait(resp: requests.Response) -> float:
    """Seconds to wait before retrying a 429, from headers or the error text."""
    header = resp.headers.get("retry-after", "")
    try:
        wait = float(header)
    except (TypeError, ValueError):
        match = re.search(r"try again in ([\d.]+)s", resp.text)
        wait = float(match.group(1)) + 0.5 if match else 5.0
    return min(max(wait, 1.0), RETRY_WAIT_CAP)


def image_to_data_url(path: str) -> str:
    import base64
    import mimetypes
    from pathlib import Path

    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def fit_to_budget(messages: list[dict], tools: list[dict] | None,
                  max_tokens: int) -> list[dict]:
    """Keep the system prompt plus the newest messages that fit the input budget.

    A cheap chars/4 estimate is used, with a fixed allowance per image. This is
    approximate but good enough to avoid free-tier input-token walls.
    """
    if max_tokens <= 0 or estimate_request_tokens(messages, tools) <= max_tokens:
        return messages

    system = messages[0] if messages and messages[0].get("role") == "system" else None
    history = messages[1:] if system else messages
    kept: list[dict] = []
    for msg in reversed(history):
        trial = ([system] if system else []) + [msg] + kept
        if kept and estimate_request_tokens(trial, tools) > max_tokens:
            break
        kept.insert(0, msg)
    # A trimmed suffix must not start with orphaned tool results.
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)
    return ([system] if system else []) + kept


def estimate_request_tokens(messages: list[dict],
                            tools: list[dict] | None = None) -> int:
    def count_images(value) -> int:
        if isinstance(value, dict):
            return sum(
                1 if key == "url" and isinstance(item, str) and item.startswith("data:image")
                else count_images(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return sum(count_images(item) for item in value)
        return 0

    def scrub(value):
        if isinstance(value, dict):
            return {
                key: "[image]" if key == "url" and isinstance(item, str)
                and item.startswith("data:image") else scrub(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    images = count_images(messages)
    chars = len(json.dumps(scrub(messages), ensure_ascii=False))
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False))
    # Groq counts 2048 input tokens per image; use that conservative estimate.
    return chars // 4 + images * 2048


def compact_messages(messages: list[dict], keep_recent: int = 40) -> list[dict]:
    """Shrink old messages: drop base64 image payloads and very long tool output.

    The most recent user message keeps its images so the model can still see them.
    """
    last_user_image_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "user" and isinstance(msg.get("content"), list):
            last_user_image_idx = i
            break

    out: list[dict] = []
    for i, msg in enumerate(messages):
        msg = json.loads(json.dumps(msg))  # cheap deep copy

        content = msg.get("content")
        if isinstance(content, list) and i != last_user_image_idx:
            msg["content"] = [
                {"type": "text", "text": part.get("text", "")}
                if part.get("type") == "text"
                else {"type": "text", "text": "[image attached earlier]"}
                for part in content
            ]

        if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
            if len(msg["content"]) > 8000:
                msg["content"] = msg["content"][:8000] + "\n...[truncated]"

        out.append(msg)

    if len(out) > keep_recent:
        # never cut into the middle of an assistant tool-call exchange
        out = out[-keep_recent:]
        while out and out[0].get("role") == "tool":
            out.pop(0)
    return out
