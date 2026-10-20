"""Test doubles: a scripted stand-in for anthropic.Anthropic's streaming beta API."""

from __future__ import annotations

import itertools
from types import SimpleNamespace

import pytest

_ids = itertools.count(1)


def text(t: str):
    return SimpleNamespace(type="text", text=t)


def tool(name: str, **inp):
    return SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=inp)


def message(*blocks, stop_reason=None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=usage, stop_details=None)


class _Stream:
    def __init__(self, msg):
        self._msg = msg

    def __enter__(self):
        if isinstance(self._msg, Exception):
            raise self._msg
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._msg


class FakeClient:
    """Routes each request to a script chosen by `router(kwargs)`; records requests."""

    def __init__(self, scripts: dict[str, list], router=lambda kw: "default"):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.router = router
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        # snapshot messages: the agent keeps appending to the same list
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        key = self.router(kwargs)
        script = self.scripts[key]
        if not script:
            raise AssertionError(f"script '{key}' exhausted")
        item = script.pop(0)
        return _Stream(item(kwargs) if callable(item) else item)


@pytest.fixture
def sample_app():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "examples" / "sample-app"
