from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="AUTOMATION_", env_parse_none_str="None")

    # Web fetch settings (env-only)
    WEB_FETCH_PROXY_URL: str | None = Field(default=None, description="Optional proxy URL for web_fetch HTTP requests.")
    WEB_FETCH_AUTH_HEADERS: dict[str, dict[str, SecretStr]] = Field(
        default_factory=dict,
        description=(
            "Domain-to-headers mapping for web_fetch authentication. "
            "Keys are domain names (exact match only, e.g. 'context7.com' matches only 'context7.com' "
            "and not 'api.context7.com'), values are dicts of header name to header value. "
            'Example: \'{"context7.com": {"X-API-Key": "sk-abc"}}\''
        ),
    )


settings = AutomationSettings()
