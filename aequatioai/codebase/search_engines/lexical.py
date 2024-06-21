from __future__ import annotations

from functools import lru_cache
from typing import cast

from django.utils.text import slugify

from langchain_core.documents import Document as LangDocument
from tantivy import Document, Index, SchemaBuilder

from aequatioai.settings.components import DATA_DIR
from codebase.search_engines.base import ScoredResult, SearchEngine

TANTIVY_INDEX_PATH = DATA_DIR / "tantivy_index"


@lru_cache
def tantivy_index(index_name: str, persistent: bool = True) -> Index:
    """
    Create a Tantivy index with the given index name.

    Args:
        index_name (str): The name of the index.
        persistent (bool, optional): Whether to create a persistent index. Defaults to True.

    Returns:
        Index: The Tantivy index.
    """
    schema_builder = SchemaBuilder()
    schema_builder.add_text_field("page_source", stored=True)
    schema_builder.add_text_field("page_content", stored=True)
    schema = schema_builder.build()

    index_path = TANTIVY_INDEX_PATH / slugify(index_name)
    if persistent and not index_path.exists():
        index_path.mkdir(parents=True, exist_ok=True)

    return Index(schema, path=persistent and index_path.as_posix() or None)


class LexicalSearchEngine(SearchEngine):
    """
    Lexical search engine based on Tantivy.
    """

    def __init__(self, persistent: bool = True):
        """
        Initialize the lexical search engine.
        """
        self.persistent = persistent

    def add_documents(self, index_name: str, documents: list[LangDocument]):
        """
        Add documents to the index.

        Args:
            index_name (str): The name of the index.
            documents (list[LangDocument]): The documents to be added.

        Raises:
            AssertionError: When the index name is not provided.
        """
        assert index_name is not None, "Index name is required."
        writer = self._get_index(index_name).writer()
        for document in documents:
            writer.add_document(Document(page_source=document.metadata["source"], page_content=document.page_content))
        writer.commit()

    def delete_documents(self, index_name: str, field_name: str, field_value: str | list[str]):
        """
        Delete documents from the index by a field value.

        Args:
            index_name (str): The name of the index.
            field_name (str): The field name to identify the document.
            field_value (str): The field value.
        """
        if isinstance(field_value, str):
            field_value = [field_value]

        writer = self._get_index(index_name).writer()
        for value in field_value:
            writer.delete_documents(field_name, value)
        writer.commit()

    def delete(self, index_name: str):
        """
        Delete all documents from the index.

        Args:
            index_name (str): The name of the index.
        """
        writer = self._get_index(index_name).writer()
        writer.delete_all_documents()
        writer.commit()

    def search(self, index_name: str, query: str, k: int = 10, **kwargs) -> list[ScoredResult]:
        """
        Search the index and return the top-k scored results.

        Args:
            index_name (str): The name of the index.
            query (str): The query string.
            k (int, optional): The number of results to return. Defaults to 10.
            **kwargs: Additional keyword arguments to pass to tantity search.

        Returns:
            list[ScoredResult]: The scored results.
        """
        index = self._get_index(index_name)
        index.reload()
        searcher = index.searcher()
        parsed_query = index.parse_query(query, ["page_content"])
        results = []
        for score, best_doc_address in searcher.search(parsed_query, k, **kwargs).hits:
            document = searcher.doc(best_doc_address)
            results.append(
                ScoredResult(
                    score=score,
                    document=LangDocument(
                        page_content=cast(str, document.get_first("page_content")),
                        metadata={"source": document.get_first("page_source")},
                    ),
                )
            )
        return results

    def _get_index(self, index_name: str) -> Index:
        """
        Get the index object.

        Args:
            index_name (str): The name of the index.

        Returns:
            Index: The Tantivy index.
        """
        return tantivy_index(index_name, self.persistent)


if __name__ == "__main__":
    search = LexicalSearchEngine(persistent=False)
    search.add_documents(
        "repo1",
        [
            LangDocument(metadata={"source": "source1"}, page_content="Paris is the capital of France."),
            LangDocument(metadata={"source": "source2"}, page_content="Berlin is the capital of Germany."),
            LangDocument(metadata={"source": "source3"}, page_content="Madrid is the capital of Spain."),
        ],
    )
    results = search.search("repo1", "What is the capital of France?")
    print(results)
