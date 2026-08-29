"""Persistent state: which deviations were seen, and the cached tokens.

SQLite is used through the standard library. WAL mode plus a busy timeout
makes concurrent access safe on local storage. The database must never live
on NFS, where SQLite's advisory locking is unreliable.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
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
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    refresh_token TEXT
);
"""


@contextmanager
def _store_errors(action: str) -> Iterator[None]:
    """Re-raise sqlite3 failures as StoreError.

    StoreError is the single failure type this module promises the CLI, which
    maps it to a non-zero exit code. A raw sqlite3.Error would bypass that.
    """
    try:
        yield
    except sqlite3.Error as exc:
        raise StoreError(f"{action} failed: {exc}") from exc


@runtime_checkable
class SeenStore(Protocol):
    """Remembers which deviations have already been handled."""

    def has_seen(self, deviationid: str) -> bool: ...

    def mark_seen(self, deviation: Deviation, notified: bool) -> None: ...

    def is_empty(self) -> bool: ...


@runtime_checkable
class TokenCache(Protocol):
    """Persists tokens across short-lived process runs.

    The two credentials have separate lifecycles: an access token carries an
    expiry the code checks every run, while a refresh token lasts three months
    and nothing tracks it. They are stored and replaced independently.
    """

    def load_token(self) -> Token | None: ...

    def save_token(self, token: Token) -> None: ...

    def load_refresh_token(self) -> str | None: ...

    def save_refresh_token(self, refresh_token: str) -> None: ...


class SqliteStore:
    """SQLite-backed :class:`SeenStore` and :class:`TokenCache`."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self._db_path, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"Cannot open database at {self._db_path}: {exc}") from exc

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

    def _migrate(self) -> None:
        """Add columns that post-date the original schema.

        SCHEMA uses CREATE TABLE IF NOT EXISTS, so a database created by an
        earlier version keeps its original columns forever. The new column is
        nullable, so there is nothing to backfill and nothing to lose.
        """
        columns = {str(row["name"]) for row in self._conn.execute("PRAGMA table_info(token_cache)")}
        if "refresh_token" not in columns:
            self._conn.execute("ALTER TABLE token_cache ADD COLUMN refresh_token TEXT")

    def has_seen(self, deviationid: str) -> bool:
        with _store_errors("check seen status"):
            row = self._conn.execute(
                "SELECT 1 FROM seen_deviations WHERE deviationid = ?", (deviationid,)
            ).fetchone()
            return row is not None

    def mark_seen(self, deviation: Deviation, notified: bool) -> None:
        """Record a deviation as handled.

        Uses ON CONFLICT to preserve the earliest first_seen_at timestamp so
        re-running after a crash is safe. ``notified`` distinguishes a genuine
        push from a silent seeding write.
        """
        with _store_errors("mark deviation as seen"):
            now = datetime.now(UTC).isoformat()
            self._conn.execute(
                """
                INSERT INTO seen_deviations
                    (deviationid, title, author, url, first_seen_at, notified_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(deviationid) DO UPDATE SET
                    title = excluded.title,
                    author = excluded.author,
                    url = excluded.url,
                    notified_at = excluded.notified_at
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
        with _store_errors("fetch notification status"):
            row = self._conn.execute(
                "SELECT notified_at FROM seen_deviations WHERE deviationid = ?",
                (deviationid,),
            ).fetchone()
            if row is None:
                return None
            value = row["notified_at"]
            return str(value) if value is not None else None

    def first_seen_at(self, deviationid: str) -> str | None:
        with _store_errors("fetch first seen timestamp"):
            row = self._conn.execute(
                "SELECT first_seen_at FROM seen_deviations WHERE deviationid = ?",
                (deviationid,),
            ).fetchone()
            if row is None:
                return None
            return str(row["first_seen_at"])

    def is_empty(self) -> bool:
        with _store_errors("check if store is empty"):
            row = self._conn.execute("SELECT 1 FROM seen_deviations LIMIT 1").fetchone()
            return row is None

    def load_token(self) -> Token | None:
        with _store_errors("load cached token"):
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
        with _store_errors("save token"):
            self._conn.execute(
                """
                INSERT INTO token_cache (id, access_token, expires_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    access_token = excluded.access_token,
                    expires_at = excluded.expires_at
                """,
                (token.access_token, token.expires_at.isoformat()),
            )

    def load_refresh_token(self) -> str | None:
        with _store_errors("load refresh token"):
            row = self._conn.execute(
                "SELECT refresh_token FROM token_cache WHERE id = 1"
            ).fetchone()
            if row is None or row["refresh_token"] is None:
                return None
            return str(row["refresh_token"])

    def save_refresh_token(self, refresh_token: str) -> None:
        """Store the refresh token, creating the row if no access token exists.

        The placeholder access token is deliberately already expired, so a
        later load_token() reports it unusable and triggers a refresh rather
        than sending an empty bearer token.
        """
        with _store_errors("save refresh token"):
            self._conn.execute(
                """
                INSERT INTO token_cache (id, access_token, expires_at, refresh_token)
                VALUES (1, '', '1970-01-01T00:00:00+00:00', ?)
                ON CONFLICT(id) DO UPDATE SET
                    refresh_token = excluded.refresh_token
                """,
                (refresh_token,),
            )


class InMemoryStore:
    """Non-persistent test double with the same semantics as SqliteStore."""

    def __init__(self) -> None:
        # Map deviationid -> (first_seen_at, notified_at)
        self._seen: dict[str, tuple[str, str | None]] = {}
        self._token: Token | None = None
        self._refresh_token: str | None = None

    def has_seen(self, deviationid: str) -> bool:
        return deviationid in self._seen

    def mark_seen(self, deviation: Deviation, notified: bool) -> None:
        now = datetime.now(UTC).isoformat()
        if deviation.deviationid in self._seen:
            # Preserve the original first_seen_at
            first_seen_at, _ = self._seen[deviation.deviationid]
        else:
            first_seen_at = now
        notified_at = now if notified else None
        self._seen[deviation.deviationid] = (first_seen_at, notified_at)

    def notified_at(self, deviationid: str) -> str | None:
        if deviationid not in self._seen:
            return None
        _, notified_at = self._seen[deviationid]
        return notified_at

    def first_seen_at(self, deviationid: str) -> str | None:
        if deviationid not in self._seen:
            return None
        first_seen_at, _ = self._seen[deviationid]
        return first_seen_at

    def is_empty(self) -> bool:
        return not self._seen

    def load_token(self) -> Token | None:
        return self._token

    def save_token(self, token: Token) -> None:
        self._token = token

    def load_refresh_token(self) -> str | None:
        return self._refresh_token

    def save_refresh_token(self, refresh_token: str) -> None:
        self._refresh_token = refresh_token
