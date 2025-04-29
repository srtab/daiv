from unittest.mock import MagicMock, patch

import pytest
from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, TextSplitter

from codebase.chunking.utils import split_documents


class TestSplitDocuments:
    def test_split_documents_with_splitter_having_split_documents_method(self):
        # Create a mock text splitter with split_documents method
        text_splitter = MagicMock(spec=TextSplitter)
        text_splitter.split_documents.return_value = [
            Document(page_content="Split content 1", metadata={"source": "test1"}),
            Document(page_content="Split content 2", metadata={"source": "test2"}),
        ]

        # Create test documents
        documents = [
            Document(page_content="Original content 1", metadata={"source": "test1"}),
            Document(page_content="Original content 2", metadata={"source": "test2"}),
        ]

        # Call the function
        result = split_documents(text_splitter, documents)

        # Verify the text_splitter's split_documents method was called
        text_splitter.split_documents.assert_called_once_with(documents)

        # Verify the result
        assert len(result) == 2
        assert result[0].page_content == "Split content 1"
        assert result[1].page_content == "Split content 2"

    def test_split_documents_with_markdown_header_text_splitter(self):
        # Create a mock MarkdownHeaderTextSplitter
        markdown_splitter = MagicMock(spec=MarkdownHeaderTextSplitter)
        
        # Configure the mock to return specific chunks for each document
        markdown_splitter.split_text.side_effect = [
            [
                Document(page_content="Chunk 1", metadata={"header": "Header 1"}),
                Document(page_content="Chunk 2", metadata={"header": "Header 2"}),
            ],
            [
                Document(page_content="Chunk 3", metadata={"header": "Header 3"}),
            ],
        ]

        # Create test documents with metadata
        documents = [
            Document(page_content="# Header 1\nContent 1\n## Header 2\nContent 2", 
                    metadata={"source": "doc1", "language": "markdown"}),
            Document(page_content="# Header 3\nContent 3", 
                    metadata={"source": "doc2", "language": "markdown"}),
        ]

        # Call the function
        result = split_documents(markdown_splitter, documents)

        # Verify the markdown_splitter's split_text method was called for each document
        assert markdown_splitter.split_text.call_count == 2
        
        # Verify the result
        assert len(result) == 3
        
        # Check that metadata was properly updated
        assert result[0].metadata == {"header": "Header 1", "source": "doc1", "language": "markdown"}
        assert result[1].metadata == {"header": "Header 2", "source": "doc1", "language": "markdown"}
        assert result[2].metadata == {"header": "Header 3", "source": "doc2", "language": "markdown"}

    def test_split_documents_with_empty_documents(self):
        # Create a mock text splitter
        text_splitter = MagicMock(spec=MarkdownHeaderTextSplitter)
        
        # Call the function with an empty list
        result = split_documents(text_splitter, [])
        
        # Verify the result is an empty list
        assert result == []
        
        # Verify the text_splitter's split_text method was not called
        text_splitter.split_text.assert_not_called()

    def test_split_documents_with_empty_content(self):
        # Create a mock MarkdownHeaderTextSplitter
        markdown_splitter = MagicMock(spec=MarkdownHeaderTextSplitter)
        
        # Configure the mock to return empty chunks
        markdown_splitter.split_text.return_value = []
        
        # Create test documents with empty content
        documents = [
            Document(page_content="", metadata={"source": "empty_doc"}),
        ]
        
        # Call the function
        result = split_documents(markdown_splitter, documents)
        
        # Verify the result is an empty list
        assert result == []
        
        # Verify the markdown_splitter's split_text method was called
        markdown_splitter.split_text.assert_called_once_with("")

    def test_split_documents_with_markdown_splitter_returning_empty_chunks(self):
        # Create a mock MarkdownHeaderTextSplitter
        markdown_splitter = MagicMock(spec=MarkdownHeaderTextSplitter)
        
        # Configure the mock to return empty chunks for non-empty content
        markdown_splitter.split_text.return_value = []
        
        # Create test documents with content
        documents = [
            Document(page_content="Some content", metadata={"source": "doc"}),
        ]
        
        # Call the function
        result = split_documents(markdown_splitter, documents)
        
        # Verify the result is an empty list
        assert result == []
        
        # Verify the markdown_splitter's split_text method was called
        markdown_splitter.split_text.assert_called_once_with("Some content")