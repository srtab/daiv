from textwrap import dedent
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CodebaseSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_")

    CLIENT: Literal["gitlab"] = Field(default="gitlab", description="Client to use for codebase operations")

    # GitLab
    GITLAB_URL: HttpUrl | None = Field(default=None, description="URL of the GitLab instance")
    GITLAB_AUTH_TOKEN: SecretStr | None = Field(default=None, description="Authentication token for GitLab")
    GITLAB_WEBHOOK_SECRET: SecretStr | None = Field(
        default=None, description="Secret token for GitLab webhook validation"
    )
    GITHUB_WEBHOOK_SECRET: SecretStr | None = Field(
        default=None, description="Secret token for GitHub webhook validation"
    )

    # Embeddings
    EMBEDDINGS_API_KEY: SecretStr | None = Field(default=None, description="API key for the embeddings provider")
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
        default=250, description="Batch size for the embeddings. Only used for OpenAI models."
    )
    EMBEDDINGS_MAX_INPUT_TOKENS: int = Field(
        default=8192,
        description=(
            "Maximum number of tokens to embed. "
            "A warning will be raised if the number of tokens is higher than this value and the chunk will be skipped."
        ),
    )

    # Chunking
    CHUNK_SIZE: int = Field(
        default=1500,
        description=(
            "Size of the chunks to split the documents into. "
            "This value should not be higher than the max context length of the used embeddings model."
        ),
    )
    CHUNK_OVERLAP: int = Field(default=150, description="Overlap between chunks. Only used for non code languages.")

    # Cleanup
    CLEANUP_CHECK_ACCESSIBILITY: bool = Field(
        default=True,
        description="Check repository accessibility and remove indexes for inaccessible repositories during cleanup",
    )
    CLEANUP_OLD_BRANCHES: bool = Field(
        default=True, description="Clean up indexes from non-default branches older than the threshold during cleanup"
    )
    CLEANUP_OLD_BRANCH_AGE_DAYS: int = Field(
        default=30, description="Age threshold in days for non-default branch indexes to be considered for cleanup"
    )


settings = CodebaseSettings()  # type: ignore
