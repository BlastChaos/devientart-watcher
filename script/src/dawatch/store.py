"""Persistent state: which deviations were seen, and the cached access token.

SQLite is used through the standard library. WAL mode plus a busy timeout
makes concurrent access safe on local storage. The database must never live
on NFS, where SQLite's advisory locking is unreliable.
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from dawatch.errors import StoreError
from dawatch.models import Deviation, Token

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_deviations (
    deviationid   TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    author        TEXT NOT NULL,
    url           TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    notified_at   TEXT
);

CREATE TABLE IF NOT EXISTS token_cache (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    access_token TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
"""


@runtime_checkable
class SeenStore(Protocol):
    """Remembers which deviations have already been handled."""

    def has_seen(self, deviationid: str) -> bool: ...

    def mark_seen(self, deviation: Deviation, notified: bool) -> None: ...

    def is_empty(self) -> bool: ...


@runtime_checkable
class TokenCache(Protocol):
    """Persists an access token across short-lived process runs."""

    def load_token(self) -> Token | None: ...

    def save_token(self, token: Token) -> None: ...


class SqliteStore:
    """SQLite-backed :class:`SeenStore` and :class:`TokenCache`."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"Cannot open database at {self._db_path}: {exc}") from exc

        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def has_seen(self, deviationid: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_deviations WHERE deviationid = ?", (deviationid,)
        ).fetchone()
        return row is not None

    def mark_seen(self, deviation: Deviation, notified: bool) -> None:
        """Record a deviation as handled.

        Uses INSERT OR REPLACE so that re-running after a crash is safe.
        ``notified`` distinguishes a genuine push from a silent seeding write.
        """
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO seen_deviations
                (deviationid, title, author, url, first_seen_at, notified_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                deviation.deviationid,
                deviation.title,
                deviation.author_name,
                deviation.url or "",
                now,
                now if notified else None,
            ),
        )

    def notified_at(self, deviationid: str) -> str | None:
        row = self._conn.execute(
            "SELECT notified_at FROM seen_deviations WHERE deviationid = ?", (deviationid,)
        ).fetchone()
        if row is None:
            return None
        value = row["notified_at"]
        return str(value) if value is not None else None

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT 1 FROM seen_deviations LIMIT 1").fetchone()
        return row is None

    def load_token(self) -> Token | None:
        row = self._conn.execute(
            "SELECT access_token, expires_at FROM token_cache WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return Token(
            access_token=str(row["access_token"]),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
        )

    def save_token(self, token: Token) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO token_cache (id, access_token, expires_at)
            VALUES (1, ?, ?)
            """,
            (token.access_token, token.expires_at.isoformat()),
        )


class InMemoryStore:
    """Non-persistent test double with the same semantics as SqliteStore."""

    def __init__(self) -> None:
        self._seen: dict[str, str | None] = {}
        self._token: Token | None = None

    def has_seen(self, deviationid: str) -> bool:
        return deviationid in self._seen

    def mark_seen(self, deviation: Deviation, notified: bool) -> None:
        self._seen[deviation.deviationid] = datetime.now(UTC).isoformat() if notified else None

    def notified_at(self, deviationid: str) -> str | None:
        return self._seen.get(deviationid)

    def is_empty(self) -> bool:
        return not self._seen

    def load_token(self) -> Token | None:
        return self._token

    def save_token(self, token: Token) -> None:
        self._token = token
