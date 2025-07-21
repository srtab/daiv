from langchain_text_splitters import TextSplitter


class ChonkieTextSplitter(TextSplitter):
    """
    A text splitter using CodeChunker from chonkie package.

    This splitter is specialized in code splitting, and will try to split the text along code blocks.
    It's based on tree-sitter, and will use the language parser to split the text, which will try to split along
    code units like functions, classes, etc. instead of just splitting by separator characters.

    For more information, see the chonkie documentation:
    https://docs.chonkie.ai/chunkers/code-chunker#api-reference
    """

    def __init__(self, language: str, **kwargs):
        super().__init__(**kwargs)
        try:
            from chonkie import CodeChunker
        except ImportError:
            raise ImportError(
                "chonkie package is not installed. Please install it with `pip install chonkie[code]`."
            ) from None

        self.chonkie = CodeChunker(
            language=language, tokenizer_or_token_counter=self._length_function, chunk_size=self._chunk_size
        )

    def split_text(self, text: str) -> list[str]:
        """
        Split the text into chunks.
        """
        return [chunk.text for chunk in self.chonkie.chunk(text)]
