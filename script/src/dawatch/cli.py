"""Command line entry point.

``run`` performs exactly one poll and exits. The schedule lives in Kubernetes,
not in this process, so there is no loop, no signal handling, and no drift.
"""

import argparse
import sys
import uuid
from contextlib import ExitStack

import httpx
import structlog

from dawatch.auth import RefreshTokenAuth, TokenProvider
from dawatch.client import API_BASE, API_VERSION, DeviantArtClient
from dawatch.config import Settings
from dawatch.errors import ConfigError, DawatchError
from dawatch.logging import configure_logging
from dawatch.login import run_consent_flow
from dawatch.metrics import MetricsSink, NullSink, PushgatewaySink
from dawatch.notifier import ConsoleNotifier, Notifier, NtfyNotifier
from dawatch.service import WatchService
from dawatch.sources import WatchedDeviationsSource
from dawatch.store import SqliteStore

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2

log = structlog.get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dawatch",
        description="Push new deviations by the artists you watch to your phone.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # No --date on any command: the watch feed is an offset-paged stream with
    # no date parameter, so the flag could only ever be a lie.
    run = subparsers.add_parser("run", help="Poll once and notify what is new")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be sent without sending or writing",
    )
    run.add_argument(
        "--no-seed",
        action="store_true",
        help="Notify everything on an empty store instead of seeding silently",
    )

    subparsers.add_parser("seed", help="Record the current feed as seen without notifying")

    subparsers.add_parser("doctor", help="Check configuration, token, store and gateway")

    subparsers.add_parser(
        "login", help="Authorize dawatch against your account and print a refresh token"
    )

    return parser


def _build_metrics(settings: Settings) -> MetricsSink:
    if settings.pushgateway_url:
        return PushgatewaySink(settings.pushgateway_url)
    return NullSink()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        settings = Settings.load()
    except ConfigError as exc:
        # Logging is not configured yet, so this goes straight to stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(settings)
    structlog.contextvars.bind_contextvars(run_id=str(uuid.uuid4()), command=args.command)

    try:
        with ExitStack() as stack:
            http = stack.enter_context(httpx.Client(timeout=settings.http_timeout))

            if args.command == "login":
                # Deliberately before the store is opened: consent needs no
                # database, and this runs on a workstation, not in the cluster.
                return _login(http, settings)

            store = stack.enter_context(SqliteStore(settings.db_path))

            auth: TokenProvider = RefreshTokenAuth(
                http,
                settings.client_id.get_secret_value(),
                settings.client_secret.get_secret_value(),
                store,
                seed_refresh_token=(
                    settings.refresh_token.get_secret_value()
                    if settings.refresh_token is not None
                    else None
                ),
            )

            if args.command == "doctor":
                return _doctor(settings, http, auth)

            client = DeviantArtClient(http, auth, max_retries=settings.max_retries)
            source = WatchedDeviationsSource(client, seen=store.has_seen)

            dry_run = getattr(args, "dry_run", False)
            notifier: Notifier = (
                ConsoleNotifier()
                if dry_run
                else NtfyNotifier(http, settings.ntfy_url, settings.ntfy_topic)
            )

            service = WatchService(source, store, notifier, _build_metrics(settings))

            if args.command == "seed":
                deviations = source.fetch()
                for deviation in deviations:
                    store.mark_seen(deviation, notified=False)
                log.info("seed.complete", count=len(deviations))
                return EXIT_OK

            result = service.run(
                date=None,
                dry_run=dry_run,
                allow_seed=not args.no_seed,
            )
            return result.exit_code
    except ConfigError as exc:
        # Never succeeds on retry: a missing or expired refresh token. Exit 2
        # tells the CronJob's backoffLimit not to bother trying again.
        log.error("run.config_failed", error=str(exc), error_type=type(exc).__name__)
        return EXIT_CONFIG
    except DawatchError as exc:
        log.error("run.failed", error=str(exc), error_type=type(exc).__name__)
        return EXIT_FAILURE


def _login(http: httpx.Client, settings: Settings) -> int:
    """Mint a refresh token interactively and print it for the operator."""
    refresh_token = run_consent_flow(
        http,
        settings.client_id.get_secret_value(),
        settings.client_secret.get_secret_value(),
    )
    print("\nRefresh token (store this as DAWATCH_REFRESH_TOKEN):")
    print(refresh_token)
    print("\nIt expires in 3 months. Run 'dawatch login' again when it does.")
    return EXIT_OK


def _doctor(settings: Settings, http: httpx.Client, auth: TokenProvider) -> int:
    """Check every external dependency and report each one.

    Exists so an operator can answer 'is this deployment healthy' with one
    command instead of waiting to notice missing notifications.
    """
    failures = 0

    def report(name: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        status = "OK  " if ok else "FAIL"
        print(f"[{status}] {name}{f': {detail}' if detail else ''}")
        if not ok:
            failures += 1

    report("config", True, f"db={settings.db_path} topic={settings.ntfy_topic}")

    try:
        token = auth.token()
        report("token", True)
    except DawatchError as exc:
        report("token", False, str(exc))
        return EXIT_FAILURE

    try:
        # The real endpoint, not /placebo: this is the only check that proves
        # the granted scope covers the watch feed. Otherwise a wrong scope
        # stays invisible until the next scheduled run finds an empty feed.
        response = http.get(
            f"{API_BASE}/{WatchedDeviationsSource.PATH}",
            params={"limit": "1"},
            headers={"Authorization": f"Bearer {token}", "dA-minor-version": API_VERSION},
        )
        if response.status_code == httpx.codes.FORBIDDEN:
            report("api", False, "token lacks the scope for the watch feed; re-run 'dawatch login'")
        else:
            report("api", response.status_code == httpx.codes.OK, f"HTTP {response.status_code}")
    except httpx.HTTPError as exc:
        report("api", False, type(exc).__name__)

    if settings.pushgateway_url:
        try:
            response = http.get(settings.pushgateway_url, timeout=3.0)
            report("pushgateway", response.status_code < 500)
        except httpx.HTTPError as exc:
            report("pushgateway", False, type(exc).__name__)
    else:
        report("pushgateway", True, "disabled")

    return EXIT_OK if failures == 0 else EXIT_FAILURE
