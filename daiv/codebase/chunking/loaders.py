from __future__ import annotations

import fnmatch
import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from langchain_community.document_loaders.base import BaseLoader
from langchain_community.document_loaders.blob_loaders import FileSystemBlobLoader as LangFileSystemBlobLoader
from langchain_community.document_loaders.parsers.language import LanguageParser
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
    RecursiveJsonSplitter,
    TextSplitter,
)

from codebase.conf import settings

from .languages import filename_to_lang
from .splitters import ChonkieTextSplitter
from .utils import split_documents

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from langchain_core.documents import Document


logger = logging.getLogger("daiv.indexes")


class FileSystemBlobLoader(LangFileSystemBlobLoader):
    """
    A filesystem blob loader that allows limiting the files to a specific set of paths.
    """

    def __init__(self, limit_to: Iterable[str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.limit_to = limit_to or []

    def _yield_paths(self) -> Iterable[Path]:
        """
        Rewrite the yield paths method to allow limit files to a specific set of paths and check exclude
        with case insensitive.
        """
        if self.path.is_file():
            yield self.path
            return

        paths = self.path.glob(self.glob)
        for path in paths:
            if self.exclude and any(fnmatch.fnmatch(str(path), pattern) for pattern in self.exclude):
                continue
            if self.limit_to and not any(path.match(pattern, case_sensitive=False) for pattern in self.limit_to):
                continue
            if path.is_file():
                if self.suffixes and path.suffix not in self.suffixes:
                    continue
                yield path


class GenericLanguageLoader(BaseLoader):
    """
    A generic document loader that loads documents from a filesystem and splits them into chunks.
    """

    def __init__(self, blob_loader: FileSystemBlobLoader, documents_metadata: dict[str, str | None] | None = None):
        self.blob_loader = blob_loader
        self.blob_parser = LanguageParser()
        self.documents_metadata = documents_metadata or {}

    def lazy_load(self) -> Iterator[Document]:
        """
        Load documents lazily. Use this when working at a large scale.
        """
        for blob in self.blob_loader.yield_blobs():
            with suppress(UnicodeDecodeError):
                for doc in self.blob_parser.lazy_parse(blob):
                    # avoid documents without content
                    if doc.page_content:
                        relative_path = Path(cast("str", blob.source)).relative_to(self.blob_loader.path)
                        doc.metadata.update(self.documents_metadata)
                        doc.metadata["source"] = relative_path.as_posix()
                        doc.metadata["language"] = filename_to_lang(relative_path) or "text"
                        yield doc

    def load_and_split(self, text_splitter: TextSplitter | None = None) -> list[Document]:
        """
        Load all documents and split them into chunks.
        """
        documents_to_split: dict[str | None, list[Document]] = {}

        for document in self.lazy_load():
            language = document.metadata["language"]

            if language not in documents_to_split:
                documents_to_split[language] = []

            documents_to_split[language].append(document)

        return self._split(documents_to_split)

    def _split(self, document: dict[str | None, list[Document]]) -> list[Document]:
        """
        Split each document into smaller chunks.
        """
        splitted_documents: list[Document] = []

        for language, documents in document.items():
            logger.info(
                "Splitting %d %s documents from repo %s[%s]",
                len(documents),
                language,
                self.documents_metadata.get("repo_id", "unknown"),
                self.documents_metadata.get("ref", "unknown"),
            )

            text_splitter = self._get_text_splitter(language)

            for doc in split_documents(text_splitter, documents):
                doc.id = str(uuid4().__str__())
                splitted_documents.append(doc)

        return splitted_documents

    def _get_text_splitter(
        self, language: str | None = None
    ) -> TextSplitter | MarkdownHeaderTextSplitter | RecursiveJsonSplitter:
        """
        Get the text splitter for a given language.
        """
        if language == "markdown":
            logger.debug("Using markdown header splitter")
            return MarkdownHeaderTextSplitter(
                headers_to_split_on=[("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")], strip_headers=False
            )
        elif language == "json":
            logger.debug("Using json splitter")
            return RecursiveJsonSplitter()
        elif language:
            logger.debug("Using chonkie splitter")
            return ChonkieTextSplitter(language=language, chunk_size=settings.CHUNK_SIZE)
        logger.debug("Using default splitter")
        return RecursiveCharacterTextSplitter(chunk_size=settings.CHUNK_SIZE, chunk_overlap=settings.CHUNK_OVERLAP)

    @classmethod
    def from_filesystem(
        cls,
        path: str | Path,
        *,
        glob: str = "**/*",
        limit_to: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
        suffixes: Iterable[str] | None = None,
        documents_metadata: dict[str, str | None] | None = None,
        **kwargs,
    ) -> GenericLanguageLoader:
        """
        Create a generic document loader using a filesystem blob loader.
        """
        blob_loader = FileSystemBlobLoader(
            path=path, glob=glob, limit_to=limit_to, exclude=exclude or [], suffixes=suffixes
        )
        return cls(blob_loader, documents_metadata, **kwargs)
