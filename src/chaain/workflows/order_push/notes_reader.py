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

PROMPTS = Path(__file__).parent / "prompts"
PROMPT_VERSION = "notes-v2"  # the one the gate uses; older ones stay for the eval
# How each version labels the text it sends. The label is part of the prompt:
# v1's "changed for this order" was read as the customer asking for a one-off.
_STANDING = "Customer's standing comment (copied onto every order):\n{}"
FRAMING = {
    "notes-v1": ("Order note (changed for this order):\n{}", _STANDING),
    "notes-v2": ("Order note:\n{}", _STANDING),
}
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


def changed_text(sale: Sale, order_label: str = FRAMING[PROMPT_VERSION][0]) -> str:
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
        parts.append(order_label.format(added))
    if sale.memo.strip():
        parts.append(f"Order memo:\n{sale.memo.strip()}")
    return "\n\n".join(parts)


class NotesReader:
    def __init__(
        self, provider: LLMProvider, retry_pause_seconds: float = 2,
        sleep: Callable[[float], None] = time.sleep, prompt_version: str = PROMPT_VERSION,
    ) -> None:
        if prompt_version not in FRAMING:
            raise ValueError(f"unknown prompt version {prompt_version!r}")
        self.provider = provider
        self.prompt_version = prompt_version
        self.prompt = (PROMPTS / f"{prompt_version}.md").read_text()
        self.order_label, self.standing_label = FRAMING[prompt_version]
        self.retry_pause_seconds = retry_pause_seconds
        self.sleep = sleep
        self._standing_cache: dict[str, tuple[NoteFlag, ...]] = {}

    def read(self, sale: Sale) -> NotesResult:
        standing, standing_error = self._standing(sale.standing_note)
        text = changed_text(sale, self.order_label)
        if not text:
            return NotesResult(standing=standing, source=self.source(), standing_error=standing_error)
        try:
            flags = self.ask(text)
        except LLMError as e:
            return NotesResult(error=str(e), standing=standing, source=self.source())
        return NotesResult(flags=flags, standing=standing, source=self.source(), standing_error=standing_error)

    def _standing(self, note: str) -> tuple[tuple[NoteFlag, ...], str | None]:
        """Read a standing comment once per process, keyed by its text and the prompt version."""
        if not note.strip():
            return (), None
        key = hashlib.sha256(f"{self.prompt_version}\n{note.strip()}".encode()).hexdigest()
        if key not in self._standing_cache:
            try:
                flags = self.ask(self.frame(note, standing=True))
            except LLMError as e:
                return (), str(e)  # not cached: try again on the next order
            self._standing_cache[key] = tuple(f for f in flags if f.raised)
        return self._standing_cache[key], None

    def ask(self, text: str) -> tuple[NoteFlag, ...]:
        """One try, one retry after a pause. Malformed answers count as failures."""
        try:
            return _parse(self.provider.complete_json(self.prompt, text, SCHEMA))
        except LLMError:
            self.sleep(self.retry_pause_seconds)
            return _parse(self.provider.complete_json(self.prompt, text, SCHEMA))

    def frame(self, text: str, standing: bool = False) -> str:
        """Label text the way this prompt version expects."""
        return (self.standing_label if standing else self.order_label).format(text.strip())

    def source(self) -> str:
        return f"{self.prompt_version} via {self.provider.name}/{self.provider.model}"


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
        flags = notes.ask(notes.frame(text))
    except LLMError as e:
        sys.exit(f"Notes reader unavailable: {e}")
    print(f"Read by {notes.source()}")
    for f in flags:
        mark = "RAISED" if f.raised else "-"
        print(f"  {f.name:<24} {mark:<7} {f.confidence:.2f}")
    print(f"  {flags[0].explanation}" if flags else "")


if __name__ == "__main__":
    main()
