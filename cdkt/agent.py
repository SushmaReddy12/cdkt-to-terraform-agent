"""A tool-using agent loop over the Claude Messages API.

Written as a manual loop (rather than the SDK tool runner) because the loop
needs to: stop on a terminal tool call and return its input, handle
``max_tokens`` truncation of large file writes, retry streams whose partial
tool JSON fails to parse, and nudge an agent that ends its turn without
calling its terminal tool.

The conversation is append-only (thinking blocks and compaction blocks are
passed back exactly as received), which keeps the prompt cache warm and
preserved thinking valid.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from .tools import TERMINAL_TOOLS, ToolBox, ToolError

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # fallbacks="default": re-run policy declines on a fallback model
COMPACTION_BETA = "compact-2026-01-12"  # server-side compaction for long conversions
MAX_STREAM_RETRIES = 2
MAX_NUDGES = 2


class AgentError(RuntimeError):
    pass


@dataclass
class AgentResult:
    terminal_tool: str | None
    terminal_input: dict | None
    final_text: str
    turns: int
    usage: dict = field(default_factory=dict)


@dataclass
class Agent:
    client: anthropic.Anthropic
    name: str
    system: str
    toolbox: ToolBox
    model: str
    effort: str = "high"
    max_turns: int = 80
    max_tokens: int = 64000
    use_fallbacks: bool = True
    use_compaction: bool = True
    log: Callable[[str], None] = print

    def _system_blocks(self) -> list[dict]:
        # One stable, cached system block: identical across all agents of the same
        # role, so parallel stack converters share the cache.
        return [{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}]

    def request_params(self, messages: list) -> dict:
        params: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self._system_blocks(),
            "tools": self.toolbox.definitions(streaming=True),
            "messages": messages,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        betas = []
        if self.use_fallbacks:
            params["fallbacks"] = "default"
            betas.append(FALLBACK_BETA)
        if self.use_compaction:
            params["context_management"] = {"edits": [{"type": "compact_20260112"}]}
            betas.append(COMPACTION_BETA)
        if betas:
            params["betas"] = betas
        return params

    def _request(self, messages: list) -> Any:
        last_exc: Exception | None = None
        for attempt in range(MAX_STREAM_RETRIES + 1):
            try:
                with self.client.beta.messages.stream(**self.request_params(messages)) as stream:
                    return stream.get_final_message()
            except ValueError as exc:
                # The SDK raises ValueError when streamed tool JSON can't be parsed.
                # There is no tool_use_id to answer, so re-issue the request.
                last_exc = exc
                self.log(f"  [{self.name}] malformed tool JSON in stream, retrying ({attempt + 1})")
        raise AgentError(f"{self.name}: stream failed repeatedly: {last_exc}")

    def run(self, task: str) -> AgentResult:
        messages: list[dict] = [{"role": "user", "content": task}]
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        nudges = 0
        final_text = ""

        for turn in range(1, self.max_turns + 1):
            response = self._request(messages)
            for key in usage:
                usage[key] += getattr(response.usage, key, 0) or 0

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise AgentError(f"{self.name}: request declined ({getattr(details, 'category', None)})")

            # Always append the full content (thinking, compaction, fallback blocks included).
            messages.append({"role": "assistant", "content": response.content})
            texts = [b.text for b in response.content if b.type == "text"]
            if texts:
                final_text = texts[-1]
                self.log(f"  [{self.name}] {final_text.strip()[:200]}")
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            if response.stop_reason == "pause_turn":
                continue

            if response.stop_reason == "max_tokens":
                if tool_uses:
                    # A truncated tool input can parse as a valid-looking partial object;
                    # never run it.
                    messages.append({"role": "user", "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "is_error": True,
                            "content": "Not executed: your response hit max_tokens and this call was cut off. "
                                       "Split the content into smaller files and write them one per call.",
                        }
                        for b in tool_uses
                    ]})
                else:
                    messages.append({"role": "user", "content": "Continue from where you stopped."})
                continue

            if not tool_uses:
                if nudges >= MAX_NUDGES:
                    return AgentResult(None, None, final_text, turn, usage)
                nudges += 1
                terminal = [n for n in self.toolbox.names if n in TERMINAL_TOOLS]
                messages.append({"role": "user", "content":
                    f"You ended your turn without calling {' or '.join(terminal)}. If the work is complete, "
                    f"call it now; otherwise keep going."})
                continue

            # Run ordinary tools first so a terminal call in the same turn sees their effects.
            results = [self._execute(b) for b in tool_uses if b.name not in TERMINAL_TOOLS]
            for block in (b for b in tool_uses if b.name in TERMINAL_TOOLS):
                try:
                    self.toolbox.call(block.name, block.input)
                except ToolError as exc:
                    results.append(_error_result(block.id, str(exc)))
                    continue
                self.log(f"  [{self.name}] {block.name} after {turn} turns")
                return AgentResult(block.name, dict(block.input), final_text, turn, usage)
            # All results for this turn go back in one user message.
            messages.append({"role": "user", "content": results})

        raise AgentError(f"{self.name}: gave up after {self.max_turns} turns without finishing")

    def _execute(self, block) -> dict:
        if not isinstance(block.input, dict):
            return _error_result(block.id, f'{{"INVALID_JSON": {str(block.input)[:2000]!r}}}')
        try:
            output = self.toolbox.call(block.name, block.input)
        except ToolError as exc:
            return _error_result(block.id, str(exc))
        except Exception as exc:  # a bug in a handler shouldn't kill the whole conversion
            return _error_result(block.id, f"internal tool error: {type(exc).__name__}: {exc}")
        return {"type": "tool_result", "tool_use_id": block.id, "content": output}


def _error_result(tool_use_id: str, message: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True, "content": message}
