"""
Unit tests for services/openai_text.py — the backend's only LLM client.

No network: `httpx.AsyncClient` is swapped for a recorder, so every test here
runs offline and asserts on the request we *would* have sent. That matters more
than usual for this module, because its two most important properties are
negative ones documented in its own docstring:

  * no `tools` key is ever sent (the Grok cost leak this client replaced), and
  * no `temperature` is ever sent (GPT-5.x reasoning models reject it).

A regression in either is invisible in production — the call still returns
prose, it just quietly costs more or 400s — so they are asserted directly on
the request body rather than inferred from the response.
"""

import os
import sys
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.openai_text as ot  # noqa: E402


# ── fake transport ──────────────────────────────────────────────────────────

class FakeResponse:
    """Minimal stand-in for httpx.Response covering what the client touches."""

    _NO_JSON = object()

    def __init__(self, status_code=200, json_body=_NO_JSON, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is self._NO_JSON:
            raise ValueError("not json")
        return self._json_body


class Recorder:
    """Scripts a sequence of responses and records every post() made."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def bodies(self):
        return [c["json"] for c in self.calls]

    def client_factory(recorder):  # noqa: N805 — closure over the recorder
        class FakeClient:
            def __init__(self, timeout=None):
                self.timeout = timeout
                recorder.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, headers=None, json=None):
                # Snapshot: the client reuses and mutates one body dict across
                # the reasoning retry, so holding the reference would make both
                # recorded calls look like the second one.
                recorder.calls.append(
                    {"url": url, "headers": headers, "json": deepcopy(json)}
                )
                if not recorder.responses:
                    raise AssertionError("unexpected extra POST")
                nxt = recorder.responses.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt

        return FakeClient


def install(monkeypatch, *responses, key="test-key"):
    """Wire a fake transport + API key, returning the recorder."""
    rec = Recorder(*responses)
    monkeypatch.setattr(ot, "OPENAI_API_KEY", key)
    monkeypatch.setattr(ot.httpx, "AsyncClient", rec.client_factory())
    return rec


def message(*texts, item_type="message"):
    return {"type": item_type, "content": [{"text": t} for t in texts]}


def ok(*items):
    return FakeResponse(200, {"output": list(items)})


# ── configuration ───────────────────────────────────────────────────────────

def test_is_configured_reflects_key(monkeypatch):
    monkeypatch.setattr(ot, "OPENAI_API_KEY", "sk-live")
    assert ot.is_configured() is True
    monkeypatch.setattr(ot, "OPENAI_API_KEY", "")
    assert ot.is_configured() is False


def test_assert_configured_logs_error_when_unset(monkeypatch, caplog):
    """The whole point of this function: a missing key must be loud, not silent."""
    monkeypatch.setattr(ot, "OPENAI_API_KEY", "")
    with caplog.at_level("INFO"):
        ot.assert_configured()
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert "OPENAI_API_KEY" in caplog.text


def test_assert_configured_names_model_when_set(monkeypatch, caplog):
    monkeypatch.setattr(ot, "OPENAI_API_KEY", "sk-live")
    monkeypatch.setattr(ot, "OPENAI_TEXT_MODEL", "gpt-5.6-luna")
    with caplog.at_level("INFO"):
        ot.assert_configured()
    assert not any(r.levelname == "ERROR" for r in caplog.records)
    assert "gpt-5.6-luna" in caplog.text


async def test_missing_key_returns_none_without_calling(monkeypatch):
    rec = install(monkeypatch, key="")
    assert await ot.openai_text(system="s", user="u") is None
    assert rec.calls == []


# ── request shape ───────────────────────────────────────────────────────────

async def test_never_sends_tools(monkeypatch):
    """The cost leak this client exists to close. See the module docstring."""
    rec = install(monkeypatch, ok(message("x")))
    await ot.openai_text(system="s", user="u")
    assert "tools" not in rec.bodies[0]
    assert "tool_choice" not in rec.bodies[0]
    assert "search_enabled" not in rec.bodies[0]


async def test_never_sends_temperature(monkeypatch):
    """GPT-5.x reasoning models reject `temperature` outright."""
    rec = install(monkeypatch, ok(message("x")))
    await ot.openai_text(system="s", user="u")
    assert "temperature" not in rec.bodies[0]


async def test_body_carries_model_prompt_and_limits(monkeypatch):
    monkeypatch.setattr(ot, "OPENAI_TEXT_MODEL", "gpt-5.6-luna")
    rec = install(monkeypatch, ok(message("x")))
    await ot.openai_text(
        system="SYS", user="USR", max_tokens=999, cache_key="jink-test"
    )
    body = rec.bodies[0]
    assert body["model"] == "gpt-5.6-luna"
    assert body["input"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "USR"},
    ]
    assert body["max_output_tokens"] == 999
    assert body["prompt_cache_key"] == "jink-test"
    assert body["reasoning"] == {"effort": "none"}


async def test_default_budget_leaves_headroom_after_reasoning(monkeypatch):
    """300 truncated lore mid-sentence; the default must stay well above it."""
    rec = install(monkeypatch, ok(message("x")))
    await ot.openai_text(system="s", user="u")
    assert rec.bodies[0]["max_output_tokens"] >= 1200


async def test_auth_header_and_endpoint(monkeypatch):
    rec = install(monkeypatch, ok(message("x")), key="sk-abc")
    await ot.openai_text(system="s", user="u")
    call = rec.calls[0]
    assert call["url"] == ot.OPENAI_URL
    assert call["headers"]["Authorization"] == "Bearer sk-abc"
    assert call["headers"]["Content-Type"] == "application/json"


async def test_timeout_is_forwarded(monkeypatch):
    rec = install(monkeypatch, ok(message("x")))
    await ot.openai_text(system="s", user="u", timeout_s=7.5)
    assert rec.timeout == 7.5


# ── response parsing ────────────────────────────────────────────────────────

async def test_returns_message_text(monkeypatch):
    install(monkeypatch, ok(message("  hello world  ")))
    assert await ot.openai_text(system="s", user="u") == "hello world"


async def test_concatenates_content_parts_and_messages(monkeypatch):
    install(monkeypatch, ok(message("a", "b"), message("c")))
    assert await ot.openai_text(system="s", user="u") == "abc"


async def test_skips_non_message_items(monkeypatch):
    """Responses API interleaves reasoning items; they are not prose."""
    install(monkeypatch, ok(
        message("ignored", item_type="reasoning"),
        message("kept"),
    ))
    assert await ot.openai_text(system="s", user="u") == "kept"


async def test_skips_content_parts_without_text(monkeypatch):
    resp = FakeResponse(200, {"output": [
        {"type": "message", "content": [{"annotations": []}, {"text": "only"}]}
    ]})
    install(monkeypatch, resp)
    assert await ot.openai_text(system="s", user="u") == "only"


@pytest.mark.parametrize("payload", [
    {"output": []},
    {"output": [message("   ")]},
    {"output": [message("", "")]},
    {},
])
async def test_empty_output_is_none_not_blank(monkeypatch, payload):
    """Callers branch on None; a blank string would cache as valid lore."""
    install(monkeypatch, FakeResponse(200, payload))
    assert await ot.openai_text(system="s", user="u") is None


# ── failure handling ────────────────────────────────────────────────────────

async def test_non_200_returns_none(monkeypatch):
    install(monkeypatch, FakeResponse(500, text="upstream boom"))
    assert await ot.openai_text(system="s", user="u") is None


async def test_transport_exception_returns_none(monkeypatch):
    rec = install(monkeypatch, RuntimeError("connection reset"))
    assert await ot.openai_text(system="s", user="u") is None
    assert len(rec.calls) == 1


async def test_bad_json_returns_none(monkeypatch):
    install(monkeypatch, FakeResponse(200))  # .json() raises
    assert await ot.openai_text(system="s", user="u") is None


# ── the reasoning-effort retry ──────────────────────────────────────────────

async def test_retries_without_reasoning_on_400(monkeypatch):
    """Losing the saving is acceptable; losing the lore is not."""
    rec = install(
        monkeypatch,
        FakeResponse(400, text="Unsupported value for 'reasoning.effort'"),
        ok(message("recovered")),
    )
    assert await ot.openai_text(system="s", user="u") == "recovered"
    assert len(rec.calls) == 2
    assert "reasoning" in rec.bodies[0]
    assert "reasoning" not in rec.bodies[1]


async def test_retry_preserves_everything_else(monkeypatch):
    rec = install(
        monkeypatch,
        FakeResponse(400, text="reasoning not supported"),
        ok(message("recovered")),
    )
    await ot.openai_text(system="s", user="u", cache_key="jink-keep")
    first, second = rec.bodies
    assert second["prompt_cache_key"] == "jink-keep"
    assert second["input"] == first["input"]
    assert second["max_output_tokens"] == first["max_output_tokens"]
    assert "tools" not in second


async def test_400_unrelated_to_reasoning_does_not_retry(monkeypatch):
    rec = install(monkeypatch, FakeResponse(400, text="invalid api key"))
    assert await ot.openai_text(system="s", user="u") is None
    assert len(rec.calls) == 1


async def test_retry_that_also_fails_returns_none(monkeypatch):
    rec = install(
        monkeypatch,
        FakeResponse(400, text="reasoning effort rejected"),
        FakeResponse(400, text="still unhappy"),
    )
    assert await ot.openai_text(system="s", user="u") is None
    assert len(rec.calls) == 2


async def test_retry_transport_failure_returns_none(monkeypatch):
    install(
        monkeypatch,
        FakeResponse(400, text="REASONING unsupported"),
        RuntimeError("connection reset"),
    )
    assert await ot.openai_text(system="s", user="u") is None
