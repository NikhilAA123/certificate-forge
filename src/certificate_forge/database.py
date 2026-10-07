"""Short-lived connections and transactions keep SQLite's single writer lock brief."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from certificate_forge.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT UNIQUE,
    payload_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    issuer TEXT NOT NULL,
    issued_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN
        ('QUEUED','PROCESSING','COMPLETED','PARTIALLY_COMPLETED','FAILED')),
    total INTEGER NOT NULL CHECK(total > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    lease_token TEXT,
    lease_expires_at REAL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, lease_expires_at, created_at);
CREATE TABLE IF NOT EXISTS recipients (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT,
    email TEXT,
    status TEXT NOT NULL CHECK(status IN ('PENDING','PROCESSING','SUCCEEDED','FAILED','INVALID')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    error_code TEXT,
    error_message TEXT,
    artifact_path TEXT,
    sha256 TEXT,
    UNIQUE(job_id, position)
);
CREATE INDEX IF NOT EXISTS recipients_job_status ON recipients(job_id, status);
CREATE TABLE IF NOT EXISTS worker_heartbeats (
    id TEXT PRIMARY KEY,
    last_seen_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, settings: Settings):
        self.path = settings.database_path
        self._keeper: sqlite3.Connection | None = None

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            if self._keeper is None:
                # Keep WAL open across short transactions. Otherwise closing the last
                # connection repeatedly checkpoints and removes WAL files on the hot path.
                # This connection never serves requests and never holds a transaction.
                self._keeper = sqlite3.connect(
                    self.path, isolation_level=None, check_same_thread=False
                )
                self._keeper.execute("SELECT COUNT(*) FROM jobs").fetchone()

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
