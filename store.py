"""SQLite persistence: conversations, their messages, and post intents.

Every message is committed the moment it exists, so the conversation on disk is always
the conversation so far. Messages are stored in a provider-neutral shape (see agent.py).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DB_PATH = Path(__file__).with_name("agent.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    name          TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    system_prompt TEXT,                 -- pinned when the conversation starts (see pinned_setup)
    tools         TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation TEXT NOT NULL REFERENCES conversations(name),
    body         TEXT NOT NULL,          -- one canonical message as JSON
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS post_intents (
    tool_call_id   TEXT PRIMARY KEY,     -- also sent to the ledger as the reference
    conversation   TEXT NOT NULL,
    vendor_id      TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    status         TEXT NOT NULL,        -- posting | posted | failed
    entry_id       TEXT,
    created_at     TEXT NOT NULL
);
-- At most one post of an invoice in flight, across every conversation and process.
-- Finished posts don't hold the claim: the ledger itself is the record of those.
CREATE UNIQUE INDEX IF NOT EXISTS one_post_in_flight
    ON post_intents (vendor_id, invoice_number) WHERE status = 'posting';
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(conversations)")}
        for column in ("system_prompt", "tools"):  # databases made before these columns existed
            if column not in columns:
                self.db.execute(f"ALTER TABLE conversations ADD COLUMN {column} TEXT")

    # -- conversations ------------------------------------------------------------

    def open_conversation(self, name: str, default_provider: str) -> tuple[str, bool]:
        """Return (provider, is_new), creating the conversation if needed."""
        row = self.db.execute("SELECT provider FROM conversations WHERE name = ?", (name,)).fetchone()
        if row:
            return row["provider"], False
        with self.db:
            self.db.execute(
                "INSERT INTO conversations (name, provider, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, default_provider, _now(), _now()),
            )
        return default_provider, True

    def pinned_setup(self, name: str, system: str, tool_defs: list[dict]) -> tuple[str, list[dict]]:
        """The system prompt and tool definitions this conversation started with.

        Claude's thinking blocks are bound to the exact prompt and tools they were made
        under: resuming a conversation with a changed prompt is rejected (HTTP 400). So the
        first call saves them, and every later call, after any restart or code update,
        gets the saved ones back. New conversations pick up a changed prompt.
        """
        row = self.db.execute("SELECT system_prompt, tools FROM conversations WHERE name = ?", (name,)).fetchone()
        if row["system_prompt"] is not None:
            return row["system_prompt"], json.loads(row["tools"])
        with self.db:
            self.db.execute(
                "UPDATE conversations SET system_prompt = ?, tools = ? WHERE name = ?",
                (system, json.dumps(tool_defs), name),
            )
        return system, tool_defs

    def set_provider(self, name: str, provider: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE conversations SET provider = ?, updated_at = ? WHERE name = ?", (provider, _now(), name)
            )

    def lock_conversation(self, name: str) -> bool:
        """Take an OS lock on this conversation for as long as the process lives.

        Two programs appending to one conversation would interleave messages and could
        both show the same approval. The kernel drops the lock when the process exits,
        even on kill -9, so a crash never leaves a stale lock behind.
        """
        try:
            import fcntl
        except ImportError:  # Windows: no lock (see README)
            return True
        digest = hashlib.sha1(name.encode()).hexdigest()[:12]
        self._lock_file = open(self.path.with_name(f"{self.path.name}.{digest}.lock"), "w")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            return False
        return True

    # -- messages -----------------------------------------------------------------

    def messages(self, name: str) -> list[dict]:
        rows = self.db.execute("SELECT body FROM messages WHERE conversation = ? ORDER BY id", (name,))
        return [json.loads(row["body"]) for row in rows]

    def append(self, name: str, message: dict) -> None:
        with self.db:
            self._insert_message(name, message)

    def _insert_message(self, name: str, message: dict) -> None:
        self.db.execute(
            "INSERT INTO messages (conversation, body, created_at) VALUES (?, ?, ?)",
            (name, json.dumps(message), _now()),
        )
        self.db.execute("UPDATE conversations SET updated_at = ? WHERE name = ?", (_now(), name))

    # -- post intents (write-ahead record of every ledger.post) ---------------------

    def begin_post(self, tool_call_id: str, name: str, vendor_id: str, invoice_number: str) -> bool:
        """Claim this invoice for posting. Committed *before* ledger.post is called.

        Returns False if another post of the same invoice is in flight: the unique index
        makes check-and-claim one atomic step, even across processes. The saved row is
        also how a crash mid-post is found and settled on the next start.
        """
        try:
            with self.db:
                self.db.execute(
                    "INSERT INTO post_intents VALUES (?, ?, ?, ?, 'posting', NULL, ?)",
                    (tool_call_id, name, vendor_id, invoice_number, _now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def finish_post(self, tool_call_id: str, status: str, entry_id: str | None, name: str, message: dict) -> None:
        """Mark the intent done and save the tool result in one transaction."""
        with self.db:
            self.db.execute(
                "UPDATE post_intents SET status = ?, entry_id = ? WHERE tool_call_id = ?",
                (status, entry_id, tool_call_id),
            )
            self._insert_message(name, message)

    def unfinished_posts(self) -> list[dict]:
        """Posts that started but never recorded an outcome: the program died mid-post."""
        rows = self.db.execute("SELECT * FROM post_intents WHERE status = 'posting'")
        return [dict(row) for row in rows]

    def post_in_flight(self, vendor_id: str, invoice_number: str) -> bool:
        """True if any conversation started posting this invoice and hasn't resolved it."""
        row = self.db.execute(
            "SELECT 1 FROM post_intents WHERE vendor_id = ? AND invoice_number = ? AND status = 'posting'",
            (vendor_id, invoice_number),
        ).fetchone()
        return row is not None

    def earlier_posts(self, vendor_id: str, invoice_number: str) -> list[str]:
        """Entry ids this program has recorded posting for the invoice, in any conversation."""
        rows = self.db.execute(
            "SELECT entry_id FROM post_intents WHERE vendor_id = ? AND invoice_number = ? AND status = 'posted'",
            (vendor_id, invoice_number),
        )
        return [row["entry_id"] for row in rows]
