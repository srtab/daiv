from typing import Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .base import GitPlatform


class CodebaseSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_", env_parse_none_str="None")

    CLIENT: Literal[GitPlatform.GITLAB, GitPlatform.GITHUB] = Field(
        default=GitPlatform.GITLAB, description="Git platform to use for codebase operations"
    )

    # GitLab
    GITLAB_URL: HttpUrl | None = Field(default=None, description="URL of the GitLab instance")
    GITLAB_AUTH_TOKEN: SecretStr | None = Field(default=None, description="Authentication token for GitLab")
    GITLAB_PAT_FALLBACK: bool = Field(
        default=True,
        description="Use GITLAB_AUTH_TOKEN for git clone/push when a per-project clone token can't be created",
    )
    GITLAB_ID_TOKEN_ISSUER: str | None = Field(
        default=None,
        description=(
            "Issuer of GitLab CI ID tokens, which CI jobs present to the jobs API: GitLab's external URL. Defaults to "
            "GITLAB_URL; set it when DAIV reaches GitLab at another address"
        ),
    )

    REPO_ACCESS_SYNC_CRON: str = Field(
        default="*/15 * * * *", description="Cron expression for the periodic repository access sync"
    )
    REPO_ACCESS_HARD_TTL_HOURS: int = Field(
        default=24,
        description="Deny non-admin access to a repository when its synced access data is older than this many hours",
    )
    LFS_MAX_CLONE_BYTES: int = Field(
        default=256 * 1024 * 1024,
        ge=0,
        description=(
            "The most Git LFS content one clone downloads, summed over the repository and its submodules; 0 downloads "
            "none"
        ),
    )

    # GitHub
    GITHUB_URL: HttpUrl | None = Field(default=None, description="URL of the GitHub instance")
    GITHUB_APP_ID: int | None = Field(default=None, description="GitHub app ID")
    GITHUB_INSTALLATION_ID: int | None = Field(default=None, description="GitHub installation ID")
    GITHUB_PRIVATE_KEY: SecretStr | None = Field(default=None, description="GitHub private key")
    GITHUB_ACTIONS_OIDC_ISSUER: str | None = Field(
        default=None,
        description=(
            "Issuer of GitHub Actions OIDC tokens, which CI jobs present to the jobs API. Defaults to "
            "https://token.actions.SUBDOMAIN.ghe.com when GITHUB_URL names a GHE.com enterprise SUBDOMAIN, "
            "https://HOST/_services/token when it names a GitHub Enterprise Server HOST, else "
            "https://token.actions.githubusercontent.com"
        ),
    )


settings = CodebaseSettings()
