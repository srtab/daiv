from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveJsonSplitter

if TYPE_CHECKING:
    from collections.abc import Iterable

    from langchain_core.documents import Document
    from langchain_text_splitters import TextSplitter


def split_documents(
    text_splitter: RecursiveJsonSplitter | MarkdownHeaderTextSplitter | TextSplitter, documents: Iterable[Document]
) -> list[Document]:
    """
    Split documents into chunks.
    """
    if hasattr(text_splitter, "split_documents"):
        return text_splitter.split_documents(documents)

    texts = []
    metadatas = []

    for doc in documents:
        texts.append(doc.page_content)
        metadatas.append(doc.metadata)

    if isinstance(text_splitter, RecursiveJsonSplitter):
        return text_splitter.create_documents([json.loads(text) for text in texts], metadatas=metadatas)

    if isinstance(text_splitter, MarkdownHeaderTextSplitter):
        documents = []
        for i, text in enumerate(texts):
            for chunk in text_splitter.split_text(text):
                chunk.metadata.update(metadatas[i])
                documents.append(chunk)
        return documents
