from typing import override

from langchain.retrievers.multi_query import DEFAULT_QUERY_PROMPT, LineListOutputParser, MultiQueryRetriever
from langchain_core.documents import Document
from langchain_core.language_models import BaseLanguageModel
from langchain_core.prompts import BasePromptTemplate
from langchain_core.prompts.prompt import PromptTemplate
from langchain_core.retrievers import BaseRetriever

OUTPUT_FORMAT_PROMPT = (
    "\nThe output should be a list of queries, separated by newlines, with no numbering or additional formatting."
)

REPHRASE_QUERY_PROMPT = PromptTemplate.from_template(
    """\
    You are an assistant tasked with taking 3 natural language queries from a user and converting them into 3 queries for a vectorstore.
    In this process, you strip out information that is not relevant for the retrieval task. Here are the user queries:  {% for query in queries %}
     - {{ query }}
    {% endfor %}
    """  # noqa: E501
    + OUTPUT_FORMAT_PROMPT,
    template_format="jinja2",
)


class MultiQueryRephraseRetriever(MultiQueryRetriever):
    @classmethod
    @override
    def from_llm(
        cls,
        retriever: BaseRetriever,
        llm: BaseLanguageModel,
        prompt: BasePromptTemplate = DEFAULT_QUERY_PROMPT,
        parser_key: str | None = None,
        include_original: bool = False,
    ) -> "MultiQueryRephraseRetriever":
        """Initialize from llm using default template.

        Args:
            retriever: retriever to query documents from
            llm: llm for query generation using DEFAULT_QUERY_PROMPT
            prompt: The prompt which aims to generate several different versions
                of the given user query
            include_original: Whether to include the original query in the list of
                generated queries.

        Returns:
            MultiQueryRephraseRetriever
        """
        prompt.template += OUTPUT_FORMAT_PROMPT

        output_parser = LineListOutputParser()
        llm_chain = prompt | llm | output_parser | REPHRASE_QUERY_PROMPT | llm | output_parser
        return cls(retriever=retriever, llm_chain=llm_chain, include_original=include_original)

    @override
    def unique_union(self, documents: list[Document]) -> list[Document]:
        """
        Get unique Documents based on their id.

        Args:
            documents: List of retrieved Documents

        Returns:
            List of unique retrieved Documents
        """
        unique_docs: dict[str | None, Document] = {doc.metadata.get("id"): doc for doc in documents}
        return list(unique_docs.values())
