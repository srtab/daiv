import json
import uuid
from datetime import datetime

from django.http import HttpRequest, StreamingHttpResponse

from langchain.storage.in_memory import InMemoryStore
from langchain_core.messages.utils import message_chunk_to_message
from langchain_core.prompts import SystemMessagePromptTemplate
from ninja import Router

from automation.agents.base import CODING_COST_EFFICIENT_MODEL_NAME
from automation.agents.prebuilt import REACTAgent
from automation.agents.review_addressor.agent import respond_reviewer_system
from automation.tools.toolkits import ReadRepositoryToolkit, SandboxToolkit
from chat.api.schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex
from core.config import RepositoryConfig

router = Router()


@router.post("/chat/completions", response=ChatCompletionResponse | dict)
async def create_chat_completion(request: HttpRequest, payload: ChatCompletionRequest):
    repo_client = RepoClient.create_instance()
    repo_config = RepositoryConfig.get_config("dipcode/django-webhooks")
    codebase_index = CodebaseIndex(repo_client)

    messages = [msg.dict() for msg in payload.messages]

    toolkit = ReadRepositoryToolkit.create_instance(repo_client, "dipcode/django-webhooks", repo_config.default_branch)
    sandbox_toolkit = SandboxToolkit.create_instance()

    system_message_template = SystemMessagePromptTemplate.from_template(
        respond_reviewer_system, "jinja2", additional_kwargs={"cache-control": {"type": "ephemeral"}}
    )
    system_message = system_message_template.format(
        diff="",
        project_description=repo_config.repository_description,
        repository_structure=codebase_index.extract_tree("dipcode/django-webhooks", repo_config.default_branch),
    )

    react_agent = REACTAgent(
        run_name="chat_completion_react_agent",
        tools=toolkit.get_tools() + sandbox_toolkit.get_tools(),
        model_name=CODING_COST_EFFICIENT_MODEL_NAME,
        store=InMemoryStore(),
    )

    # Non-streaming completion
    if not payload.stream:
        try:
            result = react_agent.agent.invoke({"messages": [system_message] + messages})
            return ChatCompletionResponse(
                id=str(uuid.uuid4()),
                created=int(datetime.now().timestamp()),
                choices=[{"index": 1, "message": result["messages"][-1].content, "finish_reason": "stop"}],
            )
        except Exception as e:
            return {"error": str(e)}

    # Streaming completion
    async def generate_stream():
        try:
            async for chunk in react_agent.agent.astream(
                {"messages": [system_message] + messages}, stream_mode="messages"
            ):
                if chunk[0]:
                    message = message_chunk_to_message(chunk[0])
                    message = message.model_dump(mode="json")
                    if (
                        isinstance(message["content"], str)
                        or isinstance(message["content"], list)
                        and "text" in message["content"][0]
                    ):
                        chat_chunk = ChatCompletionChunk(
                            id=str(uuid.uuid4()),
                            created=int(datetime.now().timestamp()),
                            choices=[{"index": 1, "delta": {"content": message["content"], "role": message["type"]}}],
                        )
                        yield f"data: {chat_chunk.model_dump_json()}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingHttpResponse(generate_stream(), content_type="text/event-stream")


@router.get("/models")
def get_models(request: HttpRequest):
    return {
        "object": "list",
        "data": [{"id": "dipcode-gpt", "object": "model", "created": 1727404800, "owned_by": "dipcode"}],
    }
