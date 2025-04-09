from unittest.mock import patch

import pytest
from langchain_voyageai.embeddings import DEFAULT_VOYAGE_3_BATCH_SIZE
from pydantic import SecretStr

from codebase.search_engines.semantic import embeddings_function
from daiv.settings.components import DATA_DIR


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the embeddings function cache before each test."""
    embeddings_function.cache_clear()


@pytest.fixture
def mock_settings():
    """Fixture to mock the settings for testing."""
    with patch("codebase.search_engines.semantic.settings") as mock:
        yield mock


def test_openai_embeddings(mock_settings):
    """Test OpenAI embeddings configuration."""
    mock_settings.EMBEDDINGS_MODEL_NAME = "openai/text-embedding-3-large"
    mock_settings.EMBEDDINGS_DIMENSIONS = 1536
    mock_settings.EMBEDDINGS_BATCH_SIZE = 500
    mock_settings.EMBEDDINGS_API_KEY = "test-api-key"

    with patch("codebase.search_engines.semantic.OpenAIEmbeddings") as mock_embeddings:
        result = embeddings_function()
        mock_embeddings.assert_called_once_with(
            model="text-embedding-3-large", dimensions=1536, chunk_size=500, api_key=SecretStr("test-api-key")
        )
        assert result == mock_embeddings.return_value


def test_huggingface_embeddings(mock_settings):
    """Test HuggingFace embeddings configuration."""
    mock_settings.EMBEDDINGS_MODEL_NAME = "huggingface/Alibaba-NLP/gte-modernbert-base"
    mock_settings.EMBEDDINGS_API_KEY = None

    with patch("codebase.search_engines.semantic.HuggingFaceEmbeddings") as mock_embeddings:
        result = embeddings_function()
        mock_embeddings.assert_called_once_with(
            model_name="Alibaba-NLP/gte-modernbert-base", cache_folder=str(DATA_DIR / "embeddings")
        )
        assert result == mock_embeddings.return_value


def test_voyageai_embeddings(mock_settings):
    """Test VoyageAI embeddings configuration."""
    mock_settings.EMBEDDINGS_MODEL_NAME = "voyageai/voyage-code-3"
    mock_settings.EMBEDDINGS_DIMENSIONS = 1536
    mock_settings.EMBEDDINGS_API_KEY = "test-api-key"

    with patch("codebase.search_engines.semantic.VoyageAIEmbeddings") as mock_embeddings:
        result = embeddings_function()
        mock_embeddings.assert_called_once_with(
            model="voyage-code-3",
            output_dimension=1536,
            batch_size=DEFAULT_VOYAGE_3_BATCH_SIZE,
            api_key=SecretStr("test-api-key"),
        )
        assert result == mock_embeddings.return_value


def test_embeddings_caching(mock_settings):
    """Test that the embeddings function is properly cached."""
    mock_settings.EMBEDDINGS_MODEL_NAME = "openai/text-embedding-3-large"
    mock_settings.EMBEDDINGS_DIMENSIONS = 1536
    mock_settings.EMBEDDINGS_BATCH_SIZE = 500
    mock_settings.EMBEDDINGS_API_KEY = None

    with patch("codebase.search_engines.semantic.OpenAIEmbeddings") as mock_embeddings:
        # First call
        result1 = embeddings_function()
        # Second call should return the same instance
        result2 = embeddings_function()

        # OpenAIEmbeddings should only be called once
        mock_embeddings.assert_called_once()
        assert result1 == result2


def test_unsupported_provider(mock_settings):
    """Test error handling for unsupported provider."""
    mock_settings.EMBEDDINGS_MODEL_NAME = "unsupported/test-model"

    with pytest.raises(ValueError, match="Unsupported embeddings provider: unsupported"):
        embeddings_function()
