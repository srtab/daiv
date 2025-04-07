from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class CodebaseSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_")

    CLIENT: Literal["gitlab"] = Field(default="gitlab", description="Client to use for codebase operations")

    # GitLab
    GITLAB_URL: HttpUrl | None = Field(default=None, description="URL of the GitLab instance")
    GITLAB_AUTH_TOKEN: str | None = Field(default=None, description="Authentication token for GitLab")

    # Embeddings
    EMBEDDINGS_MODEL_NAME: str = Field(
        default="text-embedding-3-large",
        description="Name of the embeddings model to use. OpenAI or HuggingFace models are supported.",
    )
    EMBEDDINGS_DIMENSIONS: int = Field(
        default=1536,
        description="Number of dimensions of the embeddings. The default is the max supported by pgvector.",
    )
    EMBEDDINGS_CHUNK_SIZE: int = Field(default=1000, description="Chunk size for the embeddings.")


settings = CodebaseSettings()  # type: ignore
