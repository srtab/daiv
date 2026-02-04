from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_", env_parse_none_str="None")

    EXTERNAL_URL: HttpUrl = Field(default=HttpUrl("https://app:8000"), description="URL of the DAIV webapp")

    SANDBOX_URL: HttpUrl = Field(default=HttpUrl("http://sandbox:8000"), description="URL of the sandbox service")
    SANDBOX_TIMEOUT: float = Field(default=600, description="Timeout for sandbox requests in seconds")
    SANDBOX_API_KEY: SecretStr | None = Field(default=None, description="API key for sandbox requests")
    SANDBOX_BASE_IMAGE: str | None = Field(
        default="python:3.12-alpine",
        description=(
            "Default base image for sandbox sessions. "
            "If set to None, sandbox is disabled unless a repository `.daiv.yml` overrides it."
        ),
    )
    SANDBOX_EPHEMERAL: bool = Field(
        default=False, description="Whether sandbox sessions should be ephemeral by default."
    )
    SANDBOX_NETWORK_ENABLED: bool = Field(
        default=False, description="Whether to enable the network in sandbox sessions by default."
    )
    SANDBOX_CPU: float | None = Field(default=None, description="CPUs to allocate to sandbox sessions by default.")
    SANDBOX_MEMORY: int | None = Field(
        default=None, description="Memory limit (bytes) to allocate to sandbox sessions by default."
    )


settings = CoreSettings()
