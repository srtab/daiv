from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebhooksSettings(BaseSettings):
    # Keep the CODEBASE_ prefix: deployments set CODEBASE_GITLAB_WEBHOOK_SECRET and the rest.
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_", env_parse_none_str="None")

    GITLAB_WEBHOOK_SECRET: SecretStr | None = Field(
        default=None, description="Secret token for GitLab webhook validation"
    )
    GITHUB_WEBHOOK_SECRET: SecretStr | None = Field(
        default=None, description="Secret token for GitHub webhook validation"
    )
    WEBHOOK_SETUP_CRON: str = Field(
        default="*/5 * * * *", description="Cron expression for periodic webhook setup (GitLab only)"
    )


settings = WebhooksSettings()
