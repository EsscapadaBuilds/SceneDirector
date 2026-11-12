"""The agent conversation loop."""
from __future__ import annotations

import json
from typing import Callable

from ..blender.bridge import Bridge
from ..config import Config
from ..llm import LLM, LLMError, compact_messages, fit_to_budget, image_to_data_url
from ..state import Project
from .prompts import system_prompt
from .tools import TOOLS, Toolbox, truncate_tool_payload

MAX_TOOL_ROUNDS = 14


class Agent:
    def __init__(
        self,
        project: Project,
        bridge: Bridge,
        cfg: Config,
        printer: Callable[[str], None] = print,
        tool_printer: Callable[[str], None] | None = None,
    ):
        self.project = project
        self.bridge = bridge
        self.cfg = cfg
        self.llm = LLM(cfg)
        self.print = printer
        self.tool_print = tool_printer or (lambda msg: printer(f"  {msg}"))
        self.box = Toolbox(project, bridge, cfg, progress=self.tool_print)
        self.messages: list[dict] = project.load_messages()

    # ------------------------------------------------------------------ helpers
    def _system_message(self) -> dict:
        return {"role": "system", "content": system_prompt(self.project, self.bridge, self.cfg)}

    def _append_observations(self, images: list[str]) -> None:
        if not images or not self.cfg.llm_vision:
            return
        unique = list(dict.fromkeys(images))[-1:]
        parts: list[dict] = [{
            "type": "text",
            "text": "Latest rendered image(s) from the tools. Inspect them before continuing:",
        }]
        for path in unique:
            try:
                url = image_to_data_url(path)
            except OSError:
                continue
            parts.append({"type": "image_url", "image_url": {"url": url}})
        self.messages.append({"role": "user", "content": parts})

    def _serialize(self) -> list[dict]:
        """Strip bulky image payloads before persisting the chat log."""
        out = []
        for msg in self.messages:
            if msg.get("role") == "system":
                continue
            copy = {"role": msg.get("role")}
            content = msg.get("content")
            if isinstance(content, list):
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                copy["content"] = ("\n".join(texts) + "\n[image attachment]").strip()
            else:
                copy["content"] = content or ""
            if msg.get("tool_calls"):
                copy["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                copy["tool_call_id"] = msg["tool_call_id"]
            out.append(copy)
        return out

    # --------------------------------------------------------------------- turn
    def send(self, text: str, attachments: list[str] | None = None) -> str:
        images = attachments or []
        if images and not self.cfg.llm_vision:
            names = ", ".join(images)
            text += f"\n\n[User attached reference image(s) at: {names}]"
            images = []

        self.messages.append(LLM.user_message(text, images))

        final_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            request_messages = [self._system_message()] + compact_messages(self.messages)
            request_messages = fit_to_budget(
                request_messages, TOOLS, self.cfg.llm_max_input_tokens
            )
            reply = self.llm.chat(request_messages, tools=TOOLS)
            message = reply.message
            self.messages.append(message)

            content = (message.get("content") or "").strip()
            if content:
                self.print(content)
                final_text = content

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                break

            observed_images: list[str] = []
            for call in tool_calls:
                name = call["function"]["name"]
                raw_args = call["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}
                    result = {"ok": False, "error": f"Invalid JSON arguments: {raw_args[:300]}"}
                else:
                    self.tool_print(f"{name}({self._preview_args(args)})")
                    result = self.box.execute(name, args)

                if isinstance(result, dict):
                    observed_images.extend(result.get("images") or [])

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": truncate_tool_payload(result),
                })

            self._append_observations(observed_images)
        else:
            note = "Tool budget for this turn is exhausted. Summarise progress and ask the user."
            self.print(note)
            final_text = note

        self.project.save_messages(self._serialize())
        return final_text

    @staticmethod
    def _preview_args(args: dict, limit: int = 60) -> str:
        bits = []
        for key, value in args.items():
            if key == "code":
                bits.append(f"code=<{len(str(value))} chars>")
            elif key == "content":
                bits.append(f"content=<{len(str(value))} chars>")
            else:
                text = str(value).replace("\n", " ")
                bits.append(f"{key}={text[:limit]}")
        return ", ".join(bits)


__all__ = ["Agent", "LLMError"]
