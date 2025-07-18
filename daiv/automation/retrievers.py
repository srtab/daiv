import textwrap
from typing import override

from langchain.retrievers.multi_query import LineListOutputParser, MultiQueryRetriever
from langchain_core.documents import Document
from langchain_core.language_models import BaseLanguageModel
from langchain_core.messages import SystemMessage
from langchain_core.prompts import BasePromptTemplate, ChatPromptTemplate, HumanMessagePromptTemplate, PromptTemplate
from langchain_core.retrievers import BaseRetriever

DEFAULT_QUERY_PROMPT = PromptTemplate(
    input_variables=["question"],
    template=textwrap.dedent(
        """\
        You are an AI language model assistant. Your task is to generate 3 different versions of the given user question to retrieve relevant documents from a vector database.

        By generating multiple perspectives on the user question, your goal is to help the user overcome some of the limitations of distance-based similarity search. Provide these alternative questions separated by newlines. Maintain the original meaning of the question as much as possible. Don't loose precision in the query

        The output should be a list of questions, separated by newlines, with no numbering or additional formatting.
        Original question: {question}
        """  # NOQA: E501
    ),
)

REPHRASE_SYSTEM = SystemMessage(
    textwrap.dedent(
        """\
        You are an assistant tasked with taking 3 coding-related queries in natural language from a user and converting them into 3 queries optimized for a semantic search on a vector database.
        In this process, strip out information that is not relevant for semantic matching, such as filler words. Maintain the original meaning of the question as much as possible. Don't loose precision in the query.
        The output should be a list of queries, separated by newlines, with no numbering or additional formatting.
        """  # NOQA: E501
    )
)

REPHRASE_HUMAN = HumanMessagePromptTemplate.from_template(
    "{% for query in queries %}{{ query }}\n{% endfor %}", template_format="jinja2"
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
        rephrase_prompt = ChatPromptTemplate.from_messages([REPHRASE_SYSTEM, REPHRASE_HUMAN])

        output_parser = LineListOutputParser()
        llm_chain = prompt | llm | output_parser | rephrase_prompt | llm | output_parser
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
