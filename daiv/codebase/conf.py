from textwrap import dedent
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
    EMBEDDINGS_API_KEY: str | None = Field(default=None, description="API key for the embeddings provider")
    EMBEDDINGS_MODEL_NAME: str = Field(
        default="openai/text-embedding-3-large",
        description=dedent("""
            Name of the embeddings model to use. OpenAI, HuggingFace or VoyageAI models are supported.
            For more embeddings models, check: https://mteb-leaderboard.hf.space/?benchmark_name=CoIR.
        """),
        examples=[
            # Code specific: https://docs.voyageai.com/docs/embeddings
            "voyageai/voyage-code-3",
            # General purpose: https://huggingface.co/Alibaba-NLP/gte-modernbert-base
            "huggingface/Alibaba-NLP/gte-modernbert-base",
            # General purpose: https://platform.openai.com/docs/guides/embeddings
            "openai/text-embedding-3-large",
        ],
    )
    EMBEDDINGS_DIMENSIONS: int = Field(
        default=1536,
        description=dedent("""
            Number of dimensions of the embeddings. The default is the max supported by pgvector.

            > WARNING: If you change this value after the documents have been already indexed, you will need to recreate the `CodebaseDocument` table on the database and re-index all the documents.
        """),  # noqa: E501
    )
    EMBEDDINGS_BATCH_SIZE: int = Field(
        default=500, description="Batch size for the embeddings. Only used for OpenAI models."
    )


settings = CodebaseSettings()  # type: ignore
