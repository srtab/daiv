from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_", env_parse_none_str="None")

    ENCRYPTION_KEY: SecretStr | None = Field(
        default=None,
        description=(
            "Fernet encryption key for encrypting secrets stored in the database. "
            "If not set, a key is derived from DJANGO_SECRET_KEY via HKDF."
        ),
    )

    EXTERNAL_URL: HttpUrl = Field(default=HttpUrl("https://127.0.0.1:8000"), description="URL of the DAIV webapp")

    SANDBOX_URL: HttpUrl = Field(default=HttpUrl("http://sandbox:8000"), description="URL of the sandbox service")
    SANDBOX_COMMAND_POLICY_DISALLOW: tuple[str, ...] = Field(
        default=(),
        description=(
            "Global list of additional bash command prefixes to block before sandbox execution. "
            "Each entry is a space-separated prefix, e.g. 'rm -rf'. "
            "Built-in safety rules always apply and cannot be removed via this setting."
        ),
    )
    SANDBOX_COMMAND_POLICY_ALLOW: tuple[str, ...] = Field(
        default=(),
        description=(
            "Global list of bash command prefixes that override the default disallow policy. "
            "Repository-level disallow rules and built-in rules still take precedence."
        ),
    )


settings = CoreSettings()
