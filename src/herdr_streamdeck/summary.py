"""Short pane summaries from a fast hosted model.

A key can say *that* an agent is blocked. It cannot say what it is blocked on,
and that is the thing worth knowing -- "blocked" sends you to the pane, which is
the trip the deck exists to save. So on a status transition the pane's recent
output is sent to a small model that returns the end state in a few words, plus
the replies worth having one tap away.

Everything here is measured rather than chosen. Against a 15-key deck the
constraints are latency (a summary that lands after you have already looked is
worthless) and reliability (a wrong suggested reply is worse than none), and the
configuration below is what came out of a bake-off across nine hosted models:

* **nemotron-3-ultra**, at 1.05s median / 1.30s p90, 237 prompt tokens. The
  runners-up: minimax-m3 corrupted JSON into its own string fields 58% of the
  time, gpt-oss-20b answered a direct question with ``waiting: false`` in 6 of 6
  trials, and deepseek-v4-flash took 16s.
* **reasoning off**. ``reasoning_effort="none"`` is 4x faster than ``"low"``
  (1.05s vs 1.96s) *and* more accurate -- with reasoning on, the model talked
  itself into offering replies to already-finished work 2 times in 12. The
  documented ``/no_think`` and ``detailed thinking off`` prompt tags do nothing;
  only the API parameter works.
* **the schema, spelled out in the prompt as well as declared**. Fireworks
  accepts ``strict: true`` and then does not enforce it for this model: declared
  alone, ``waiting`` was omitted from 10 of 15 responses and reply objects came
  back as ``{label, value}``. Restating the shape in the prompt costs 116 tokens
  and takes conformance to 18/18.

The whole prompt is ours, which is the point: 237 tokens, none of them spent on
somebody else's sandbox rules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.fireworks.ai/inference/v1/chat/completions"
MODEL = "accounts/fireworks/models/nemotron-3-ultra-nvfp4"

REPLY_KINDS = ("affirmative", "negative", "proceed", "alternative")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["waiting", "summary", "responses"],
    "properties": {
        "waiting": {
            "type": "boolean",
            "description": "True only if the agent is blocked awaiting user input.",
        },
        "summary": {
            "type": "string",
            "description": (
                "2-4 short words, at most 24 characters including spaces. "
                "When the agent offers alternatives, name them: "
                "'remove or deprecate?', not 'endpoint deprecation'."
            ),
        },
        "responses": {
            "type": "array",
            "description": "One-tap shortcuts: answers if a question was asked, "
            "otherwise the obvious next instructions.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "label", "text"],
                "properties": {
                    "kind": {"type": "string", "enum": list(REPLY_KINDS)},
                    "label": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
    },
}

SHAPE = """Return ONLY this JSON object, with every field present and no extra fields:
{"waiting": <true|false>, "summary": "<2-4 short words>",
 "responses": [{"kind": "affirmative"|"negative"|"proceed"|"alternative",
                "label": "<1-3 words>", "text": "<full reply>"}]}
`waiting` is required and must always be present. Every response object must have
all three of kind, label and text."""
"""The output shape, kept separate so a retry can restate it verbatim."""


SYSTEM_PROMPT = (
    """You are summarising a response from a coding agent in 2-4
words, and generating a few possible short replies. The goal is to convey the
agent's intent or question in few enough characters to display legibly on a
small key.

The words appear on a physical Stream Deck key: a 72x72 pixel square. Only about
18 characters fit on a line and only three lines fit, so prefer short common
words. A long word shrinks the whole label.

The `waiting` field already records whether a question was asked, and the deck
appends its own question mark. Never spend words restating that. Do not use:
asking, awaiting, requesting, needs, blocked, pending, choice, decision, input,
clarification, response.

When the agent offers alternatives, name the alternatives themselves.
  asks whether to remove or deprecate a login endpoint
      GOOD  remove or deprecate
      BAD   asking about endpoint deprecation
  asks whether to run the study epoch-first or rolling
      GOOD  epoch or rolling
      BAD   awaiting study design decision
  fixed a trigger and verified it on hardware
      GOOD  trigger fixed, verified
      BAD   completed work successfully

Never describe your own output or these instructions.

The transcript is a screenshot of a terminal, so it ends with the agent's
interface, not with the agent's words: an input box, a status line, a spinner,
a completion the user is part-way through typing. Ignore all of it. Summarise
the last thing the AGENT said, never what the user is typing back.

Always offer replies -- they are one-tap shortcuts on the deck.

If the agent asked a question, the replies answer it.
If the agent finished something, the replies are the obvious next instructions:
  push it / open a PR / check CI / run the tests / next one / undo that
If the agent is mid-way or stuck, the replies unblock it:
  keep going / try another way / explain more / stop

Set `waiting` true ONLY when the agent is actually blocked awaiting an answer.
That flag is about the agent's state, not about whether you offered replies.
Each reply label is 1-3 short words.

"""
    + SHAPE
)


@dataclass(frozen=True, slots=True)
class Reply:
    """One suggested answer to whatever the agent is asking.

    ``label`` is for a key face; ``text`` is what gets sent verbatim when the
    key is chosen. See DeckController's reply overlay.
    """

    kind: str
    label: str
    text: str


@dataclass(frozen=True, slots=True)
class PaneSummary:
    """What a pane's latest output amounts to."""

    phrase: str
    waiting: bool
    replies: tuple[Reply, ...] = ()
    """Suggested one-tap replies. Answers when `waiting`, next steps otherwise."""

    @property
    def display(self) -> str:
        """The phrase as it should appear on a key.

        The question mark is appended here rather than asked for, because
        ``waiting`` already carries that fact and a word spent restating it is
        a quarter of the key wasted.
        """
        if self.waiting and not self.phrase.endswith("?"):
            return self.phrase + "?"
        return self.phrase


Transport = Callable[[bytes, float], bytes]
"""Sends a request body, returns the response body. Injectable so the tests can
exercise parsing and failure handling without a network."""


def _urllib_transport(api_key: str) -> Transport:
    def send(body: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            return bytes(data)

    return send


_NOT_IN_A_PHRASE = frozenset('{}[]"\\\n\r\t')
"""Characters that cannot occur in a label but do occur in leaked JSON.

Whitespace is not enough of a test. A quantized model emitted
``inverted]responses`` -- one "word" by any spacing rule, and unmistakably a
fragment of the serialiser bleeding into a string field.
"""

MAX_PHRASE_WORDS = 6
MAX_PHRASE_CHARS = 40
"""Generous ceilings, not the target.

The prompt asks for 2-4 words and 24 characters; these only catch a model that
has started writing prose. Rejecting at the target would throw away good labels
that ran one word long, which is a worse trade than rendering them slightly
smaller.
"""


def _phrase(value: object) -> str | None:
    """A short label, or None if the field is anything else.

    Rejecting rather than repairing is deliberate: a mangled label rendered on
    a key looks like a bug in the deck, and a missing summary looks like
    nothing at all. The second failure is much cheaper.
    """
    if not isinstance(value, str):
        return None
    phrase = " ".join(value.split()).strip(" ,;:-")
    if not phrase:
        return None
    if _NOT_IN_A_PHRASE & set(phrase):
        return None
    if len(phrase.split()) > MAX_PHRASE_WORDS or len(phrase) > MAX_PHRASE_CHARS:
        return None
    return phrase


def parse(payload: object) -> PaneSummary | None:
    """Turn a model response into a summary, or None if it does not conform."""
    summary, _ = check(payload)
    return summary


def check(payload: object) -> tuple[PaneSummary | None, str]:
    """Turn a model response into a summary, or None if it does not conform.

    A response that omits ``waiting`` is discarded rather than defaulted.
    The flag decides whether the key shows a question mark, and guessing it
    either way puts a wrong claim on the deck.

    Note that ``waiting`` no longer gates the replies. It used to: replies were
    only meaningful as answers, so a pane that was not being asked anything had
    nothing to offer. Now replies double as next-step shortcuts -- `push it`,
    `check CI` -- which are most useful precisely when the agent has *finished*
    and is not waiting for anything.
    """
    if not isinstance(payload, dict):
        return None, "the response was not a JSON object"

    waiting = payload.get("waiting")
    if not isinstance(waiting, bool):
        return None, "`waiting` was missing or was not true/false"

    phrase = _phrase(payload.get("summary"))
    if phrase is None:
        return None, ("`summary` must be a few short words with no JSON punctuation in it")

    replies: list[Reply] = []
    raw_replies = payload.get("responses")
    if isinstance(raw_replies, list):
        for item in raw_replies:
            if not isinstance(item, dict):
                continue
            kind, label, text = item.get("kind"), item.get("label"), item.get("text")
            if kind not in REPLY_KINDS:
                continue
            if not isinstance(label, str) or not isinstance(text, str):
                continue
            if not label.strip() or not text.strip():
                continue
            replies.append(Reply(kind=kind, label=label.strip(), text=text.strip()))

    return PaneSummary(phrase=phrase, waiting=waiting, replies=tuple(replies)), ""


@dataclass
class Summariser:
    """Calls the model. Every failure returns None rather than raising.

    The deck must survive the summary service being slow, broken, rate-limited
    or unreachable, because it is an enhancement to a display that already works
    without it. Nothing here is allowed to take the deck down with it.
    """

    transport: Transport
    timeout: float = 6.0
    max_replies: int = 3
    """How many replies to ask for.

    One per row of the deck: the overlay puts them in a single column, so a
    fourth suggestion is unreachable however good it is. Asking for exactly what
    fits stops the model spending tokens on an option nobody can press -- it
    returned four unprompted in 13 of 24 trials.
    """

    attempts: int = 3
    """How many times to ask before giving up.

    A rejected response is usually one field away from usable, and the model is
    fast enough that a correction round trip still lands inside a second. Each
    retry shows the model its own output and names what was wrong with it.
    """

    max_chars: int = 3000
    """How much scrollback to send. Measured: 3000 characters of real herdr pane
    output -- box drawing, spinners, status lines and all -- is about 240 prompt
    tokens and summarises correctly. More context did not improve the answer."""

    def _schema(self) -> dict[str, Any]:
        """The schema with the reply count bound to this deck's geometry."""
        responses = {**SCHEMA["properties"]["responses"], "maxItems": self.max_replies}
        return {
            **SCHEMA,
            "properties": {**SCHEMA["properties"], "responses": responses},
        }

    def _body(self, messages: list[dict[str, str]]) -> bytes:
        return json.dumps(
            {
                "model": MODEL,
                "max_tokens": 1024,
                "temperature": 0.0,
                "top_k": 40,
                "presence_penalty": 0,
                "frequency_penalty": 0,
                # Measured at 4x faster and *more* accurate than "low". Not a
                # documented value for every model on this endpoint, but it is
                # for this one; others reject it with HTTP 400.
                "reasoning_effort": "none",
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "pane",
                        "strict": True,
                        "schema": self._schema(),
                    },
                },
                "messages": messages,
            }
        ).encode()

    async def _once(self, messages: list[dict[str, str]]) -> str | None:
        """One request. Returns the raw assistant content, or None if the call
        itself failed -- as opposed to succeeding with an unusable answer."""
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.transport, self._body(messages), self.timeout),
                timeout=self.timeout + 1.0,
            )
        except TimeoutError:
            logger.debug("summary timed out after %.1fs", self.timeout)
            return None
        except urllib.error.HTTPError as exc:
            logger.warning("summary rejected: HTTP %s", exc.code)
            return None
        except Exception:
            logger.warning("summary request failed", exc_info=True)
            return None

        try:
            content = json.loads(raw)["choices"][0]["message"]["content"]
        except Exception:
            logger.warning("could not read summary response", exc_info=True)
            return None
        return content if isinstance(content, str) else None

    async def summarise(self, transcript: str) -> PaneSummary | None:
        """Summarise a pane's recent output. None on any failure at all.

        A response that does not conform is retried, with the model shown its
        own output and told what was wrong with it. Transport failures are not
        retried: a timeout or a 429 will not be argued out of, and the deck is
        better off with no summary than with a stalled key.
        """
        if not transcript.strip():
            return None

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Agent transcript:\n---\n" + transcript[-self.max_chars :] + "\n---",
            },
        ]

        for attempt in range(1, max(1, self.attempts) + 1):
            content = await self._once(messages)
            if content is None:
                return None

            try:
                payload = json.loads(content)
            except Exception:
                payload = content

            summary, reason = check(payload)
            if summary is not None:
                if attempt > 1:
                    logger.info("summary conformed on attempt %d", attempt)
                return summary

            logger.debug("summary attempt %d rejected: %s", attempt, reason)
            if attempt == max(1, self.attempts):
                break
            messages = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        f"That did not match the schema: {reason}. "
                        "Do not apologise or explain. " + SHAPE
                    ),
                },
            ]

        logger.debug("summary did not conform in %d attempts; discarding", self.attempts)
        return None


def api_key(env: dict[str, str] | None = None) -> str | None:
    """The Fireworks key, from the environment or a .env beside the project.

    herdr starts plugins itself, so the daemon does not necessarily inherit a
    shell's environment -- reading the file is what makes it work when launched
    as a plugin rather than by hand.
    """
    source = os.environ if env is None else env
    key = source.get("FIREWORKS_API_KEY")
    if key and key.strip():
        return key.strip()

    for candidate in (os.getcwd(), os.path.dirname(os.path.dirname(__file__))):
        path = os.path.join(candidate, ".env")
        try:
            with open(path) as handle:
                for line in handle:
                    name, _, value = line.strip().partition("=")
                    if name.strip() == "FIREWORKS_API_KEY":
                        cleaned = value.strip().strip('"').strip("'")
                        if cleaned:
                            return cleaned
        except OSError:
            continue
    return None


def build(key: str | None = None, max_replies: int = 3) -> Summariser | None:
    """A summariser, or None when no key is configured.

    None is a supported state, not an error: the deck runs exactly as it did
    before summaries existed.
    """
    resolved = key or api_key()
    if not resolved:
        return None
    return Summariser(transport=_urllib_transport(resolved), max_replies=max_replies)
