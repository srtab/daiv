import functools

from daiv.settings.components import DATA_DIR
from langchain_core.embeddings import Embeddings
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import OpenAIEmbeddings
from langchain_voyageai.embeddings import DEFAULT_VOYAGE_3_BATCH_SIZE, VoyageAIEmbeddings

from codebase.conf import settings


@functools.cache
def embeddings_function() -> Embeddings:
    """
    Creates and returns a cached embeddings function.

    Returns:
        Embeddings: Configured embeddings model with optimized chunk size.
    """
    provider, model_name = settings.EMBEDDINGS_MODEL_NAME.split("/", 1)

    common_kwargs = {}
    if settings.EMBEDDINGS_API_KEY:
        common_kwargs["api_key"] = settings.EMBEDDINGS_API_KEY.get_secret_value()

    if provider == "openai":
        return OpenAIEmbeddings(
            model=model_name,
            dimensions=settings.EMBEDDINGS_DIMENSIONS,
            chunk_size=settings.EMBEDDINGS_BATCH_SIZE,
            **common_kwargs,
        )
    elif provider == "huggingface":
        return HuggingFaceEmbeddings(model_name=model_name, cache_folder=str(DATA_DIR / "embeddings"))
    elif provider == "voyageai":
        return VoyageAIEmbeddings(
            model=model_name,
            output_dimension=settings.EMBEDDINGS_DIMENSIONS if settings.EMBEDDINGS_DIMENSIONS != 1536 else 1024,
            batch_size=DEFAULT_VOYAGE_3_BATCH_SIZE,
            **common_kwargs,
        )
    else:
        raise ValueError(f"Unsupported embeddings provider: {provider}")
