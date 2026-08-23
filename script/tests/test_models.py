from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from dawatch.models import DailyDeviationsPage, Deviation, Token

MINIMAL_DEVIATION = {"deviationid": "ABC-123"}

FULL_DEVIATION = {
    "deviationid": "DEF-456",
    "title": "Neon Alley",
    "url": "https://www.deviantart.com/artist/art/neon-alley-1",
    "author": {"userid": "u1", "username": "artist", "usericon": "https://x/i.png"},
    "is_mature": True,
    "published_time": "1724371200",
    "preview": {"src": "https://images/preview.jpg", "height": 400, "width": 600},
    "content": {"src": "https://images/full.jpg", "filesize": 90210},
    "stats": {"comments": 3, "favourites": 40},
    "some_future_field": {"nested": True},
}


def test_parses_a_full_deviation() -> None:
    deviation = Deviation.model_validate(FULL_DEVIATION)

    assert deviation.deviationid == "DEF-456"
    assert deviation.title == "Neon Alley"
    assert deviation.author_name == "artist"
    assert deviation.is_mature is True


def test_tolerates_unknown_fields() -> None:
    """The API adds fields over time; that must never break a scheduled run."""
    deviation = Deviation.model_validate(FULL_DEVIATION)

    assert not hasattr(deviation, "some_future_field")


def test_applies_defaults_for_a_minimal_deviation() -> None:
    deviation = Deviation.model_validate(MINIMAL_DEVIATION)

    assert deviation.title == "Untitled"
    assert deviation.author_name == "unknown"
    assert deviation.image_url is None
    assert deviation.is_mature is False


def test_deviationid_is_required() -> None:
    with pytest.raises(ValidationError):
        Deviation.model_validate({"title": "no id"})


def test_image_url_prefers_preview_over_content() -> None:
    deviation = Deviation.model_validate(FULL_DEVIATION)

    assert deviation.image_url == "https://images/preview.jpg"


def test_image_url_falls_back_to_content() -> None:
    payload = {"deviationid": "X", "content": {"src": "https://images/full.jpg"}}

    assert Deviation.model_validate(payload).image_url == "https://images/full.jpg"


def test_parses_a_page() -> None:
    page = DailyDeviationsPage.model_validate(
        {"results": [MINIMAL_DEVIATION, FULL_DEVIATION], "has_more": False}
    )

    assert len(page.results) == 2
    assert page.has_more is False


def test_page_defaults_to_empty_results() -> None:
    page = DailyDeviationsPage.model_validate({})

    assert page.results == []


def test_token_from_response_computes_expiry() -> None:
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

    token = Token.from_response(
        {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}, now
    )

    assert token.access_token == "tok"
    assert token.expires_at == now + timedelta(seconds=3600)


def test_token_is_valid_well_before_expiry() -> None:
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    token = Token(access_token="tok", expires_at=now + timedelta(seconds=3600))

    assert token.is_valid(now) is True


def test_token_is_invalid_inside_the_leeway_window() -> None:
    """A token expiring in 30s is treated as dead, so a slow run cannot 401."""
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    token = Token(access_token="tok", expires_at=now + timedelta(seconds=30))

    assert token.is_valid(now, leeway_seconds=60) is False


def test_token_is_invalid_after_expiry() -> None:
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
    token = Token(access_token="tok", expires_at=now - timedelta(seconds=1))

    assert token.is_valid(now) is False
