import pytest
from langsmith import testing as t
from openevals.llm import create_llm_as_judge
from openevals.prompts import CORRECTNESS_PROMPT, RAG_HELPFULNESS_PROMPT

from automation.agents.base import BaseAgent, ThinkingLevel
from automation.agents.codebase_chat import CodebaseChatAgent
from automation.agents.constants import ModelName

correctness_evaluator = create_llm_as_judge(
    prompt=CORRECTNESS_PROMPT,
    feedback_key="correctness",
    judge=BaseAgent.get_model(model=ModelName.O4_MINI, thinking_level=ThinkingLevel.MEDIUM),
)

rag_helpfulness_evaluator = create_llm_as_judge(
    prompt=RAG_HELPFULNESS_PROMPT,
    feedback_key="helpfulness",
    judge=BaseAgent.get_model(model=ModelName.O4_MINI, thinking_level=ThinkingLevel.MEDIUM),
)


@pytest.mark.django_db
@pytest.mark.langsmith(output_keys=["reference_outputs"])
@pytest.mark.parametrize(
    "question,reference_outputs",
    [
        (
            "What are the configuration options for the codebase chat agent?",
            """1 · Answer
- The codebase chat agent exposes three configurable settings:
  1. NAME — agent name. Default: "CodebaseChat" (env var: CODEBASE_CHAT_NAME).
  2. MODEL_NAME — model to use for chat. Default (code): ModelName.GPT_4_1; documented default string: "openrouter:openai/gpt-4-1-mini" (env var: CODEBASE_CHAT_MODEL_NAME).
  3. TEMPERATURE — sampling temperature (float). Default: 0.2 (env var: CODEBASE_CHAT_TEMPERATURE).

- These are implemented on CodebaseChatSettings with an env prefix CODEBASE_CHAT_. The settings file sets a secrets_dir and env_prefix for loading (model_config).

2 · References
- [daiv/automation/agents/codebase_chat/conf.py](http://gitlab:8929/srtab/daiv/-/blob/main/daiv/automation/agents/codebase_chat/conf.py)
- [docs/getting-started/environment-variables.md](http://gitlab:8929/srtab/daiv/-/blob/main/docs/getting-started/environment-variables.md)""",  # noqa: E501
        ),
        ("Locate all the agents defined in DAIV and what they do.", ""),
        (
            "How can i setup a test project on local GitLab to be used with DAIV?",
            """1 · Answer

Follow the DAIV docs to create and register a test repository on the local GitLab that ships with the DAIV development stack:

- Start the local environment
  1. Clone DAIV and copy the example secrets file:
     - cp docker/local/app/config.secrets.env.example docker/local/app/config.secrets.env
     - Add your GitLab personal access token (and any other API keys) into docker/local/app/config.secrets.env.
  2. Start the services:
     - docker compose up --build
     - The local GitLab UI will be available at http://localhost:8929.

- Create a GitLab personal access token
  - Create a token with api scope and save it. Add that token into docker/local/app/config.secrets.env so DAIV can call GitLab.

- Create / import a test project into the local GitLab
  - Create a new project in the local GitLab and push your test code, or use “Import by repository URL” (Admin Area → Settings → General → Import and export settings → enable Repository by URL) to import an existing repo.

- (If needed) get the initial root password for the local GitLab container
  - Run: docker compose exec -it gitlab grep 'Password:' /etc/gitlab/initial_root_password

- Configure webhooks and index the repository so DAIV can see changes
  1. Enter the app container:
     - docker compose exec -it app bash
  2. Set up webhooks:
     - django-admin setup_webhooks
     - (For local dev you can add --disable-ssl-verification to the command.)
     - If you see “Invalid url given” when creating webhooks, enable outbound requests to the local network in GitLab Admin Area → Settings → Network → “Allow requests to the local network from webhooks and integrations”.
  3. Update DAIV’s repository index:
     - django-admin update_index

- Test the integration
  - Create an issue in the project titled, e.g., "Add hello world function", add the label daiv, and DAIV should automatically comment with a plan.

Notes / edge details from the docs:
- The GitLab container is defined in docker-compose.yml and exposes ports 8929 (HTTP) and 2224 (SSH).
- Use a token with api scope (created in GitLab → Access Tokens).
- The README and configuration docs include the exact commands above and tips for import/webhook troubleshooting.

2 · References
- [README.md](http://gitlab:8929/srtab/daiv/-/blob/main/README.md)
- [docs/getting-started/configuration.md](http://gitlab:8929/srtab/daiv/-/blob/main/docs/getting-started/configuration.md)
- [docker-compose.yml](http://gitlab:8929/srtab/daiv/-/blob/main/docker-compose.yml)
- [CHANGELOG.md](http://gitlab:8929/srtab/daiv/-/blob/main/CHANGELOG.md)""",  # noqa: E501
        ),
        (
            "What are the supported models in DAIV?",
            """1 · Answer
- DAIV explicitly enumerates these supported LLM model names (ModelName enum):
  - openrouter:anthropic/claude-sonnet-4
  - openrouter:anthropic/claude-opus-4
  - openrouter:openai/gpt-4.1
  - openrouter:openai/gpt-4.1-mini
  - openrouter:openai/gpt-4.1-nano
  - openrouter:openai/o4-mini
  - openrouter:openai/o3
  - openrouter:deepseek/deepseek-chat-v3-0324
  - openrouter:google/gemini-2.5-pro

- DAIV also defines supported model providers (ModelProvider enum):
  - anthropic
  - openai
  - google_genai
  - openrouter

If you want where agents pick models from (env var names / defaults), see the docs/config referenced in the repository.

2 · References
- [daiv/automation/agents/constants.py](http://gitlab:8929/srtab/daiv/-/blob/main/daiv/automation/agents/constants.py)
- [daiv/automation/agents/base.py](http://gitlab:8929/srtab/daiv/-/blob/main/daiv/automation/agents/base.py)""",  # noqa: E501
        ),
        (
            "Is there a way to configure embeddings for the codebase? If yes, what are the options?",
            """1 · Answer
- Yes. The codebase supports configurable embeddings via settings. The provider/model is selected from settings.EMBEDDINGS_MODEL_NAME which is parsed as "provider/model" and currently supports:
  - openai (OpenAIEmbeddings)
  - huggingface (HuggingFaceEmbeddings)
  - voyageai (VoyageAIEmbeddings)
  The selection and construction logic is implemented in embeddings_function() and raises on unsupported providers.
- Configurable options (settings/environment variables):
  - EMBEDDINGS_MODEL_NAME (e.g., "openai/text-embedding-3-large", "huggingface/Alibaba-NLP/gte-modernbert-base", "voyageai/voyage-code-3")
  - EMBEDDINGS_API_KEY (optional; used when present)
  - EMBEDDINGS_DIMENSIONS (integer; vector dimension; default 1536; changing it requires recreating the CodebaseDocument table and re-indexing)
  - EMBEDDINGS_BATCH_SIZE (integer; used for OpenAI batch/chunk sizing)
  - CODEBASE_CHUNK_SIZE and CODEBASE_CHUNK_OVERLAP (document chunking settings referenced in docs)
- Behavioural notes from the implementation:
  - For OpenAI the code passes model (model name after the provider slash), dimensions, and chunk_size (from EMBEDDINGS_BATCH_SIZE) into OpenAIEmbeddings.
  - For HuggingFace it uses HuggingFaceEmbeddings with model_name and a cache folder under DATA_DIR/embeddings.
  - For VoyageAI it passes model, output_dimension (with a special-case mapping when EMBEDDINGS_DIMENSIONS == 1536), and a batch size constant DEFAULT_VOYAGE_3_BATCH_SIZE plus API key if provided.
  - Token-counting for embeddings differs by provider: voyageai uses the client.count_tokens, openai uses tiktoken.encoding_for_model, others default to cl100k_base.
- Warning: changing EMBEDDINGS_DIMENSIONS after indexing requires recreating the CodebaseDocument table and re-indexing documents.

2 · References
- daiv/codebase/search_engines/semantic.py (embeddings creation and provider handling): http://gitlab:8929/srtab/daiv/-/blob/main/daiv/codebase/search_engines/semantic.py
- daiv/codebase/conf.py (settings defaults and descriptions for EMBEDDINGS_*): http://gitlab:8929/srtab/daiv/-/blob/main/daiv/codebase/conf.py
- docs/getting-started/environment-variables.md (environment variables and examples): http://gitlab:8929/srtab/daiv/-/blob/main/docs/getting-started/environment-variables.md
- tests/codebase/search_engines/test_semantic.py (OpenAI/HuggingFace usage expectations): http://gitlab:8929/srtab/daiv/-/blob/main/tests/codebase/search_engines/test_semantic.py
- CHANGELOG.md (added support note for HuggingFace and VoyageAI): http://gitlab:8929/srtab/daiv/-/blob/main/CHANGELOG.md""",  # noqa: E501
        ),
        (
            "Hi, what is the capital of France?",
            """I can only answer using content from the repositories I can access. Please either:

1) Tell me which repository/file to look in (e.g., repo name and path), or
2) Confirm you want me to search the available repo(s) I have access to (srtab/daiv) for any mention of "France" or "capital".

Which do you want?""",  # noqa: E501
        ),
    ],
)
async def test_codebase_chat_correctness(question, reference_outputs):
    t.log_reference_outputs(reference_outputs)

    codebase_chat = await CodebaseChatAgent().agent

    t.log_inputs({"question": question})

    outputs = await codebase_chat.ainvoke({"messages": [("human", question)]})

    t.log_outputs({"response": outputs["messages"][-1].content})

    correctness_result = correctness_evaluator(
        inputs={"question": question},
        outputs={"response": outputs["messages"][-1].content},
        reference_outputs=reference_outputs,
    )
    assert correctness_result["score"] is True, correctness_result["comment"]

    rag_helpfulness_result = rag_helpfulness_evaluator(
        inputs={"question": question}, outputs={"response": outputs["messages"][-1].content}
    )
    assert rag_helpfulness_result["score"] is True, rag_helpfulness_result["comment"]
