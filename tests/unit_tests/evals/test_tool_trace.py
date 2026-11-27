"""Capture actual nested tool results without constructing a provider model."""

import json

import pytest
from deepagents.middleware.filesystem import FilesystemMiddleware
from deepagents.middleware.subagents import SubAgentMiddleware
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from automation.agent.middlewares.file_system import DAIVFilesystemBackend


class ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def trace_api():
    try:
        from evals import tool_trace
    except ImportError:
        pytest.fail("Opt-in tool-result tracing has not been implemented", pytrace=False)
    return tool_trace


def call(name, args, identifier):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])


def read_agent(tmp_path, calls):
    (tmp_path / "source.py").write_text("".join(f"line {i}\n" for i in range(1, 161)))
    return create_agent(
        model=ScriptedModel(messages=iter([*calls, AIMessage(content="finished")])),
        middleware=[
            FilesystemMiddleware(
                backend=DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True), tools=["read_file"]
            )
        ],
    )


def test_unset_trace_path_is_a_noop(trace_api, monkeypatch):
    monkeypatch.delenv("DAIV_EVAL_TRACE_OUT", raising=False)
    with trace_api.capture_tool_trace(nodeid="review::clean", model="scripted") as recorder:
        assert recorder is None


async def test_nested_reads_keep_arguments_results_and_detector_identity(trace_api, monkeypatch, tmp_path):
    path = tmp_path / "traces" / "run.jsonl"
    monkeypatch.setenv("DAIV_EVAL_TRACE_OUT", str(path))
    monkeypatch.setenv("DAIV_EVAL_RUN", "2")

    child = read_agent(
        tmp_path,
        [
            call("read_file", {"file_path": "/source.py"}, "first"),
            call("read_file", {"file_path": "/source.py", "offset": 100}, "second"),
        ],
    )
    parent = create_agent(
        model=ScriptedModel(
            messages=iter([
                call("task", {"subagent_type": "cr-correctness", "description": "read"}, "detector"),
                AIMessage(content="review done"),
            ])
        ),
        middleware=[
            SubAgentMiddleware(
                backend=DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True),
                subagents=[{"name": "cr-correctness", "description": "Check correctness", "runnable": child}],
            )
        ],
    )
    with trace_api.capture_tool_trace(nodeid="review::clean", model="scripted") as recorder:
        assert recorder is not None
        await parent.ainvoke({"messages": [{"role": "user", "content": "review"}]}, config={"callbacks": [recorder]})

    row = json.loads(path.read_text())
    assert (row["nodeid"], row["run"], row["model"]) == ("review::clean", 2, "scripted")
    reads = [e for e in row["events"] if e["tool"] == "read_file"]
    starts = [e for e in reads if e["event"] == "tool_start"]
    ends = [e for e in reads if e["event"] == "tool_end"]
    assert len(starts) == len(ends) == 2
    assert all(e["detector"] == "cr-correctness" for e in reads)
    assert starts[0]["args"] == {"file_path": "/source.py"}
    assert starts[1]["args"] == {"file_path": "/source.py", "offset": 100}
    assert "lines 1-100 of 160" in ends[0]["output"]["content"]
    assert "next offset 100" in ends[0]["output"]["content"]
    assert "lines 101-160 of 160" in ends[1]["output"]["content"]
    assert "line 160" in ends[1]["output"]["content"]
    assert starts[0]["run_id"] == ends[0]["run_id"]
    assert starts[1]["run_id"] == ends[1]["run_id"]
    visible = [e for e in reads if e["event"] == "model_tool_result"]
    assert len(visible) == 2
    assert visible[0]["output"]["content"] == ends[0]["output"]["content"]
    assert visible[1]["output"]["content"] == ends[1]["output"]["content"]
    assert all(e["result_detector"] == "cr-correctness" for e in visible)
    summaries = [e for e in row["events"] if e["tool"] == "task" and e["event"] == "model_tool_result"]
    assert len(summaries) == 1
    assert summaries[0]["detector"] is None
    assert summaries[0]["result_detector"] == "cr-correctness"


async def test_parallel_detectors_do_not_share_identity(trace_api, monkeypatch, tmp_path):
    path = tmp_path / "parallel.jsonl"
    monkeypatch.setenv("DAIV_EVAL_TRACE_OUT", str(path))

    names = ["cr-correctness", "cr-security"]
    children = [
        {
            "name": name,
            "description": "Review",
            "runnable": read_agent(tmp_path, [call("read_file", {"file_path": "/source.py"}, "read")]),
        }
        for name in names
    ]
    parent = create_agent(
        model=ScriptedModel(
            messages=iter([
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "task",
                            "args": {"subagent_type": "cr-correctness", "description": "read"},
                            "id": "one",
                        },
                        {"name": "task", "args": {"subagent_type": "cr-security", "description": "read"}, "id": "two"},
                    ],
                ),
                AIMessage(content="review done"),
            ])
        ),
        middleware=[
            SubAgentMiddleware(backend=DAIVFilesystemBackend(root_dir=tmp_path, virtual_mode=True), subagents=children)
        ],
    )
    with trace_api.capture_tool_trace(nodeid="review::parallel", model="scripted") as recorder:
        assert recorder is not None
        await parent.ainvoke({"messages": [{"role": "user", "content": "review"}]}, config={"callbacks": [recorder]})
    row = json.loads(path.read_text())
    ends = [e for e in row["events"] if e["tool"] == "read_file" and e["event"] == "tool_end"]
    assert len(ends) == 2
    assert {e["detector"] for e in ends} == {"cr-correctness", "cr-security"}
    visible = [e for e in row["events"] if e["tool"] == "read_file" and e["event"] == "model_tool_result"]
    assert len(visible) == 2
    assert {e["result_detector"] for e in visible} == {"cr-correctness", "cr-security"}


def test_invalid_output_path_fails_before_starting_an_agent(trace_api, monkeypatch, tmp_path):
    monkeypatch.setenv("DAIV_EVAL_TRACE_OUT", str(tmp_path))
    with pytest.raises(IsADirectoryError), trace_api.capture_tool_trace(nodeid="review::invalid", model="scripted"):
        pytest.fail("Invalid tracing destination must be rejected before the agent runs")


async def test_failure_still_writes_partial_trace(trace_api, monkeypatch, tmp_path):
    path = tmp_path / "failure.jsonl"
    monkeypatch.setenv("DAIV_EVAL_TRACE_OUT", str(path))

    @tool
    def broken() -> str:
        """Fail for this fixture."""
        raise ValueError("fixture failure")

    agent = create_agent(model=ScriptedModel(messages=iter([call("broken", {}, "broken")])), tools=[broken])
    with (
        pytest.raises(ValueError, match="fixture failure"),
        trace_api.capture_tool_trace(nodeid="review::failure", model="scripted") as recorder,
    ):
        assert recorder is not None
        await agent.ainvoke({"messages": [{"role": "user", "content": "go"}]}, config={"callbacks": [recorder]})
    row = json.loads(path.read_text())
    assert row["error"] == {"type": "ValueError", "message": "fixture failure"}
    errors = [e for e in row["events"] if e["event"] == "tool_error"]
    assert len(errors) == 1
    assert errors[0]["tool"] == "broken"
    assert errors[0]["error"] == {"type": "ValueError", "message": "fixture failure"}
