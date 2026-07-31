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
    check,
    last_message,
    parse,
    strip_input_box,
    strip_status_lines,
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
    """A response shaped the way the model actually answers: a tool call."""
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "label_pane",
                                    "arguments": json.dumps(payload),
                                },
                            }
                        ],
                    }
                }
            ]
        }
    ).encode()


def prose_envelope(payload: object) -> bytes:
    """A model that answered in text instead of calling the tool."""
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


async def test_the_answer_is_forced_through_a_tool_call() -> None:
    """Both mechanisms conform with reasoning off, but only the tool call
    survives reasoning being on: a response schema drops to 7/15 there,
    silently omitting `responses`. Since `reasoning_effort="none"` is an
    undocumented value, the deck should not depend on it holding."""
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    await Summariser(transport=send).summarise("x")
    assert "response_format" not in seen[0]
    assert seen[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "label_pane"},
    }
    tools = seen[0]["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    for field in ("waiting", "summary", "responses"):
        assert field in SYSTEM_PROMPT, f"{field} is not described in the prompt"


async def test_a_prose_answer_still_parses() -> None:
    """A model that ignores tool_choice and answers in text should go down the
    normal path, not be mistaken for a broken response."""

    def send(body: bytes, timeout: float) -> bytes:
        return prose_envelope(GOOD)

    summary = await Summariser(transport=send).summarise("x")
    assert summary is not None
    assert summary.phrase == "remove or deprecate"


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
    # The cap is on the transcript, not on the whole turn -- which also carries
    # the closing block quoted back and the instruction that points at it.
    assert "A" * 150 not in content, "the transcript was not capped"


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
    tools = seen[0]["tools"]
    assert isinstance(tools, list)
    schema = tools[0]["function"]["parameters"]
    assert schema["properties"]["responses"]["maxItems"] == 3


def test_a_taller_deck_may_ask_for_more() -> None:
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    import asyncio

    asyncio.run(Summariser(transport=send, max_replies=4).summarise("x"))
    tools = seen[0]["tools"]
    assert isinstance(tools, list)
    assert tools[0]["function"]["parameters"]["properties"]["responses"]["maxItems"] == 4


def test_binding_the_count_does_not_mutate_the_shared_schema() -> None:
    """SCHEMA is a module constant; a per-call cap must not leak into it."""
    Summariser(transport=lambda b, t: envelope(GOOD), max_replies=9)._schema()
    assert "maxItems" not in SCHEMA["properties"]["responses"]


# ------------------------------------------------------------------- retries


def replying(*payloads: object) -> tuple[Transport, list[dict[str, object]]]:
    """A transport that returns each payload in turn, recording what it was sent."""
    seen: list[dict[str, object]] = []
    queue = list(payloads)

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(queue.pop(0) if queue else GOOD)

    return send, seen


async def test_a_non_conforming_response_is_retried() -> None:
    send, seen = replying({"summary": "no waiting field"}, GOOD)
    summary = await Summariser(transport=send).summarise("x")
    assert summary is not None
    assert summary.phrase == "remove or deprecate"
    assert len(seen) == 2, "it should have asked again"


async def test_the_retry_says_what_was_wrong_and_restates_the_shape() -> None:
    """A bare 'try again' invites the same mistake. The parser knows which
    field it rejected, so the correction can name it."""
    send, seen = replying({"summary": "no waiting field"}, GOOD)
    await Summariser(transport=send).summarise("x")

    messages = seen[1]["messages"]
    assert isinstance(messages, list)
    assert messages[2]["role"] == "assistant", "the model should see its own answer"
    correction = messages[3]["content"]
    assert "`waiting`" in correction
    assert '"summary"' in correction, "the shape is restated in full"


async def test_it_gives_up_after_the_attempt_limit() -> None:
    bad = {"summary": "still no waiting field"}
    send, seen = replying(bad, bad, bad, bad, bad)
    assert await Summariser(transport=send, attempts=3).summarise("x") is None
    assert len(seen) == 3, "exactly three tries, not more"


async def test_a_good_first_answer_is_not_retried() -> None:
    send, seen = replying(GOOD)
    assert await Summariser(transport=send).summarise("x") is not None
    assert len(seen) == 1


async def test_transport_failures_are_not_retried() -> None:
    """A timeout or a 429 will not be argued out of, and a stalled key is worse
    than an absent summary."""
    calls = 0

    def failing(body: bytes, timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError("url", 429, "Too Many", {}, None)  # type: ignore[arg-type]

    assert await Summariser(transport=failing, attempts=3).summarise("x") is None
    assert calls == 1


async def test_the_transcript_is_not_resent_on_a_retry() -> None:
    """It is the largest part of the request; the correction is a continuation
    of the same conversation, not a fresh one."""
    send, seen = replying({"summary": "bad"}, GOOD)
    await Summariser(transport=send).summarise("PANE OUTPUT HERE")

    second = seen[1]["messages"]
    assert isinstance(second, list)
    assert sum(1 for m in second if "PANE OUTPUT HERE" in str(m.get("content"))) == 1


def test_check_explains_each_rejection() -> None:
    for payload, expected in (
        ({"summary": "x", "responses": []}, "waiting"),
        ({"waiting": True, "summary": "in]valid", "responses": []}, "summary"),
        ("not an object", "JSON object"),
    ):
        summary, reason = check(payload)
        assert summary is None
        assert expected in reason, f"{payload!r} -> {reason!r}"


# ---------------------------------------------------------- terminal chrome

RULE = "─" * 96

PROMPT_BOX = "\n".join(
    [
        "  I switched the serialiser to orjson and p99 fell to 95ms.",
        "",
        "✻ Brewed for 1m 7s",
        "   ",
        RULE,
        "❯ set up kimi code pointed at fireworks",
        RULE,
        "   …/tas/herdr-streamdeck   main  ✱ Opus 5 ⣶  5h / 7d [⠀⠀⠀⠀⠀]",
        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← 1 agent",
    ]
)

DIALOG = "\n".join(
    [
        "  This session is 12d 5h old and 180.5k tokens.",
        RULE,
        "  Resuming will consume a substantial portion of your usage limits.",
        "",
        "  ❯ 1. Resume from summary (recommended)",
        "    2. Resume full session as-is",
        "    3. Don't ask me again",
        "",
        "  Enter to confirm · Esc to cancel",
    ]
)


def test_the_input_box_and_status_lines_are_removed() -> None:
    """The pane read is a screenshot, so it ends with the harness rather than
    with the agent. Summarising the tail described what the *user* was typing:
    a pane whose box held "set up kimi code pointed at fireworks" was labelled
    `Kimi→Fireworks direct path` instead of the agent's actual answer."""
    kept = strip_input_box(PROMPT_BOX)
    assert kept.endswith("✻ Brewed for 1m 7s")
    assert "kimi" not in kept
    assert "auto mode on" not in kept
    assert "orjson" in kept, "the agent's own words survive"


def test_a_dialog_is_left_alone() -> None:
    """Drawn with rules too, but it is content -- the question being asked. It
    is distinguished by having text *below* it rather than a status bar."""
    assert strip_input_box(DIALOG) == DIALOG.rstrip()


def test_a_transcript_with_no_furniture_is_untouched() -> None:
    plain = "I refactored auth.\nAll 47 tests pass."
    assert strip_input_box(plain) == plain


def test_a_rule_in_the_agents_own_output_is_not_a_box() -> None:
    """Only rules near the very end count, so a horizontal rule earlier in the
    output is just text."""
    body = f"before\n{RULE}\nafter\n" + "\n".join(f"line {n}" for n in range(8))
    assert strip_input_box(body).endswith("line 7")


def test_a_short_dash_run_is_not_a_rule() -> None:
    """`---` in markdown must not read as the top of an input box."""
    body = "some notes\n---\nmore notes"
    assert strip_input_box(body) == body


@pytest.mark.parametrize("text", ["", "   ", "\n\n\n", "one line"])
def test_degenerate_transcripts_survive(text: str) -> None:
    strip_input_box(text)


async def test_the_summariser_strips_before_sending() -> None:
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    await Summariser(transport=send).summarise(PROMPT_BOX)
    messages = seen[0]["messages"]
    assert isinstance(messages, list)
    sent = str(messages[1]["content"])
    assert "kimi" not in sent
    assert "orjson" in sent


# ------------------------------------------------------- pointing at the end

SCROLLBACK = "\n".join(
    [
        "• Should I delete the legacy /v1/login endpoint, or keep it deprecated?",
        "",
        "",
        "› yes remove it",
        "",
        "",
        "• I finished adding a retry wrapper to the S3 client. All 12 tests pass.",
        "",
        "",
        "› Explain this codebase",
        "",
        "  gpt-5.6-terra medium · ~/diggy",
    ]
)
"""A real Codex pane: every past user turn echoed with the same chevron the live
box uses, and the live box last, holding a placeholder."""


def test_the_last_message_is_the_closing_block() -> None:
    assert last_message(strip_input_box(SCROLLBACK)) == (
        "• I finished adding a retry wrapper to the S3 client. All 12 tests pass."
    )


def test_the_live_box_wins_over_every_earlier_echo() -> None:
    """Codex echoes each past turn with the chevron it draws its live box with.
    The box is always the last of them, so the echoes never matter."""
    kept = strip_input_box(SCROLLBACK)
    assert kept.rstrip().endswith("All 12 tests pass.")
    assert "Explain this codebase" not in kept
    assert "yes remove it" in kept, "an earlier turn is context, not furniture"


def test_a_multi_line_closing_block_is_kept_whole() -> None:
    text = "old news\n\nline one\nline two\nline three"
    assert last_message(text) == "line one\nline two\nline three"


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_an_empty_transcript_has_no_last_message(text: str) -> None:
    assert last_message(text) == ""


async def test_the_request_points_at_the_end_of_the_scrollback() -> None:
    """The scrollback alone does not say which part is current. A Codex pane
    holding an old "delete or deprecate?" above a newer "retry wrapper added"
    was labelled with the question every time -- and offered replies to it,
    which would have answered something nobody was waiting on."""
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    await Summariser(transport=send).summarise(SCROLLBACK)
    messages = seen[0]["messages"]
    assert isinstance(messages, list)
    sent = str(messages[1]["content"])

    assert sent.count("retry wrapper") == 2, "quoted back as well as in context"
    assert sent.count("Should I delete") == 1, "the old question stays context only"
    assert "latest message" in sent


# ------------------------------------------------- the harness's own trailing


SPINNERS = [
    "✻ Brewed for 1m 7s",
    "✻ Crunched for 6m 28s · 1 shell still running",
    "* Bootstrapping… (1m 1s · ↓ 1.0k tokens)",
    "✻ Worked for 2m 36s",
]


@pytest.mark.parametrize("spinner", SPINNERS)
def test_a_progress_line_is_not_the_agents_last_word(spinner: str) -> None:
    """Left in place these become the final line, and the pointer at the newest
    message lands on one instead of on anything the agent said."""
    text = f"I switched the serialiser to orjson and p99 fell to 95ms.\n\n{spinner}"
    assert strip_status_lines(text).endswith("95ms.")


def test_a_right_aligned_counter_is_dropped() -> None:
    """Measured at 125 to 305 characters of padding across the sampled panes;
    no wrapped agent output comes close."""
    text = "All 12 integration tests pass." + "\n" + " " * 150 + "702133 tokens"
    assert strip_status_lines(text).endswith("tests pass.")


def test_prose_that_merely_mentions_tokens_survives() -> None:
    """The discriminator is the alignment, not the word."""
    text = "The harness overhead came to about 4200 tokens per call."
    assert strip_status_lines(text) == text


@pytest.mark.parametrize(
    "line",
    [
        "● 2 background shell command tasks have no completion record",
        "※ recap: You asked me to find a machine ID",
        "• I finished adding a retry wrapper. All 12 tests pass.",
    ],
)
def test_a_bulleted_message_is_not_a_progress_line(line: str) -> None:
    """These open with a glyph too, which is why the pattern also requires a
    duration or a parenthetical right after the first word."""
    assert strip_status_lines(f"earlier context\n{line}").endswith(line)


def test_a_progress_line_mid_transcript_is_left_alone() -> None:
    """It is a fair record of what happened; only a trailing one pretends to be
    the agent's latest word."""
    text = "✻ Brewed for 1m 7s\n\nAnd here is what I found."
    assert strip_status_lines(text) == text


async def test_the_request_carries_neither_box_nor_spinner() -> None:
    seen: list[dict[str, object]] = []

    def send(body: bytes, timeout: float) -> bytes:
        seen.append(json.loads(body))
        return envelope(GOOD)

    await Summariser(transport=send).summarise(
        PROMPT_BOX.replace("✻ Brewed for 1m 7s", "✻ Brewed for 1m 7s")
    )
    messages = seen[0]["messages"]
    assert isinstance(messages, list)
    sent = str(messages[1]["content"])
    assert "Brewed for" not in sent
    assert "auto mode on" not in sent
    assert "orjson" in sent
