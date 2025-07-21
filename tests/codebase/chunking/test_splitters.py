from unittest.mock import patch

from codebase.chunking.splitters import ChonkieTextSplitter


def test_chonkie_init():
    with patch("chonkie.CodeChunker") as mcode_chunker:
        splitter = ChonkieTextSplitter("python")
        mcode_chunker.assert_called_once_with(
            language="python", tokenizer_or_token_counter=splitter._length_function, chunk_size=splitter._chunk_size
        )


def test_chonkie_split_text():
    with patch("chonkie.CodeChunker") as mcode_chunker:
        splitter = ChonkieTextSplitter("python")
        splitter.split_text("Text example")
        mcode_chunker.return_value.chunk.assert_called_with("Text example")
