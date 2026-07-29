"""Three-word pane summaries from a fast hosted model.

A key can say *that* an agent is blocked. It cannot say what it is blocked on,
and that is the thing worth knowing -- "blocked" sends you to the pane, which is
the trip the deck exists to save. So on a status transition the pane's recent
output is sent to a small model that returns the end state in three words, plus
the replies that would answer it.

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
            "description": "Empty unless the agent is blocked awaiting input.",
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

SYSTEM_PROMPT = """You are summarising a response from a coding agent in 2-4
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

Offer replies ONLY if the agent is blocked waiting on the user. If it finished
cleanly and asked nothing, return an empty responses list. Each reply label is
1-3 short words.

Return ONLY this JSON object, with every field present and no extra fields:
{"waiting": <true|false>, "summary": "<2-4 short words>",
 "responses": [{"kind": "affirmative"|"negative"|"proceed"|"alternative",
                "label": "<1-3 words>", "text": "<full reply>"}]}
`waiting` is required and must always be present. Every response object must have
all three of kind, label and text."""


@dataclass(frozen=True, slots=True)
class Reply:
    """One suggested answer to whatever the agent is asking.

    ``label`` is for a key face; ``text`` is what would be sent verbatim. Nothing
    sends these yet -- see DeckController, which stores them and stops there.
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
    """Turn a model response into a summary, or None if it does not conform.

    Strictness here is load-bearing in one specific way: ``waiting`` gates
    whether replies are offered at all, so a response that omits it is discarded
    rather than defaulted. Defaulting to False would silently drop suggestions
    on a pane that needs them; defaulting to True would offer answers to a
    question nobody asked.
    """
    if not isinstance(payload, dict):
        return None

    waiting = payload.get("waiting")
    if not isinstance(waiting, bool):
        return None

    phrase = _phrase(payload.get("summary"))
    if phrase is None:
        return None

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

    # A reply the model itself says is unwanted is a reply we must not show.
    if not waiting:
        replies = []

    return PaneSummary(phrase=phrase, waiting=waiting, replies=tuple(replies))


@dataclass
class Summariser:
    """Calls the model. Every failure returns None rather than raising.

    The deck must survive the summary service being slow, broken, rate-limited
    or unreachable, because it is an enhancement to a display that already works
    without it. Nothing here is allowed to take the deck down with it.
    """

    transport: Transport
    timeout: float = 6.0
    max_chars: int = 3000
    """How much scrollback to send. Measured: 3000 characters of real herdr pane
    output -- box drawing, spinners, status lines and all -- is about 240 prompt
    tokens and summarises correctly. More context did not improve the answer."""

    def _body(self, transcript: str) -> bytes:
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
                    "json_schema": {"name": "pane", "strict": True, "schema": SCHEMA},
                },
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": "Agent transcript:\n---\n"
                        + transcript[-self.max_chars :]
                        + "\n---",
                    },
                ],
            }
        ).encode()

    async def summarise(self, transcript: str) -> PaneSummary | None:
        """Summarise a pane's recent output. None on any failure at all."""
        if not transcript.strip():
            return None
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self.transport, self._body(transcript), self.timeout),
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
            envelope = json.loads(raw)
            content = envelope["choices"][0]["message"]["content"]
            summary = parse(json.loads(content))
        except Exception:
            logger.warning("could not read summary response", exc_info=True)
            return None

        if summary is None:
            logger.debug("summary did not conform to the schema; discarding")
        return summary


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


def build(key: str | None = None) -> Summariser | None:
    """A summariser, or None when no key is configured.

    None is a supported state, not an error: the deck runs exactly as it did
    before summaries existed.
    """
    resolved = key or api_key()
    if not resolved:
        return None
    return Summariser(transport=_urllib_transport(resolved))
