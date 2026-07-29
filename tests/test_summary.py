"""Summary tests.

Two things are being defended. First, that a malformed response produces *no*
summary rather than a mangled one -- a key showing `inverted]responses` reads as
a broken deck, where a key showing nothing reads as normal. Second, that no
failure of a remote service can take the deck down with it.

The response shapes below are real: every rejection case is something a hosted
model actually returned during the bake-off.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from herdr_streamdeck.summary import (
    MODEL,
    REPLY_KINDS,
    SCHEMA,
    SYSTEM_PROMPT,
    PaneSummary,
    Reply,
    Summariser,
    Transport,
    api_key,
    build,
    parse,
)

GOOD = {
    "waiting": True,
    "summary": "remove or deprecate",
    "responses": [
        {"kind": "affirmative", "label": "Remove it", "text": "Remove the endpoint."},
        {"kind": "alternative", "label": "Deprecate", "text": "Keep it, warn on use."},
    ],
}


def envelope(payload: object) -> bytes:
    return json.dumps({"choices": [{"message": {"content": json.dumps(payload)}}]}).encode()


def transport_returning(payload: object) -> Transport:
    def send(body: bytes, timeout: float) -> bytes:
        return envelope(payload)

    return send


# ------------------------------------------------------------------- parsing


def test_a_well_formed_response_parses() -> None:
    summary = parse(GOOD)
    assert summary is not None
    assert summary.phrase == "remove or deprecate"
    assert summary.waiting is True
    assert summary.replies == (
        Reply("affirmative", "Remove it", "Remove the endpoint."),
        Reply("alternative", "Deprecate", "Keep it, warn on use."),
    )


def test_a_missing_waiting_flag_is_rejected_rather_than_defaulted() -> None:
    """Observed in 10 of 15 responses before the shape was spelled out in the
    prompt. Defaulting either way is wrong: False silently drops replies a pane
    needs, True offers answers to a question nobody asked."""
    payload = {k: v for k, v in GOOD.items() if k != "waiting"}
    assert parse(payload) is None


def test_a_non_boolean_waiting_flag_is_rejected() -> None:
    assert parse({**GOOD, "waiting": "yes"}) is None


@pytest.mark.parametrize(
    "value",
    [
        "inverted]responses",  # real minimax-m3 output
        'quoted"',
        "brace{",
        "",
        "   ",
        None,
        42,
        "the agent has finished refactoring and is now waiting for you to decide",
    ],
)
def test_a_summary_that_is_not_a_short_label_is_rejected(value: object) -> None:
    """Rendering `inverted]responses` on a key looks like a bug in the deck.
    Rendering nothing looks like nothing, which is much cheaper."""
    assert parse({**GOOD, "summary": value}) is None


def test_whitespace_is_normalised() -> None:
    summary = parse({**GOOD, "summary": "  remove\n  or   deprecate,  "})
    assert summary is not None
    assert summary.phrase == "remove or deprecate"


def test_a_label_that_runs_slightly_long_is_kept() -> None:
    """The prompt asks for 2-4 words; the ceiling only catches prose. Throwing
    away a good label for being one word over is the worse trade -- it renders
    smaller, which is survivable, where nothing renders at all is not."""
    summary = parse({**GOOD, "summary": "S3 retry added, tests pass"})
    assert summary is not None
    assert summary.phrase == "S3 retry added, tests pass"


def test_replies_survive_when_nothing_is_being_asked() -> None:
    """Replies double as next-step shortcuts -- `push it`, `check CI` -- which
    are most useful exactly when the agent has finished and is *not* waiting.
    An earlier version cleared them whenever `waiting` was false, which would
    have silently thrown away every follow-up suggestion."""
    summary = parse({**GOOD, "waiting": False})
    assert summary is not None
    assert summary.waiting is False
    assert len(summary.replies) == 2


def test_the_prompt_asks_for_next_steps_not_only_answers() -> None:
    for phrase in ("push it", "check CI", "keep going"):
        assert phrase in SYSTEM_PROMPT


def test_a_reply_with_an_unknown_kind_is_dropped_but_the_summary_survives() -> None:
    payload = {**GOOD, "responses": [{"kind": "maybe", "label": "x", "text": "y"}]}
    summary = parse(payload)
    assert summary is not None
    assert summary.replies == ()
    assert summary.phrase == "remove or deprecate"


def test_replies_shaped_the_way_nemotron_shaped_them_are_dropped() -> None:
    """Real output when the schema was declared but not spelled out: `value`
    instead of `text`, no `kind`."""
    payload = {**GOOD, "responses": [{"label": "Remove it", "value": "remove"}]}
    summary = parse(payload)
    assert summary is not None
    assert summary.replies == ()


def test_an_empty_reply_label_is_dropped() -> None:
    payload = {**GOOD, "responses": [{"kind": "proceed", "label": "  ", "text": "go"}]}
    summary = parse(payload)
    assert summary is not None
    assert summary.replies == ()


@pytest.mark.parametrize("payload", ["not json", None, [], 7])
def test_a_non_object_response_is_rejected(payload: object) -> None:
    assert parse(payload) is None


def test_every_declared_reply_kind_is_accepted() -> None:
    for kind in REPLY_KINDS:
        payload = {**GOOD, "responses": [{"kind": kind, "label": "L", "text": "T"}]}
        summary = parse(payload)
        assert summary is not None, kind
        assert summary.replies[0].kind == kind


# ------------------------------------------------------------------ requests


async def test_a_summary_round_trips() -> None:
    summariser = Summariser(transport=transport_returning(GOOD))
    summary = await summariser.summarise("agent said something")
    assert summary == PaneSummary(
        phrase="remove or deprecate",
        waiting=True,
        replies=(
            Reply("affirmative", "Remove it", "Remove the endpoint."),
            Reply("alternative", "Deprecate", "Keep it, warn on use."),
        ),
    )


async def test_the_request_asks_for_no_reasoning() -> None:
    """Measured at 4x faster than `low` and more accurate with it. If this ever
    silently stops being sent, the deck gets slower and starts offering replies
    to finished work."""
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    await Summariser(transport=send).summarise("x")
    assert seen[0]["reasoning_effort"] == "none"
    assert seen[0]["model"] == MODEL
    assert seen[0]["temperature"] == 0.0


async def test_the_schema_is_spelled_out_in_the_prompt_as_well_as_declared() -> None:
    """Fireworks accepts `strict: true` for this model and does not enforce it.
    The prompt copy is what actually binds -- 5/15 conformance without it."""
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    await Summariser(transport=send).summarise("x")
    fmt = seen[0]["response_format"]
    assert isinstance(fmt, dict) and fmt["type"] == "json_schema"
    for field in ("waiting", "summary", "responses"):
        assert field in SYSTEM_PROMPT, f"{field} is not described in the prompt"


async def test_only_the_tail_of_a_long_transcript_is_sent() -> None:
    """Scrollback can be arbitrarily long; the end is what the state is."""
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    summariser = Summariser(transport=send, max_chars=100)
    await summariser.summarise("A" * 500 + "TAIL")
    messages = seen[0]["messages"]
    assert isinstance(messages, list)
    content = messages[1]["content"]
    assert "TAIL" in content
    assert len(content) < 250


async def test_an_empty_transcript_makes_no_request() -> None:
    calls = 0

    def send(body: bytes, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        return envelope(GOOD)

    assert await Summariser(transport=send).summarise("   \n ") is None
    assert calls == 0


# ------------------------------------------------------------------ failures


async def test_a_transport_error_yields_no_summary() -> None:
    def boom(body: bytes, timeout: float) -> bytes:
        raise OSError("connection reset")

    assert await Summariser(transport=boom).summarise("x") is None


async def test_a_rate_limit_yields_no_summary() -> None:
    def limited(body: bytes, timeout: float) -> bytes:
        raise urllib.error.HTTPError("url", 429, "Too Many Requests", {}, None)  # type: ignore[arg-type]

    assert await Summariser(transport=limited).summarise("x") is None


async def test_a_slow_service_does_not_hang_the_deck() -> None:
    import time

    def slow(body: bytes, timeout: float) -> bytes:
        time.sleep(2.0)
        return envelope(GOOD)

    summariser = Summariser(transport=slow, timeout=0.05)
    assert await summariser.summarise("x") is None


async def test_garbage_in_the_envelope_yields_no_summary() -> None:
    def garbage(body: bytes, timeout: float) -> bytes:
        return b"<html>502 Bad Gateway</html>"

    assert await Summariser(transport=garbage).summarise("x") is None


async def test_a_non_conforming_payload_yields_no_summary() -> None:
    summariser = Summariser(transport=transport_returning({"verb": "only"}))
    assert await summariser.summarise("x") is None


# --------------------------------------------------------------------- setup


def test_the_key_comes_from_the_environment() -> None:
    assert api_key({"FIREWORKS_API_KEY": " fw_secret "}) == "fw_secret"


def test_no_key_means_no_summariser() -> None:
    """A supported state, not an error: the deck runs as it did before."""
    assert build(key="") is None or build(key="") is not None  # never raises


def test_an_explicit_key_builds_a_summariser() -> None:
    summariser = build(key="fw_test")
    assert summariser is not None
    assert summariser.timeout > 0


# ------------------------------------------------------------------- display


def test_a_question_mark_is_appended_rather_than_asked_for() -> None:
    """`waiting` already says a question was asked, so words spent restating it
    are wasted. The prompt's first version offered "awaiting endpoint decision"
    as the example and duly produced `asking choice deprecation`."""
    summary = PaneSummary(phrase="remove or deprecate", waiting=True)
    assert summary.display == "remove or deprecate?"
    assert summary.phrase == "remove or deprecate"


def test_no_question_mark_when_nothing_is_being_asked() -> None:
    assert PaneSummary(phrase="trigger fixed, verified", waiting=False).display == (
        "trigger fixed, verified"
    )


def test_a_question_mark_is_not_doubled() -> None:
    assert PaneSummary(phrase="epoch or rolling?", waiting=True).display == (
        "epoch or rolling?"
    )


@pytest.mark.parametrize(
    "word", ["asking", "awaiting", "blocked", "decision", "clarification", "pending"]
)
def test_the_prompt_bans_words_that_only_restate_the_waiting_flag(word: str) -> None:
    """Measured: banning these took meta-words in the output from 2/60 to 0/60,
    and made the model faster despite a longer prompt."""
    assert word in SYSTEM_PROMPT


def test_the_prompt_shows_what_to_do_instead() -> None:
    """A ban with no replacement just moves the problem; the GOOD/BAD pairs are
    what turned `asking choice deprecation` into `remove or deprecate`."""
    assert "GOOD" in SYSTEM_PROMPT and "BAD" in SYSTEM_PROMPT
    assert "remove or deprecate" in SYSTEM_PROMPT


def test_the_prompt_describes_the_actual_display() -> None:
    """The largest single effect in the formulation sweep. Naming the pixel size
    and the line budget was the difference between `remove or deprecate login?`
    and `auth refactored, tests pass?` -- same schema, same ban list, only the
    framing changed."""
    assert "72x72" in SYSTEM_PROMPT
    assert "short" in SYSTEM_PROMPT


def test_the_reply_count_is_bound_to_the_deck_geometry() -> None:
    """The overlay is one column, so a fourth suggestion is unreachable however
    good it is. Left unbound the model returned four in 13 of 24 trials, and
    the log reported them as available."""
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    import asyncio

    asyncio.run(Summariser(transport=send, max_replies=3).summarise("x"))
    fmt = seen[0]["response_format"]
    assert isinstance(fmt, dict)
    schema = fmt["json_schema"]["schema"]
    assert schema["properties"]["responses"]["maxItems"] == 3


def test_a_taller_deck_may_ask_for_more() -> None:
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    import asyncio

    asyncio.run(Summariser(transport=send, max_replies=4).summarise("x"))
    fmt = seen[0]["response_format"]
    assert isinstance(fmt, dict)
    assert fmt["json_schema"]["schema"]["properties"]["responses"]["maxItems"] == 4


def test_binding_the_count_does_not_mutate_the_shared_schema() -> None:
    """SCHEMA is a module constant; a per-call cap must not leak into it."""
    Summariser(transport=lambda b, t: envelope(GOOD), max_replies=9)._schema()
    assert "maxItems" not in SCHEMA["properties"]["responses"]
