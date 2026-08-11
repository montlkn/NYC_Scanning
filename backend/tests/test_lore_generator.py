"""
Unit tests for services/lore_generator.py — pure logic and the synthesis retry.

Scope is deliberately narrow: everything in this module that touches the DB is
left alone, and only the two things that can silently corrupt a building's
permanent record are covered here —

  * `_is_complete`, the gate that decides whether a generation is allowed to be
    cached at all, and
  * `_synthesize`'s truncation retry, which decides how many times we pay for a
    generation before giving up.

Both are worth pinning because a failure in either is *invisible*: a truncated
fragment caches as that building's story forever and still returns HTTP 200.

Skipped entirely when sqlalchemy is absent, matching this repo's philosophy
that nothing here should require live infra to pass.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Guard on the module itself, not on one dependency: it reaches sqlalchemy and
# pydantic_settings transitively, and either being absent should skip rather
# than error the whole collection.
lore = pytest.importorskip(
    "services.lore_generator",
    reason="requires the backend's runtime deps (sqlalchemy, pydantic-settings)",
)

import services.openai_text as openai_text_mod  # noqa: E402

_is_complete = lore._is_complete
_synthesize = lore._synthesize


MIN_LEN = 120


def sentence(n=MIN_LEN, end="."):
    """A string of exactly n characters ending in `end`."""
    return "x" * (n - len(end)) + end


# ── _is_complete: the cache gate ────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "   ", "\n\t "])
def test_empty_is_incomplete(value):
    assert _is_complete(value) is False


def test_the_regression_that_motivated_this_gate():
    """The 32-char fragment that the old `len > 30` check waved through."""
    assert _is_complete("At 1,454 feet tall including its") is False


def test_long_but_unterminated_is_incomplete():
    """Length alone cannot distinguish a cut-off generation from a good one."""
    assert _is_complete("x" * 500) is False
    assert _is_complete("x" * 500 + " including its") is False


def test_short_but_terminated_is_incomplete():
    """A complete sentence that is too short is still not lore."""
    assert _is_complete("A building.") is False


@pytest.mark.parametrize("end", [".", "!", "?", '"', "”", "’"])
def test_accepted_terminal_marks(end):
    assert _is_complete(sentence(end=end)) is True


def test_straight_apostrophe_is_not_accepted():
    """Documents actual behaviour: only the curly ’ closes a quote here."""
    assert _is_complete(sentence(end="'")) is False


@pytest.mark.parametrize("end", [",", ";", ":", "-", "—", "and", "x"])
def test_rejected_terminal_marks(end):
    assert _is_complete(sentence(end=end)) is False


def test_length_boundary_is_measured_after_stripping():
    assert _is_complete(sentence(MIN_LEN - 1)) is False
    assert _is_complete(sentence(MIN_LEN)) is True
    # Surrounding whitespace must not be counted toward the minimum...
    assert _is_complete("  " + sentence(MIN_LEN - 1) + "  ") is False
    # ...nor hide the terminal mark.
    assert _is_complete("  " + sentence(MIN_LEN) + "  \n") is True


def test_returns_bool_not_truthy_value():
    """Callers use it as a plain predicate; keep it honest."""
    assert isinstance(_is_complete("nope"), bool)
    assert isinstance(_is_complete(sentence()), bool)


# ── _synthesize: the truncation retry ───────────────────────────────────────

class FakeLLM:
    """Returns a scripted result per call, recording the max_tokens asked for."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, *, system, user, max_tokens=1200, **kw):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        return self.results.pop(0) if self.results else None

    @property
    def budgets(self):
        return [c["max_tokens"] for c in self.calls]


@pytest.fixture
def llm(monkeypatch):
    def install(*results):
        fake = FakeLLM(*results)
        monkeypatch.setattr(openai_text_mod, "openai_text", fake)
        return fake
    return install


async def synth(**kw):
    kw.setdefault("raw_text", "LPC designation report text.")
    kw.setdefault("building_name", "Chrysler Building")
    kw.setdefault("address", "405 Lexington Ave")
    kw.setdefault("year_built", "1930")
    kw.setdefault("style", "Art Deco")
    kw.setdefault("architect", "William Van Alen")
    return await _synthesize(**kw)


async def test_complete_first_pass_does_not_retry(llm):
    fake = llm(sentence())
    assert await synth() == sentence()
    assert fake.budgets == [1200]


async def test_result_is_stripped(llm):
    llm("  " + sentence() + "  ")
    assert await synth() == sentence()


async def test_truncated_first_pass_retries_with_larger_budget(llm):
    """The fix is more headroom, not another model."""
    fake = llm("At 1,454 feet tall including its", sentence())
    assert await synth() == sentence()
    assert fake.budgets == [1200, 2400]


async def test_two_truncations_give_up_rather_than_bill_forever(llm):
    fake = llm("At 1,454 feet tall including its", "Truncated again mid-")
    assert await synth() is None
    assert len(fake.calls) == 2


async def test_no_api_key_does_not_retry(llm):
    """None means unconfigured, not truncated — a retry would cost the same."""
    fake = llm(None)
    assert await synth() is None
    assert fake.budgets == [1200]


async def test_empty_result_does_not_retry(llm):
    fake = llm("")
    assert await synth() is None
    assert fake.budgets == [1200]


async def test_never_returns_raw_source_text(llm):
    """A designation report opens with OCR'd hearing boilerplate — and caches."""
    raw = "LANDMARKS PRESERVATION COMMISSION hearing boilerplate, " * 10
    llm(None)
    assert await synth(raw_text=raw) is None


async def test_prompt_carries_subject_and_metadata(llm):
    fake = llm(sentence())
    await synth()
    user = fake.calls[0]["user"]
    assert "Chrysler Building" in user
    assert "405 Lexington Ave" in user
    assert "William Van Alen" in user
    assert "LPC designation report text." in user


async def test_placeholder_building_name_is_dropped_from_metadata(llm):
    """'0' is a sentinel in the source data, not a name."""
    fake = llm(sentence())
    await synth(building_name="0")
    assert "0, 405 Lexington" not in fake.calls[0]["user"]


async def test_retry_reuses_the_same_prompt(llm):
    fake = llm("truncated mid-", sentence())
    await synth()
    assert fake.calls[0]["user"] == fake.calls[1]["user"]
    assert fake.calls[0]["system"] == fake.calls[1]["system"]
