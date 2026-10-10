"""SQLite state store for resumable runs and submission logging."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

# Status constants for the problems table.
SEEN = "seen"          # discovered in the problem list, not yet attempted
ATTEMPTED = "attempted"  # a submission was made but not accepted
ACCEPTED = "accepted"  # last submission was Accepted
FAILED = "failed"      # generation or submission errored out


class Database:
    """Thread-safe wrapper around a single SQLite file."""

    def __init__(self, db_path: Path):
        self._path = db_path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS problems (
                    question_id      TEXT PRIMARY KEY,
                    frontend_id      TEXT,
                    title            TEXT,
                    title_slug        TEXT,
                    difficulty        TEXT,
                    status            TEXT NOT NULL DEFAULT 'seen',
                    last_attempt_at   INTEGER,
                    accepted_at       INTEGER,
                    runtime           TEXT,
                    memory            TEXT,
                    submission_id     TEXT,
                    attempts          INTEGER NOT NULL DEFAULT 0,
                    last_error        TEXT
                );

                CREATE TABLE IF NOT EXISTS submissions (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id      TEXT NOT NULL,
                    title_slug        TEXT NOT NULL,
                    submission_id     TEXT,
                    status_display    TEXT,
                    runtime           TEXT,
                    memory            TEXT,
                    lang              TEXT,
                    code              TEXT,
                    attempted_at      INTEGER NOT NULL,
                    FOREIGN KEY (question_id) REFERENCES problems(question_id)
                );

                CREATE INDEX IF NOT EXISTS idx_problems_status ON problems(status);
                CREATE INDEX IF NOT EXISTS idx_problems_frontend ON problems(frontend_id);
                CREATE INDEX IF NOT EXISTS idx_submissions_qid ON submissions(question_id);
                """
            )

    # ---- problem-level operations -------------------------------------------

    def upsert_problem(
        self,
        question_id: str,
        frontend_id: str,
        title: str,
        title_slug: str,
        difficulty: str,
    ) -> None:
        """Insert a freshly discovered problem, or no-op if already tracked."""
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO problems
                    (question_id, frontend_id, title, title_slug, difficulty, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (question_id, frontend_id, title, title_slug, difficulty, SEEN),
            )

    def get_problem(self, question_id: str) -> Optional[sqlite3.Row]:
        with self._lock, self._conn() as conn:
            return conn.execute(
                "SELECT * FROM problems WHERE question_id = ?", (question_id,)
            ).fetchone()

    def list_unsolved(self, limit: int = 0, max_attempts: int = 3) -> list[sqlite3.Row]:
        """Problems not yet accepted, ordered so least-attempted come first.

        Problems that have already been attempted ``max_attempts`` times are
        excluded, so a persistently-failing problem won't loop forever.
        """
        with self._lock, self._conn() as conn:
            sql = (
                "SELECT * FROM problems WHERE status != ? AND attempts < ? "
                "ORDER BY attempts ASC, CAST(frontend_id AS INTEGER) ASC"
            )
            params: list = [ACCEPTED, max_attempts]
            if limit and limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            return conn.execute(sql, params).fetchall()

    def mark_attempted(self, question_id: str, error: Optional[str] = None) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                UPDATE problems
                SET status = ?, last_attempt_at = ?, attempts = attempts + 1,
                    last_error = ?
                WHERE question_id = ?
                """,
                (
                    FAILED if error else ATTEMPTED,
                    int(time.time()),
                    error,
                    question_id,
                ),
            )

    def record_result(
        self,
        question_id: str,
        status_display: str,
        runtime: Optional[str],
        memory: Optional[str],
        submission_id: Optional[str],
    ) -> None:
        accepted = status_display.strip().lower() == "accepted"
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                UPDATE problems
                SET status = ?, last_attempt_at = ?, attempts = attempts + 1,
                    runtime = ?, memory = ?, submission_id = ?,
                    accepted_at = ?, last_error = NULL
                WHERE question_id = ?
                """,
                (
                    ACCEPTED if accepted else ATTEMPTED,
                    int(time.time()),
                    runtime,
                    memory,
                    submission_id,
                    int(time.time()) if accepted else None,
                    question_id,
                ),
            )

    # ---- submission log -----------------------------------------------------

    def log_submission(
        self,
        question_id: str,
        title_slug: str,
        submission_id: Optional[str],
        status_display: str,
        runtime: Optional[str],
        memory: Optional[str],
        lang: str,
        code: str,
    ) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                INSERT INTO submissions
                    (question_id, title_slug, submission_id, status_display,
                     runtime, memory, lang, code, attempted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    title_slug,
                    submission_id,
                    status_display,
                    runtime,
                    memory,
                    lang,
                    code,
                    int(time.time()),
                ),
            )

    def summary(self) -> dict:
        with self._lock, self._conn() as conn:
            counts = conn.execute(
                "SELECT status, COUNT(*) AS n FROM problems GROUP BY status"
            ).fetchall()
            return {row["status"]: row["n"] for row in counts}

    def iter_all(self) -> Iterable[sqlite3.Row]:
        with self._lock, self._conn() as conn:
            for row in conn.execute(
                "SELECT * FROM problems ORDER BY CAST(frontend_id AS INTEGER) ASC"
            ):
                yield row
