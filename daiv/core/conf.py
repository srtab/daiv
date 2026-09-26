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
            "Global list of additional bash command rules to block before sandbox execution. "
            "Each entry is a command name plus arguments that must follow it in order, not necessarily "
            "adjacent, e.g. 'rm -rf'. "
            "Built-in safety rules always apply and cannot be removed via this setting."
        ),
    )
    SANDBOX_COMMAND_POLICY_ALLOW: tuple[str, ...] = Field(
        default=(),
        description=(
            "Global list of bash command rules to permit. Has no effect: built-in rules and "
            "SANDBOX_COMMAND_POLICY_DISALLOW take precedence, and every other command is already allowed."
        ),
    )


settings = CoreSettings()
