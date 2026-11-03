"""The notes reader: the only AI step. It reads free text and returns flags.

It never returns "send". The gate turns any raised flag, any unsure flag, or
a failure to read into a HOLD, so the AI can only add holds.

What it reads (from Context Check 03):
- Only what changed on this order: the note minus the customer's standing
  comment, plus the memo. If nothing changed, there's no AI call at all.
- Each standing comment once, cached. A problem there is reported as "fix
  the customer record" on that customer's orders, not held every time.

A read is safe to repeat, unlike a warehouse send, so a failure is retried
once after a pause. If that fails too, the result says so and the gate holds.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable

from chaain.core.models import Sale
from chaain.llm import LLMError, LLMProvider
from chaain.workflows.order_push.gate import NoteFlag, NotesResult

PROMPT_VERSION = "notes-v1"
PROMPT = (Path(__file__).parent / "prompts" / f"{PROMPT_VERSION}.md").read_text()
FLAGS = ("invoice_only", "partial_send", "one_off_delivery_change", "time_in_instructions", "needs_human")

SCHEMA = {
    "type": "object",
    "properties": {
        "flags": {
            "type": "object",
            "properties": {
                name: {
                    "type": "object",
                    "properties": {
                        "raised": {"type": "boolean"},
                        "confidence": {"type": "number"},
                        "evidence": {"type": "string"},
                    },
                    "required": ["raised", "confidence", "evidence"],
                    "additionalProperties": False,
                }
                for name in FLAGS
            },
            "required": list(FLAGS),
            "additionalProperties": False,
        },
        "explanation": {"type": "string"},
    },
    "required": ["flags", "explanation"],
    "additionalProperties": False,
}


def changed_text(sale: Sale) -> str:
    """What's new on this order compared with the standing comment."""
    note, standing = sale.note.strip(), sale.standing_note.strip()
    if note == standing:
        added = ""
    elif standing and standing in note:
        added = note.replace(standing, " ").strip()  # text added around the standing comment
    else:
        added = note  # rewritten for this order: read all of it
    parts = []
    if added:
        parts.append(f"Order note (changed for this order):\n{added}")
    if sale.memo.strip():
        parts.append(f"Order memo:\n{sale.memo.strip()}")
    return "\n\n".join(parts)


class NotesReader:
    def __init__(
        self, provider: LLMProvider, retry_pause_seconds: float = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.retry_pause_seconds = retry_pause_seconds
        self.sleep = sleep
        self._standing_cache: dict[str, tuple[NoteFlag, ...]] = {}

    def read(self, sale: Sale) -> NotesResult:
        standing, standing_error = self._standing(sale.standing_note)
        text = changed_text(sale)
        if not text:
            return NotesResult(standing=standing, source=self._source(), standing_error=standing_error)
        try:
            flags = self._ask(text)
        except LLMError as e:
            return NotesResult(error=str(e), standing=standing, source=self._source())
        return NotesResult(flags=flags, standing=standing, source=self._source(), standing_error=standing_error)

    def _standing(self, note: str) -> tuple[tuple[NoteFlag, ...], str | None]:
        """Read a standing comment once per process, keyed by its text and the prompt version."""
        if not note.strip():
            return (), None
        key = hashlib.sha256(f"{PROMPT_VERSION}\n{note.strip()}".encode()).hexdigest()
        if key not in self._standing_cache:
            try:
                flags = self._ask(f"Customer's standing comment (copied onto every order):\n{note.strip()}")
            except LLMError as e:
                return (), str(e)  # not cached: try again on the next order
            self._standing_cache[key] = tuple(f for f in flags if f.raised)
        return self._standing_cache[key], None

    def _ask(self, text: str) -> tuple[NoteFlag, ...]:
        """One try, one retry after a pause. Malformed answers count as failures."""
        try:
            return _parse(self.provider.complete_json(PROMPT, text, SCHEMA))
        except LLMError:
            self.sleep(self.retry_pause_seconds)
            return _parse(self.provider.complete_json(PROMPT, text, SCHEMA))

    def _source(self) -> str:
        return f"{PROMPT_VERSION} via {self.provider.name}/{self.provider.model}"


def _parse(answer: dict) -> tuple[NoteFlag, ...]:
    """Check the answer like any other untrusted input."""
    try:
        explanation = str(answer["explanation"]).strip()
        flags = []
        for name in FLAGS:
            f = answer["flags"][name]
            raised, confidence = f["raised"], f["confidence"]
            if not isinstance(raised, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise LLMError(f"bad values for {name}")
            evidence = str(f.get("evidence", "")).strip()
            detail = f'"{evidence}". {explanation}' if evidence else explanation
            flags.append(NoteFlag(name, raised, float(confidence), detail))
        return tuple(flags)
    except (KeyError, TypeError) as e:
        raise LLMError(f"answer didn't match the schema ({e})") from e


def main(argv: list[str] | None = None) -> None:
    """Read one note with the configured model: `make read-note NOTE="..."`."""
    import json
    import sys

    from chaain.core.config import load_settings
    from chaain.llm import provider_from_config

    text = " ".join(argv if argv is not None else sys.argv[1:]).strip()
    if not text:
        sys.exit('usage: make read-note NOTE="Deliver before 10am please"')
    llm = load_settings()["llm"]
    notes = NotesReader(provider_from_config(llm), llm.get("retry_pause_seconds", 2))
    try:
        flags = notes._ask(f"Order note (changed for this order):\n{text}")
    except LLMError as e:
        sys.exit(f"Notes reader unavailable: {e}")
    print(f"Read by {notes._source()}")
    for f in flags:
        mark = "RAISED" if f.raised else "-"
        print(f"  {f.name:<24} {mark:<7} {f.confidence:.2f}")
    print(f"  {flags[0].explanation}" if flags else "")


if __name__ == "__main__":
    main()
