from __future__ import annotations

import fnmatch
import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast

import litellm
from langchain_community.document_loaders.base import BaseLoader
from langchain_community.document_loaders.blob_loaders import FileSystemBlobLoader as LangFileSystemBlobLoader
from langchain_community.document_loaders.parsers.language import LanguageParser
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from langchain_core.documents import Document
    from langchain_text_splitters import TextSplitter

logger = logging.getLogger(__name__)

EXCLUDE_PATTERN = (
    # files
    "*pipfile.lock",
    "*package-lock.json",
    "*yarn.lock",
    "*gemfile.lock",
    "*composer.lock",
    "*.svg",
    # folders
    "*vendor/*",
    "*.git/*",
)
EXTRA_LANGUAGE_EXTENSIONS = {"html": Language.HTML, "md": Language.MARKDOWN}


class FileSystemBlobLoader(LangFileSystemBlobLoader):
    """
    A filesystem blob loader that allows limiting the files to a specific set of paths.
    """

    def __init__(self, limit_to: list[str] | None = None, **kwargs):
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
            if self.exclude and any(fnmatch.fnmatch(str(path).lower(), glob) for glob in self.exclude):
                continue
            if self.limit_to and not any(fnmatch.fnmatch(str(path).lower(), glob) for glob in self.limit_to):
                continue
            if path.is_file():
                if self.suffixes and path.suffix not in self.suffixes:
                    continue
                yield path


class GenericLanguageLoader(BaseLoader):
    """
    A generic document loader that loads documents from a filesystem and splits them into chunks.
    """

    chunk_size = 1024

    def __init__(
        self,
        blob_loader: FileSystemBlobLoader,
        blob_parser: LanguageParser,
        documents_metadata: dict[str, str] | None = None,
        tokenizer_model: str | None = None,
    ):
        self.blob_loader = blob_loader
        self.blob_parser = blob_parser
        self.documents_metadata = documents_metadata or {}
        self.tokenizer_model = tokenizer_model

    def lazy_load(self) -> Iterator[Document]:
        """
        Load documents lazily. Use this when working at a large scale.
        """
        for blob in self.blob_loader.yield_blobs():
            with suppress(UnicodeDecodeError):
                yield from self.blob_parser.lazy_parse(blob)

    def load_and_split(self, text_splitter: TextSplitter | None = None) -> list[Document]:
        """
        Load all documents and split them into chunks.
        """
        documents_by_language = self._load()
        return self._split(documents_by_language)

    def _load(self):
        """
        Load documents from the blob loader and group them by language.
        """
        documents_by_language: dict[str | None, list[Document]] = {}
        for document in self.lazy_load():
            source_path = Path(document.metadata["source"]).relative_to(self.blob_loader.path)
            document.metadata.update(self.documents_metadata)
            document.metadata["source"] = source_path.as_posix()
            language = document.metadata.get("language")
            if language is None:
                language = EXTRA_LANGUAGE_EXTENSIONS.get(source_path.suffix[1:])
            if language not in documents_by_language:
                documents_by_language[language] = []
            documents_by_language[language].append(document)
        return documents_by_language

    def _split(self, document: dict[str | None, list[Document]]) -> list[Document]:
        """
        Split documents into chunks.
        """
        splitted_documents = []
        for language, documents in document.items():
            logger.info(
                "Splitting %d %s documents from repo %s",
                len(documents),
                language or "Text",
                self.documents_metadata["repo_id"],
            )
            text_splitter = self._get_text_splitter(language)
            splitted_documents.extend(text_splitter.split_documents(documents))
        return splitted_documents

    def _get_text_splitter(self, language: str | None = None) -> RecursiveCharacterTextSplitter:
        """
        Get the text splitter for a given language.
        """

        def _tokenizer_length(text: str) -> int:
            assert self.tokenizer_model is not None, "tokenizer_model must be set set."
            return litellm.token_counter(model=self.tokenizer_model, text=text)

        kwargs = {"length_function": _tokenizer_length, "chunk_size": self.chunk_size}

        if language is None:
            return RecursiveCharacterTextSplitter(**kwargs)
        language = cast(Language, language)
        return RecursiveCharacterTextSplitter.from_language(language=language, **kwargs)

    @classmethod
    def from_filesystem(
        cls,
        path: str | Path,
        *,
        glob: str = "**/[!.]*",
        limit_to: list[str] | None = None,
        exclude: list[str] | None = None,
        suffixes: list[str] | None = None,
        documents_metadata: dict[str, str] | None = None,
        **kwargs,
    ) -> GenericLanguageLoader:
        """
        Create a generic document loader using a filesystem blob loader.
        """
        exclude = exclude or []
        blob_loader = FileSystemBlobLoader(
            path=path, glob=glob, limit_to=limit_to, exclude=exclude + list(EXCLUDE_PATTERN), suffixes=suffixes
        )
        return cls(blob_loader, LanguageParser(), documents_metadata, **kwargs)
