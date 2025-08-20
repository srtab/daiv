from collections.abc import Sequence
from typing import Any

from langchain.prompts import BasePromptTemplate, ChatPromptTemplate
from langchain.retrievers.document_compressors.listwise_rerank import LLMListwiseRerank
from langchain_core.callbacks import Callbacks
from langchain_core.documents import Document
from langchain_core.language_models import BaseLanguageModel
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from pydantic import BaseModel, Field

system = """ROLE
You rerank already-retrieved documents by their usefulness in completing the task described by the Intent. Assume keyword/semantic matching has been handled upstream; do not reward sheer token overlap.

INPUTS
- Query: {query}
- Intent: {intent}

DOCUMENTS
{context}

WHAT TO RETURN
- A JSON array of document IDs in descending relevance (e.g., [0,3,5,1,4,2]).
- No explanations, no scores, no extra text.
- Do not invent IDs. Deduplicate if the same ID appears more than once.
- If nothing is relevant, return [].

INTERPRETATION RULE
- The Intent is primary. Use the Query only to disambiguate or rerank when the Intent could mean multiple things or is missing.

TOKENIZATION & MATCHING (apply everywhere below)
- Exact identifiers/error codes are case-sensitive (e.g., FooBar, ERR_42, myModule.func).
- Natural-language words are case-insensitive.
- Treat dotted/segmented identifiers as a unit and by parts (e.g., "pkg.mod.func" matches the whole and the parts).
- Prefer whole-token matches (boundary chars: . _ / - : : #).

RANKING PRINCIPLES (apply in order)
1) Implementation readiness
   - Prefer concrete, executable code (function/class bodies, runnable SQL, live config blocks) over comments, docstrings, READMEs, or type-only stubs.
   - Prefer definitions + their call sites/wiring over isolated declarations.

2) Production over test/demo (with an exception)
   - Deprioritize tests/mocks/fixtures/examples/tutorials by default.
   - Exception: If these contain the clearest, most direct path to implement the Intent and no production chunk is comparable, rank them accordingly.

3) Path centrality & filename signals
   - Prefer core/source locations (src/, app/, lib/, core/, server/, services/, pkg/).
   - Prefer paths/filenames that semantically align with the Intent (feature/module names, domain terms).
   - Deprioritize vendor/, third_party/, build/, dist/, generated/, tmp/, sandbox/.

4) Framework/runtime alignment
   - If the Intent mentions a framework/library/endpoint/runtime or version, prefer documents that *use* those cues (imports, decorators, annotations, routing, CLI wiring, config activation), not just mention them.

5) Negative file-type signals
   - Strongly deprioritize changelogs and release notes (CHANGELOG, CHANGES, RELEASE_NOTES, HISTORY).
   - Also deprioritize lockfiles and machine-generated artifacts (*.lock, package-lock.json, yarn.lock, go.sum, Cargo.lock, *.min.*, *.map, *.pb.go, *.g.dart).

6) Freshness & authority hints (if present in paths/text)
   - Prefer non-legacy areas over legacy/, old/, deprecated/, archive/.
   - Prefer current-version dirs (e.g., v2/) when Intent targets that version.

7) Cohesion & locality
   - Prefer chunks where relevant material is concentrated within the same logical block (function/class/config section) rather than dispersed across unrelated areas.

TIE-BREAKERS (in order)
A) Higher density of implementation lines that directly support the Intent (not comments).
B) Shallower, more “central” paths over deep or peripheral ones.
C) More explicit framework/runtime usage relevant to the Intent.
D) If upstream retriever scores are available, prefer the higher-scored item.
E) If still tied, return IDs in ascending lexicographic order (determinism)."""  # noqa: E501

_DEFAULT_PROMPT = ChatPromptTemplate.from_messages([("system", system)])


def _get_prompt_input(input_: dict) -> dict[str, Any]:
    """Return the compression chain input."""
    documents = input_["documents"]
    context = "<documents>\n"
    for index, doc in enumerate(documents):
        context += f"<document ID='{index}' path='{doc.metadata['source']}'>\n{doc.page_content}\n</document>\n"
    context += "</documents>\n\n"
    document_range = "empty list"
    if len(documents) > 0:
        document_range = f"Document ID: 0, ..., Document ID: {len(documents) - 1}"
    context += f"Documents = [{document_range}]"
    return {"query": input_["query"], "context": context, "intent": input_["intent"]}


def _parse_ranking(results: dict) -> list[Document]:
    ranking = results["ranking"]
    docs = results["documents"]
    return [docs[i] for i in ranking.ranked_document_ids]


class CodebaseSearchReranker(LLMListwiseRerank):
    """
    Reranker for codebase search.
    """

    intent: str
    """The intent of the search query, why you are searching for this code."""

    def compress_documents(
        self, documents: Sequence[Document], query: str, callbacks: Callbacks | None = None
    ) -> Sequence[Document]:
        """Filter down documents based on their relevance to the query."""
        unique_docs: dict[str, Document] = {doc.id: doc for doc in documents}

        results = self.reranker.invoke(
            {"documents": list(unique_docs.values()), "query": query, "intent": self.intent or "(Not specified)"},
            config={"callbacks": callbacks},
        )
        return results[: self.top_n]

    @classmethod
    def from_llm(
        cls,
        llm: BaseLanguageModel,
        *,
        prompt: BasePromptTemplate | None = None,
        intent: str | None = None,
        **kwargs: Any,
    ) -> "LLMListwiseRerank":
        """Create a LLMListwiseRerank document compressor from a language model.

        Args:
            llm: The language model to use for filtering. **Must implement
                BaseLanguageModel.with_structured_output().**
            prompt: The prompt to use for the filter.
            intent: The intent to use for the filter.
            kwargs: Additional arguments to pass to the constructor.

        Returns:
            A LLMListwiseRerank document compressor that uses the given language model.
        """

        if llm.with_structured_output == BaseLanguageModel.with_structured_output:
            msg = f"llm of type {type(llm)} does not implement `with_structured_output`."
            raise ValueError(msg)

        class RankDocuments(BaseModel):
            """Rank the documents by their relevance to the user question/intent.
            Rank from most to least relevant."""

            ranked_document_ids: list[int] = Field(
                ...,
                description=(
                    "The integer IDs of the documents, sorted from most to least relevant to the user question."
                ),
            )

        _prompt = prompt or _DEFAULT_PROMPT
        reranker = RunnablePassthrough.assign(
            ranking=RunnableLambda(_get_prompt_input) | _prompt | llm.with_structured_output(RankDocuments)
        ) | RunnableLambda(_parse_ranking)
        return cls(reranker=reranker, intent=intent or "(Not specified)", **kwargs)
