"""Application configuration. The only place environment variables are read."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from dawatch.errors import ConfigError


class Settings(BaseSettings):
    """Runtime configuration, populated from the environment.

    Credentials use the ``DEVIANTART_`` prefix because they are issued by
    DeviantArt; everything else uses ``DAWATCH_`` because it is ours.
    """

    model_config = SettingsConfigDict(
        env_prefix="DAWATCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    client_id: SecretStr = Field(validation_alias="DEVIANTART_CLIENT_ID")
    client_secret: SecretStr = Field(validation_alias="DEVIANTART_CLIENT_SECRET")

    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str

    db_path: Path = Path("/data/dawatch.db")

    env: Literal["dev", "prod"] = "prod"
    log_level: str = "INFO"

    pushgateway_url: str | None = None

    http_timeout: float = 10.0
    max_retries: int = 3
    notify_mature: bool = False

    @classmethod
    def load(cls) -> Self:
        """Build settings from the environment.

        Raises:
            ConfigError: if any required value is missing or malformed. The
                message names the offending variables so an operator can fix
                the deployment without reading source.
        """
        try:
            return cls()  # type: ignore[call-arg]
        except ValidationError as exc:
            names = [cls._env_name_for(error["loc"]) for error in exc.errors()]
            raise ConfigError(
                f"Invalid configuration. Check these environment variables: {', '.join(names)}"
            ) from exc

    @classmethod
    def _env_name_for(cls, loc: tuple[int | str, ...]) -> str:
        """Map a pydantic error location back to its environment variable name.

        For a field with a ``validation_alias`` (e.g. ``client_id``), pydantic
        reports the alias itself in ``loc`` (e.g. ``"DEVIANTART_CLIENT_ID"``),
        not the field name. Such a string is not a key in ``model_fields``, so
        when the lookup misses, ``loc[0]`` already IS the environment variable
        name and is returned verbatim.
        """
        if not loc:
            return "<unknown>"
        field_name = str(loc[0])
        field = cls.model_fields.get(field_name)
        if field is None:
            return field_name
        alias = getattr(field, "validation_alias", None)
        if isinstance(alias, str):
            return alias
        return f"DAWATCH_{field_name.upper()}"
