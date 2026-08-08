from datetime import datetime  # noqa: TC003 - required at runtime by pydantic

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NotificationsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DAIV_", env_parse_none_str="None")

    NOTIFY_NOT_BEFORE: datetime | None = Field(
        default=None,
        description=(
            "One-time coverage-widening cutoff. When universal classification is first deployed, set this "
            "to the deploy timestamp (ISO-8601) so runs that finished earlier never retro-blast notifications. "
            "None disables the cutoff. Pair with a value carrying a timezone offset (naive values are treated as UTC)."
        ),
    )


settings = NotificationsSettings()
