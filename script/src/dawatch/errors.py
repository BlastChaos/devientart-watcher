"""Exception hierarchy. Every failure the application raises lands here."""


class DawatchError(Exception):
    """Base for every error this application raises deliberately."""


class ConfigError(DawatchError):
    """Configuration is missing or invalid. Retrying will not help."""


class AuthError(DawatchError):
    """The DeviantArt token endpoint refused our credentials."""


class FetchError(DawatchError):
    """Fetching the feed failed after exhausting retries."""


class NotifyError(DawatchError):
    """Delivering one notification failed."""


class StoreError(DawatchError):
    """The seen-store is unreadable or unwritable."""
