"""The ledger in SQLite: a local file, so no network and no rate limits.

Each write runs in a transaction opened with BEGIN IMMEDIATE, which takes the
write lock up front. Two runs at once therefore queue rather than both
reading "not claimed" and both sending.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from chaain.core.ledger.repository import (
    ConcurrentChange, DuplicateEntry, Entry, Event, InvalidTransition, Ledger, NotFound,
    normalise_guid, normalise_so,
)
from chaain.core.ledger.states import State, can_move

_STATES = ", ".join(f"'{s.value}'" for s in State)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS entries (
    id          INTEGER PRIMARY KEY,
    so_number   TEXT NOT NULL UNIQUE,
    guid        TEXT UNIQUE,
    customer_id TEXT,
    state       TEXT NOT NULL CHECK (state IN ({_STATES})),
    final       INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    entry_id   INTEGER NOT NULL REFERENCES entries (id),
    from_state TEXT,
    to_state   TEXT NOT NULL,
    reason     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    at         TEXT NOT NULL,
    details    TEXT
);
CREATE INDEX IF NOT EXISTS events_by_entry ON events (entry_id, id);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SqliteLedger(Ledger):
    def __init__(self, path: str | Path = ":memory:", clock: Callable[[], datetime] = _utc_now) -> None:
        # isolation_level=None: we open and close transactions ourselves.
        self._conn = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer
        self._conn.executescript(SCHEMA)
        self._clock = clock

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # ---------- reads ----------

    def find(self, guid=None, so_number=None):
        guid, so = normalise_guid(guid), normalise_so(so_number) if so_number else None
        row = None
        if guid:
            row = self._conn.execute("SELECT * FROM entries WHERE guid = ?", (guid,)).fetchone()
        if row is None and so:
            row = self._conn.execute("SELECT * FROM entries WHERE so_number = ?", (so,)).fetchone()
        return _entry(row) if row else None

    def history(self, so_number):
        rows = self._conn.execute(
            "SELECT e.* FROM events e JOIN entries n ON n.id = e.entry_id "
            "WHERE n.so_number = ? ORDER BY e.id",
            (normalise_so(so_number),),
        ).fetchall()
        return [_event(r) for r in rows]

    def in_state(self, *states):
        marks = ", ".join("?" for _ in states)
        rows = self._conn.execute(
            f"SELECT * FROM entries WHERE state IN ({marks}) ORDER BY updated_at, id",
            [s.value for s in states],
        ).fetchall()
        return [_entry(r) for r in rows]

    # ---------- writes ----------

    def open(self, so_number, guid, customer_id, reason):
        return self._insert(so_number, guid, customer_id, State.CANDIDATE, "system", "system", reason)

    def record_manual(self, so_number, guid, actor, reason):
        return self._insert(so_number, guid, None, State.SENT, "manual", actor, reason)

    def move(self, so_number, to, reason, actor="system", *, final=False, fingerprint=None, details=None):
        _require_reason(reason)
        if final and to is not State.SKIPPED:
            raise ValueError("only a SKIPPED row can be final")
        so = normalise_so(so_number)
        now = self._clock()
        with self._transaction():
            row = self._conn.execute("SELECT * FROM entries WHERE so_number = ?", (so,)).fetchone()
            if row is None:
                raise NotFound(so)
            current = State(row["state"])
            if not can_move(current, to, bool(row["final"])):
                why = " (final skip)" if row["final"] else ""
                raise InvalidTransition(f"{so}: {current.value} -> {to.value} is not allowed{why}")
            updated = self._conn.execute(
                "UPDATE entries SET state = ?, final = ?, reason = ?, updated_at = ?, "
                "fingerprint = COALESCE(?, fingerprint) WHERE id = ? AND state = ?",
                (to.value, int(final), reason, now.isoformat(), fingerprint, row["id"], current.value),
            )
            if updated.rowcount != 1:
                raise ConcurrentChange(so)
            self._insert_event(row["id"], current, to, reason, actor, now, details)
        return self.find(so_number=so)

    def _insert(self, so_number, guid, customer_id, state, source, actor, reason):
        _require_reason(reason)
        so, guid = normalise_so(so_number), normalise_guid(guid)
        now = self._clock()
        with self._transaction():
            if self.find(guid, so) is not None:
                raise DuplicateEntry(f"{so} / {guid}")
            try:
                cur = self._conn.execute(
                    "INSERT INTO entries (so_number, guid, customer_id, state, source, reason, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (so, guid, customer_id, state.value, source, reason, now.isoformat(), now.isoformat()),
                )
            except sqlite3.IntegrityError as e:  # the unique constraints are the backstop
                raise DuplicateEntry(f"{so} / {guid}") from e
            self._insert_event(cur.lastrowid, None, state, reason, actor, now, None)
        return self.find(so_number=so)

    def _insert_event(self, entry_id, from_state, to_state, reason, actor, at, details):
        self._conn.execute(
            "INSERT INTO events (entry_id, from_state, to_state, reason, actor, at, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entry_id, from_state.value if from_state else None, to_state.value, reason, actor,
             at.isoformat(), json.dumps(details) if details else None),
        )


def _require_reason(reason: str) -> None:
    if not reason or not reason.strip():
        raise ValueError("every state change needs a reason")


def _entry(row: sqlite3.Row) -> Entry:
    return Entry(
        so_number=row["so_number"], guid=row["guid"], customer_id=row["customer_id"],
        state=State(row["state"]), final=bool(row["final"]), source=row["source"],
        reason=row["reason"], created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]), fingerprint=row["fingerprint"],
    )


def _event(row: sqlite3.Row) -> Event:
    return Event(
        from_state=State(row["from_state"]) if row["from_state"] else None,
        to_state=State(row["to_state"]), reason=row["reason"], actor=row["actor"],
        at=datetime.fromisoformat(row["at"]),
        details=json.loads(row["details"]) if row["details"] else None,
    )
