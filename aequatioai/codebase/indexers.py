import logging
from pathlib import Path
from typing import Literal

import chromadb
import chromadb.config
from langchain_chroma import Chroma
from langchain_community.document_loaders.blob_loaders import Blob
from langchain_community.document_loaders.generic import GenericLoader
from langchain_community.document_loaders.parsers.language import LanguageParser
from langchain_community.embeddings.sentence_transformer import SentenceTransformerEmbeddings
from langchain_core.documents import Document
from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

from codebase.models import RepositoryFile

from .clients import GitHubClient, GitLabClient
from .conf import settings

logger = logging.getLogger(__name__)

EXTRA_LANGUAGE_EXTENSIONS = {"html": Language.HTML, "md": Language.MARKDOWN}


class CodebaseIndex:
    """
    Index a codebase into Chroma.
    """

    repo_client: GitLabClient | GitHubClient
    chroma_client: chromadb.HttpClient
    embedding_function: SentenceTransformerEmbeddings

    chunk_size = 512
    chunk_overlap = 128

    def __init__(self, repo_client: GitLabClient | GitHubClient, embedding_model: str = "all-MiniLM-L6-v2"):
        self.chroma_client = chromadb.HttpClient(
            host=settings.CODEBASE_CHROMA_HOST,
            port=settings.CODEBASE_CHROMA_PORT,
            settings=chromadb.config.Settings(anonymized_telemetry=False),
        )
        self.repo_client = repo_client
        self.embedding_function = SentenceTransformerEmbeddings(model_name=embedding_model)
        self.db = Chroma(
            client=self.chroma_client,
            collection_name=settings.CODEBASE_COLLECTION_NAME,
            collection_metadata={"hnsw:space": "cosine"},
            embedding_function=self.embedding_function,
        )

    def update(self, repo_id: str):
        """
        Index a codebase into Chroma.
        """
        # TODO: only update the new commits since the last update
        documents = self.extract_chunks(repo_id, sha="dev")
        logger.info("Indexing %d chunks from repo %s", len(documents), repo_id)
        Chroma.from_documents(
            embedding=self.embedding_function,
            client=self.chroma_client,
            collection_name=settings.CODEBASE_COLLECTION_NAME,
            collection_metadata={"hnsw:space": "cosine"},
            documents=documents,
        )

    def extract_chunks(self, repo_id: str, sha: str) -> list[Document]:
        """
        Extract code chunks from a repository.
        """
        repo_dir, tmp_dir = self.repo_client.load_repo(repo_id, sha=sha)

        loader = GenericLoader.from_filesystem(
            repo_dir.as_posix(),
            glob="**/*",
            exclude=[
                "**/*.pdf",
                "**/*.docx",
                "**/*.doc",
                "**/*.jpg",
                "**/*.jpeg",
                "**/*.png",
                "**/*.gif",
                "**/*.svg",
                "**/*.ico",
                "**/*.webp",
                "**/*.bmp",
                "**/*.mp4",
                "**/*.mp3",
                "**/*.wav",
                "**/*.woff",
                "**/*.woff2",
                "**/*.ttf",
                "**/*.eot",
                "**/*.otf",
                "**/*.flv",
                "**/*.avi",
                "**/*.mov",
                "**/*.wmv",
                "**/*.webm",
                "**/*.mkv",
                "**/*.m4v",
                "**/*.flac",
                "**/*.zip",
                "**/*.tar",
                "**/*.gz",
                "**/*.xz",
                "**/*.7z",
                "**/*.rar",
                "**/*.tar.gz",
                "**/*.tar.xz",
                "**/*.tar.bz2",
                "**/*.tar.zst",
                "**/*.tar.7z",
                "**/*.tar.rar",
                "**/*.tar.zip",
            ],
            parser=LanguageParser(),
        )

        documents_by_language: dict[str | None, list[Document]] = {}
        for document in loader.lazy_load():
            source_path = Path(document.metadata["source"]).relative_to(repo_dir)
            document.metadata["repo_id"] = repo_id
            document.metadata["source"] = source_path.as_posix()
            language = document.metadata.get("language")
            if language is None:
                language = EXTRA_LANGUAGE_EXTENSIONS.get(source_path.suffix[1:])
            if language not in documents_by_language:
                documents_by_language[language] = []
            documents_by_language[language].append(document)

        tmp_dir.cleanup()

        splitted_documents = []
        for language, documents in documents_by_language.items():
            if language is None:
                logger.info("Splitting %d documents from repo %s", len(documents), repo_id)
                text_splitter = RecursiveCharacterTextSplitter(
                    chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
                )
            else:
                logger.info("Splitting %d %s documents from repo %s", len(documents), language, repo_id)
                text_splitter = RecursiveCharacterTextSplitter.from_language(
                    language=language, chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap
                )
            splitted_documents.extend(text_splitter.split_documents(documents))

        return splitted_documents

    def query(
        self,
        query: str,
        repo_id: str | None = None,
        content_type: Literal["functions_classes", "simplified_code"] | None = None,
    ) -> Document | None:
        """
        Query the codebase.
        """
        conditions: list[dict[str, str]] = []
        if repo_id:
            conditions.append({"repo_id": repo_id})
        if content_type:
            conditions.append({"content_type": content_type})

        chroma_filter: dict[str, str | list] | None = None
        if len(conditions) > 1:
            chroma_filter = {"$and": conditions}
        elif len(conditions) == 1:
            chroma_filter = conditions[0]

        results = self.db.similarity_search_with_relevance_scores(query, k=1, score_threshold=0.6, filter=chroma_filter)

        if not results:
            return None

        return results[0][0]

    def search_most_similar_filepath(self, repo_id: str, repository_file: RepositoryFile) -> str | None:
        """
        Search the most similar file path in the codebase.
        """
        documents = list(
            LanguageParser().lazy_parse(
                Blob.from_data(repository_file.content, metadata={"source": repository_file.file_path})
            )
        )

        chunk_to_search = None
        if len(documents) == 1:
            chunk_to_search = documents[0].page_content
        else:
            for document in documents:
                if "language" in document.metadata and document.metadata["content_type"] == "simplified_code":
                    chunk_to_search = document.page_content
                    break

        if not chunk_to_search:
            return None

        result = self.query(chunk_to_search, repo_id=repo_id)

        if not result:
            # Fallback to try to find the file by the file path
            get_result = self.db.get(
                include=["metadatas"],
                where={"$and": [{"repo_id": repo_id}, {"source": repository_file.file_path}]},
                limit=1,
            )

            if get_result["metadatas"]:
                return get_result["metadatas"][0]["source"]
            return None

        return result.metadata["source"]

    def reset(self, repo_id: str):
        """
        Reset the index of a repository.
        """
        results = self.db.get(where={"repo_id": repo_id})
        for document_id in results["ids"]:
            self.db.delete(document_id)
