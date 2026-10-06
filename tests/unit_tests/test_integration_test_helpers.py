import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from automation.agent.usage_tracking import _usage_metadata_var
from tests.integration_tests import utils as integration_utils
from tests.integration_tests.utils import (
    RunMetrics,
    _resolve_provider_slug,
    eval_metrics_row,
    measure,
    require_provider_for_model,
    write_eval_metrics_row,
)


@pytest.mark.parametrize(
    "model_spec,expected_slug",
    [
        ("openrouter:anthropic/claude-sonnet-4.6", "openrouter"),
        ("openrouter:openai/gpt-5.4-mini", "openrouter"),
        ("anthropic:claude-sonnet-4-6", "anthropic"),
        ("google:gemini-2.5-pro", "google"),
        ("openai:gpt-5.4", "openai"),
        ("customprovider:model-x", "customprovider"),
        # Bare-name heuristics
        ("gpt-5.4", "openai"),
        ("gpt-4-turbo", "openai"),
        ("o4-mini", "openai"),
        ("claude-haiku-4-5", "anthropic"),
        ("gemini-2.5-pro", "google_genai"),
        # Fall-through: unknown bare name returns itself unchanged so callers
        # (parse_model_spec) raise the canonical ValueError.
        ("not-a-known-model", "not-a-known-model"),
    ],
)
def test_resolve_provider_slug(model_spec: str, expected_slug: str) -> None:
    assert _resolve_provider_slug(model_spec) == expected_slug


def test_require_provider_skips_when_built_in_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception, match="OPENROUTER_API_KEY not set"):
        require_provider_for_model("openrouter:anthropic/claude-sonnet-4.6")


def test_require_provider_runs_when_built_in_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-key")
    require_provider_for_model("openrouter:anthropic/claude-sonnet-4.6")  # no raise


def test_require_provider_skips_when_custom_env_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAIV_TEST_PROVIDER_CUSTOMPROVIDER_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception, match="DAIV_TEST_PROVIDER_CUSTOMPROVIDER_API_KEY not set"):
        require_provider_for_model("customprovider:model-x")


def test_require_provider_runs_when_custom_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIV_TEST_PROVIDER_CUSTOMPROVIDER_API_KEY", "real-key")
    require_provider_for_model("customprovider:model-x")  # no raise


def test_require_provider_uses_bare_name_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(pytest.skip.Exception, match="ANTHROPIC_API_KEY not set"):
        require_provider_for_model("claude-haiku-4-5")


def test_discover_custom_slugs_extracts_non_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.integration_tests import utils as integration_utils
    from tests.integration_tests.conftest import _discover_custom_slugs

    monkeypatch.setattr(integration_utils, "CODING_MODEL_NAMES", ["openrouter:anthropic/claude-sonnet-4.6"])
    monkeypatch.setattr(integration_utils, "FAST_MODEL_NAMES", ["customprovider:model-x", "anthropic:claude-haiku-4-5"])

    slugs = _discover_custom_slugs()
    assert slugs == {"customprovider"}


def _item(**overrides) -> SimpleNamespace:
    marker = SimpleNamespace(kwargs={"test_suite_name": "DAIV: Skills"})
    fields = {
        "nodeid": "tests/integration_tests/test_skills.py::test_skill_activated[m-plan]",
        "name": "test_skill_activated[m-plan]",
        "module": SimpleNamespace(__name__="tests.integration_tests.test_skills"),
        "callspec": SimpleNamespace(params={"model_name": "openrouter:z-ai/glm-5.2"}),
        "get_closest_marker": lambda name: marker if name == "langsmith" else None,
        "fixturenames": ["model_name", "eval_request"],
    }
    return SimpleNamespace(**(fields | overrides))


def _report(outcome: str, *, when: str = "call") -> SimpleNamespace:
    return SimpleNamespace(
        when=when, passed=outcome == "passed", failed=outcome == "failed", skipped=outcome == "skipped"
    )


def _llm_end(handler, message: AIMessage) -> None:
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]))


class TestMeasure:
    def test_records_tokens_cache_reads_and_turns(self):
        request = SimpleNamespace(node=SimpleNamespace())
        reply = AIMessage(
            content="done",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_token_details": {"cache_read": 40},
            },
            response_metadata={"model_name": "unpriced/model-x"},
        )

        with measure(request) as metrics:
            _llm_end(_usage_metadata_var.get(), reply)
        metrics.messages = [HumanMessage("q"), AIMessage("looking"), reply]

        assert request.node.eval_metrics.as_row() == {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 40,
            "cost": None,
            "turns": 2,
        }

    def test_records_usage_when_the_block_raises(self):
        request = SimpleNamespace(node=SimpleNamespace())

        with pytest.raises(RuntimeError), measure(request):
            raise RuntimeError

        assert request.node.eval_metrics.usage["input_tokens"] == 0


class TestEvalMetricsRow:
    def test_carries_identity_outcome_and_measurements(self):
        metrics = RunMetrics(usage={"input_tokens": 5, "output_tokens": 1, "cache_read_tokens": 0, "cost": 0.01})
        metrics.extra["kind"] = "bug"

        row = eval_metrics_row(_item(eval_metrics=metrics), passed=True, run=2, git_sha="abc")

        assert row == {
            "nodeid": "tests/integration_tests/test_skills.py::test_skill_activated[m-plan]",
            "suite": "DAIV: Skills",
            "case": "test_skill_activated[m-plan]",
            "model": "openrouter:z-ai/glm-5.2",
            "run": 2,
            "git_sha": "abc",
            "passed": True,
            "input_tokens": 5,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "turns": 0,
            "cost": 0.01,
            "kind": "bug",
        }

    def test_an_unmeasured_test_gets_empty_measurements_and_its_module_as_suite(self):
        item = _item(get_closest_marker=lambda name: None, callspec=None)

        row = eval_metrics_row(item, passed=False, run=1, git_sha="abc")

        assert row["suite"] == "tests.integration_tests.test_skills"
        assert row["model"] is None
        assert row["input_tokens"] is None


class TestWriteEvalMetricsRow:
    @pytest.fixture(autouse=True)
    def _fixed_sha(self, monkeypatch):
        monkeypatch.setattr(integration_utils, "_git_sha", lambda: "abc")

    @pytest.fixture
    def out(self, monkeypatch, tmp_path):
        path = tmp_path / "metrics.jsonl"
        monkeypatch.setenv("DAIV_EVAL_METRICS_OUT", str(path))
        return path

    @staticmethod
    def rows(out) -> list[dict]:
        return [json.loads(line) for line in out.read_text().splitlines()]

    def test_writes_nothing_without_the_variable(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DAIV_EVAL_METRICS_OUT", raising=False)
        monkeypatch.chdir(tmp_path)

        write_eval_metrics_row(_item(eval_metrics=RunMetrics(usage={"input_tokens": 5})), _report("passed"))

        assert list(tmp_path.iterdir()) == []

    def test_appends_one_row_per_call_phase(self, monkeypatch, out):
        monkeypatch.setenv("DAIV_EVAL_RUN", "3")
        ran = _item(eval_metrics=RunMetrics(usage={"input_tokens": 5}))

        write_eval_metrics_row(ran, _report("passed", when="setup"))
        write_eval_metrics_row(ran, _report("failed"))
        write_eval_metrics_row(ran, _report("passed", when="teardown"))

        [row] = self.rows(out)
        assert (row["run"], row["passed"]) == (3, False)

    @pytest.mark.parametrize(
        "item,report",
        [
            pytest.param(
                _item(eval_metrics=RunMetrics(usage={"input_tokens": 5})),
                _report("skipped", when="setup"),
                id="skipped-setup",
            ),
            pytest.param(_item(eval_metrics=RunMetrics(usage={"input_tokens": 5})), _report("skipped"), id="skipped"),
            pytest.param(_item(), _report("failed", when="setup"), id="failed-setup"),
            pytest.param(_item(), _report("passed"), id="never-measured"),
            pytest.param(_item(), _report("failed"), id="failed-unmeasured"),
            pytest.param(
                _item(eval_metrics=RunMetrics(usage={"input_tokens": 0})), _report("failed"), id="failed-before-tokens"
            ),
        ],
    )
    def test_a_pass_that_cast_no_vote_writes_a_null_row(self, out, item, report):
        write_eval_metrics_row(item, report)

        [row] = self.rows(out)
        assert row["passed"] is None

    def test_a_test_that_is_not_an_eval_case_writes_nothing(self, out):
        write_eval_metrics_row(_item(fixturenames=["model_name"]), _report("failed"))

        assert not out.exists()

    def test_a_failure_after_the_agent_ran_is_a_vote(self, out):
        item = _item(eval_metrics=RunMetrics(usage={"input_tokens": 120, "output_tokens": 8}))

        write_eval_metrics_row(item, _report("failed"))

        [row] = self.rows(out)
        assert (row["passed"], row["input_tokens"]) == (False, 120)
