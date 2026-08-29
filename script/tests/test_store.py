import os
import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dawatch.errors import StoreError
from dawatch.models import Deviation, Token
from dawatch.store import InMemoryStore, SqliteStore

NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)


def make_deviation(deviationid: str = "ABC-123") -> Deviation:
    return Deviation.model_validate(
        {
            "deviationid": deviationid,
            "title": "Neon Alley",
            "url": "https://example.invalid/art",
            "author": {"username": "artist"},
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[SqliteStore]:
    with SqliteStore(tmp_path / "test.db") as store:
        yield store


def test_new_store_is_empty(store: SqliteStore) -> None:
    assert store.is_empty() is True


def test_unseen_deviation_reports_false(store: SqliteStore) -> None:
    assert store.has_seen("ABC-123") is False


def test_marked_deviation_reports_true(store: SqliteStore) -> None:
    store.mark_seen(make_deviation(), notified=True)

    assert store.has_seen("ABC-123") is True
    assert store.is_empty() is False


def test_mark_seen_is_idempotent(store: SqliteStore) -> None:
    """A retry after a crash must not raise on the primary key."""
    store.mark_seen(make_deviation(), notified=True)
    store.mark_seen(make_deviation(), notified=True)

    assert store.has_seen("ABC-123") is True


def test_state_survives_reopening(tmp_path: Path) -> None:
    """The whole point of the store: a new process sees the old decisions."""
    db_path = tmp_path / "test.db"
    with SqliteStore(db_path) as first:
        first.mark_seen(make_deviation(), notified=True)

    with SqliteStore(db_path) as second:
        assert second.has_seen("ABC-123") is True


def test_seeded_rows_are_distinguishable_from_notified_rows(store: SqliteStore) -> None:
    store.mark_seen(make_deviation("SEEDED"), notified=False)
    store.mark_seen(make_deviation("PUSHED"), notified=True)

    assert store.notified_at("SEEDED") is None
    assert store.notified_at("PUSHED") is not None


def test_token_cache_is_empty_initially(store: SqliteStore) -> None:
    assert store.load_token() is None


def test_token_round_trips(store: SqliteStore) -> None:
    token = Token(access_token="tok", expires_at=NOW + timedelta(seconds=3600))

    store.save_token(token)
    loaded = store.load_token()

    assert loaded is not None
    assert loaded.access_token == "tok"
    assert loaded.expires_at == token.expires_at


def test_saving_a_token_replaces_the_previous_one(store: SqliteStore) -> None:
    store.save_token(Token(access_token="old", expires_at=NOW))
    store.save_token(Token(access_token="new", expires_at=NOW + timedelta(seconds=10)))

    loaded = store.load_token()
    assert loaded is not None
    assert loaded.access_token == "new"


def test_creates_parent_directories(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "deeper" / "test.db"

    with SqliteStore(db_path) as store:
        assert store.is_empty() is True

    assert db_path.exists()


def test_in_memory_store_matches_sqlite_behaviour() -> None:
    """The test double must not diverge from the real thing."""
    store = InMemoryStore()

    assert store.is_empty() is True
    assert store.has_seen("ABC-123") is False

    store.mark_seen(make_deviation(), notified=True)

    assert store.has_seen("ABC-123") is True
    assert store.is_empty() is False
    assert store.load_token() is None

    token = Token(access_token="tok", expires_at=NOW)
    store.save_token(token)
    loaded = store.load_token()
    assert loaded is not None
    assert loaded.access_token == "tok"


def test_corrupt_file_raises_store_error(tmp_path: Path) -> None:
    """Opening a corrupted database file raises StoreError."""
    db_path = tmp_path / "corrupt.db"
    # Write garbage bytes to the database file
    db_path.write_bytes(b"This is not a valid SQLite database file")

    with pytest.raises(StoreError):
        SqliteStore(db_path)


def test_unwritable_directory_raises_store_error(tmp_path: Path) -> None:
    """Opening a store in a read-only directory raises StoreError."""
    if os.geteuid() == 0:
        pytest.skip("Cannot test permissions as root")

    db_dir = tmp_path / "readonly"
    db_dir.mkdir()
    db_path = db_dir / "test.db"

    # Create read-only directory
    db_dir.chmod(0o444)
    try:
        with pytest.raises(StoreError):
            SqliteStore(db_path)
    finally:
        # Restore permissions for cleanup
        db_dir.chmod(0o755)


def test_first_seen_at_preserved_on_reopen(tmp_path: Path) -> None:
    """Reopening and re-marking a deviation preserves first_seen_at."""
    db_path = tmp_path / "test.db"

    # First store: mark the deviation
    with SqliteStore(db_path) as first:
        first.mark_seen(make_deviation(), notified=False)
        first_timestamp = first.first_seen_at("ABC-123")
        assert first_timestamp is not None

    # Second store: re-mark the same deviation (simulating a retry)
    with SqliteStore(db_path) as second:
        second.mark_seen(make_deviation(), notified=True)
        second_timestamp = second.first_seen_at("ABC-123")
        assert second_timestamp == first_timestamp
        # Verify that notified_at was updated
        assert second.notified_at("ABC-123") is not None


def test_in_memory_store_first_seen_at_preserved() -> None:
    """InMemoryStore preserves first_seen_at when re-marking."""
    store = InMemoryStore()

    # Mark initially without notification
    store.mark_seen(make_deviation(), notified=False)
    first_timestamp = store.first_seen_at("ABC-123")
    assert first_timestamp is not None
    assert store.notified_at("ABC-123") is None

    # Re-mark with notification
    store.mark_seen(make_deviation(), notified=True)
    second_timestamp = store.first_seen_at("ABC-123")
    assert second_timestamp == first_timestamp
    # Verify that notified_at was updated
    assert store.notified_at("ABC-123") is not None


def test_refresh_token_round_trips(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "db.sqlite") as store:
        assert store.load_refresh_token() is None
        store.save_refresh_token("refresh-abc")
        assert store.load_refresh_token() == "refresh-abc"


def test_refresh_token_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    with SqliteStore(db) as store:
        store.save_refresh_token("refresh-abc")
    with SqliteStore(db) as reopened:
        assert reopened.load_refresh_token() == "refresh-abc"


def test_saving_an_access_token_preserves_the_refresh_token(tmp_path: Path) -> None:
    """The two credentials have independent lifecycles.

    An access token is replaced hourly; the refresh token must not be
    collaterally erased when that happens.
    """
    with SqliteStore(tmp_path / "db.sqlite") as store:
        store.save_refresh_token("refresh-abc")
        store.save_token(Token(access_token="access-xyz", expires_at=NOW + timedelta(hours=1)))

        assert store.load_refresh_token() == "refresh-abc"
        loaded = store.load_token()
        assert loaded is not None
        assert loaded.access_token == "access-xyz"


def test_migrates_a_database_created_before_the_refresh_column(tmp_path: Path) -> None:
    """An existing PVC has a token_cache without refresh_token.

    CREATE TABLE IF NOT EXISTS will not add the column, so opening the store
    must add it. Without this, every deployment with an existing volume breaks
    on the first query.
    """
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE token_cache (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    with SqliteStore(db) as store:
        assert store.load_refresh_token() is None
        store.save_refresh_token("refresh-abc")
        assert store.load_refresh_token() == "refresh-abc"


def test_migration_preserves_an_existing_access_token(tmp_path: Path) -> None:
    """Migrating a live PVC must not discard the token already cached there."""
    db = tmp_path / "db.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE token_cache (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT NOT NULL,
            expires_at   TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO token_cache (id, access_token, expires_at) VALUES (1, ?, ?)",
        ("existing-access", (NOW + timedelta(hours=1)).isoformat()),
    )
    conn.commit()
    conn.close()

    with SqliteStore(db) as store:
        loaded = store.load_token()
        assert loaded is not None
        assert loaded.access_token == "existing-access"
        assert store.load_refresh_token() is None


def test_migration_is_idempotent_on_a_fresh_database(tmp_path: Path) -> None:
    db = tmp_path / "db.sqlite"
    with SqliteStore(db):
        pass
    with SqliteStore(db) as store:
        assert store.load_refresh_token() is None


def test_in_memory_store_round_trips_a_refresh_token() -> None:
    store = InMemoryStore()
    assert store.load_refresh_token() is None
    store.save_refresh_token("refresh-abc")
    assert store.load_refresh_token() == "refresh-abc"
