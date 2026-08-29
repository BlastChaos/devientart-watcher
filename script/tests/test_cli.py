from pathlib import Path

import httpx
import pytest
import respx

from dawatch.auth import TOKEN_URL
from dawatch.cli import EXIT_CONFIG, EXIT_FAILURE, main
from dawatch.client import API_BASE

FEED_URL = f"{API_BASE}/browse/deviantsyouwatch"
NTFY_URL = "https://ntfy.test/my-topic"

TOKEN_RESPONSE = {"access_token": "tok", "token_type": "Bearer", "expires_in": 3600}

FEED_RESPONSE = {
    "has_more": False,
    "results": [
        {
            "deviationid": "A",
            "title": "First",
            "author": {"username": "alice"},
            "url": "https://example.invalid/a",
        }
    ],
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DEVIANTART_CLIENT_ID", "cid")
    monkeypatch.setenv("DEVIANTART_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("DAWATCH_NTFY_TOPIC", "my-topic")
    monkeypatch.setenv("DAWATCH_REFRESH_TOKEN", "seed-refresh")
    monkeypatch.setenv("DAWATCH_NTFY_URL", "https://ntfy.test")
    monkeypatch.setenv("DAWATCH_DB_PATH", str(tmp_path / "dawatch.db"))
    monkeypatch.setenv("DAWATCH_ENV", "prod")
    monkeypatch.delenv("DAWATCH_PUSHGATEWAY_URL", raising=False)
    return tmp_path


def test_missing_config_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DEVIANTART_CLIENT_ID", raising=False)
    monkeypatch.delenv("DEVIANTART_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("DAWATCH_NTFY_TOPIC", raising=False)

    assert main(["run"]) == 2
    assert "DEVIANTART_CLIENT_ID" in capsys.readouterr().err


@respx.mock
def test_first_run_seeds_and_exits_0(env: Path) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    ntfy = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    assert main(["run"]) == 0
    assert not ntfy.called, "a first run must seed, not notify"


@respx.mock
def test_second_run_notifies_and_exits_0(env: Path) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    ntfy = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    main(["seed"])
    second_feed = {
        "has_more": False,
        "results": [{"deviationid": "B", "title": "New", "author": {"username": "bob"}}],
    }
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=second_feed))

    assert main(["run"]) == 0
    assert ntfy.call_count == 1
    assert ntfy.calls.last.request.headers["X-Title"] == "New"


@respx.mock
def test_a_failed_notification_exits_1(env: Path) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    respx.post(NTFY_URL).mock(return_value=httpx.Response(500))

    main(["seed"])
    respx.get(FEED_URL).mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"deviationid": "B", "author": {"username": "b"}}]},
        )
    )

    assert main(["run"]) == 1


@respx.mock
def test_a_fetch_failure_exits_1(env: Path) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(503))

    assert main(["run"]) == 1


@respx.mock
def test_dry_run_sends_nothing(env: Path) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    ntfy = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    assert main(["run", "--dry-run"]) == 0
    assert not ntfy.called


@respx.mock
def test_no_seed_notifies_on_a_fresh_store(env: Path) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))
    ntfy = respx.post(NTFY_URL).mock(return_value=httpx.Response(200, json={}))

    assert main(["run", "--no-seed"]) == 0
    assert ntfy.call_count == 1


def test_date_flag_is_rejected(env: Path) -> None:
    """The watch feed has no date parameter, so the flag no longer exists.

    argparse exits 2 for an unknown flag, which is the code this application
    already uses for a configuration failure.
    """
    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--date", "2026-08-01"])

    assert exc_info.value.code == EXIT_CONFIG


@respx.mock
def test_expired_refresh_token_exits_config_not_failure(env: Path) -> None:
    """Exit 2 stops backoffLimit retrying a token dead for three months."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    assert main(["run"]) == EXIT_CONFIG


@respx.mock
def test_a_transient_token_failure_still_exits_1(env: Path) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))

    assert main(["run"]) == EXIT_FAILURE


@respx.mock
def test_run_requests_the_watch_feed(env: Path) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    feed = respx.get(FEED_URL).mock(return_value=httpx.Response(200, json=FEED_RESPONSE))

    main(["run"])

    assert feed.called
    assert feed.calls.last.request.url.params["limit"] == "50"


@respx.mock
def test_doctor_reports_an_insufficient_scope(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A wrong scope must fail here, not silently at the next scheduled run."""
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(403))

    assert main(["doctor"]) != 0
    assert "scope" in capsys.readouterr().out


@respx.mock
def test_doctor_reports_healthy(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={"status": "success"}))

    assert main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert "OK" in out


@respx.mock
def test_doctor_reports_bad_credentials(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "bad"}))

    assert main(["doctor"]) == 1
    assert "FAIL" in capsys.readouterr().out


@respx.mock
def test_doctor_reports_unreachable_api(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The token endpoint is up but the API itself cannot be reached.

    Distinct from test_doctor_reports_bad_credentials: token() succeeds, so
    this exercises doctor's own /placebo httpx.HTTPError branch rather than
    auth's.
    """
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("connection refused"))

    assert main(["doctor"]) == 1

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "api" in out
    assert "ConnectError" in out


@respx.mock
def test_doctor_reports_unreachable_pushgateway(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DAWATCH_PUSHGATEWAY_URL", "https://pushgateway.test")
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=TOKEN_RESPONSE))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, json={"status": "success"}))
    respx.get("https://pushgateway.test").mock(return_value=httpx.Response(503))

    assert main(["doctor"]) == 1

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "pushgateway" in out
