from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
