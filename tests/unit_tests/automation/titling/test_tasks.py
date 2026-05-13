from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest
from activity.models import Activity, TriggerType

from automation.titling import tasks as titling_tasks
from automation.titling.tasks import GeneratedTitle, _ref_is_informative, generate_batch_title_task, generate_title_task


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("", False),
        ("main", False),
        ("MAIN", False),
        ("master", False),
        ("develop", False),
        ("prod", False),
        ("Production", False),
        ("a1b2c3d", False),  # 7-char SHA
        ("DEADBEEFCAFE1234567890ABCDEF1234567890AB", False),  # 40-char SHA, mixed case
        ("feat/copilotkit-chat", True),
        ("bugfix-123", True),
        ("a1b2c3", True),  # 6 chars — too short for SHA pattern
        ("g1h2i3j4", True),  # contains non-hex chars
        ("release/2026-04", True),
    ],
)
def test_ref_is_informative(ref: str, expected: bool):
    assert _ref_is_informative(ref) is expected


def _fake_chain(title: str = "Generated test title", capture: dict | None = None):
    """Build a Mock that mimics ``llm.with_structured_output(...).with_retry(...).with_fallbacks(...)``."""
    chain = MagicMock()
    chain.with_structured_output.return_value = chain
    chain.with_retry.return_value = chain
    chain.with_fallbacks.return_value = chain
    chain.with_config.return_value = chain

    def _invoke(messages):
        if capture is not None:
            capture["messages"] = messages
        return GeneratedTitle(title=title)

    chain.invoke.side_effect = _invoke
    return chain


@pytest.mark.django_db
class TestGenerateTitleTask:
    def _make_activity(self, *, title: str = "") -> Activity:
        return Activity.objects.create(trigger_type=TriggerType.API_JOB, repo_id="group/repo", title=title)

    def test_returns_silently_when_entity_missing(self):
        with patch.object(titling_tasks.BaseAgent, "get_model") as get_model:
            generate_title_task.func(
                entity_type="activity", pk="00000000-0000-0000-0000-000000000000", prompt="any", repo_id="x/y"
            )
        get_model.assert_not_called()

    def test_overwrites_existing_heuristic_title(self):
        """Heuristic titles set at creation are placeholders; the LLM-generated
        title must overwrite them. (No user-facing edit endpoint exists, so no
        need to protect manual edits.)
        """
        activity = self._make_activity(title="Heuristic placeholder")
        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(title="LLM generated")):
            generate_title_task.func(entity_type="activity", pk=str(activity.pk), prompt="any", repo_id="group/repo")
        activity.refresh_from_db()
        assert activity.title == "LLM generated"

    def test_returns_when_model_not_configured(self):
        activity = self._make_activity()
        with patch.object(titling_tasks.BaseAgent, "get_model", side_effect=RuntimeError("no key")):
            generate_title_task.func(entity_type="activity", pk=str(activity.pk), prompt="any", repo_id="group/repo")
        activity.refresh_from_db()
        assert activity.title == ""

    def test_writes_generated_title(self):
        activity = self._make_activity()
        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(title="Add login feature")):
            generate_title_task.func(
                entity_type="activity", pk=str(activity.pk), prompt="add login", repo_id="group/repo"
            )
        activity.refresh_from_db()
        assert activity.title == "Add login feature"

    def test_user_text_includes_branch_when_informative(self):
        activity = self._make_activity()
        capture: dict = {}
        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(capture=capture)):
            generate_title_task.func(
                entity_type="activity",
                pk=str(activity.pk),
                prompt="add login",
                repo_id="group/repo",
                ref="feat/copilotkit-chat",
            )
        human_text = capture["messages"][-1].content
        assert "Repository: group/repo" in human_text
        assert "Branch: feat/copilotkit-chat" in human_text
        assert "Task: add login" in human_text

    def test_user_text_omits_branch_for_generic_ref(self):
        activity = self._make_activity()
        capture: dict = {}
        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(capture=capture)):
            generate_title_task.func(
                entity_type="activity", pk=str(activity.pk), prompt="add login", repo_id="group/repo", ref="main"
            )
        human_text = capture["messages"][-1].content
        assert "Branch:" not in human_text

    def test_prompt_truncated_to_500_chars(self):
        activity = self._make_activity()
        capture: dict = {}
        long_prompt = "x" * 1000
        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(capture=capture)):
            generate_title_task.func(
                entity_type="activity", pk=str(activity.pk), prompt=long_prompt, repo_id="group/repo"
            )
        human_text = capture["messages"][-1].content
        assert human_text.endswith("x" * 500)
        assert "x" * 501 not in human_text


@pytest.mark.django_db
class TestGenerateBatchTitleTask:
    def _make_activity(self, batch_id, *, title: str = "", repo_id: str = "group/repo") -> Activity:
        return Activity.objects.create(
            trigger_type=TriggerType.API_JOB, repo_id=repo_id, batch_id=batch_id, title=title
        )

    def test_applies_single_title_to_all_batch_members(self):
        batch_id = uuid.uuid4()
        members = [self._make_activity(batch_id, repo_id=f"o/r{i}") for i in range(3)]

        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(title="Add login feature")):
            generate_batch_title_task.func(batch_id=str(batch_id), prompt="add login")

        for activity in members:
            activity.refresh_from_db()
            assert activity.title == "Add login feature"

    def test_invokes_llm_exactly_once_for_n_repos(self):
        batch_id = uuid.uuid4()
        for i in range(5):
            self._make_activity(batch_id, repo_id=f"o/r{i}")

        chain = _fake_chain(title="One shared title")
        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=chain):
            generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")

        # ``invoke`` is the actual LLM call; building the chain may call other Mock methods
        # so we assert on ``invoke`` directly.
        assert chain.invoke.call_count == 1

    def test_does_not_overwrite_already_titled_activities(self):
        batch_id = uuid.uuid4()
        activity = self._make_activity(batch_id, title="Already set")

        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(title="LLM choice")):
            generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")

        activity.refresh_from_db()
        assert activity.title == "Already set"

    def test_preserves_pre_existing_titles_in_mixed_batch(self):
        """Schedule runs set a synchronous title; LLM titles must not overwrite them."""
        batch_id = uuid.uuid4()
        prefilled = self._make_activity(batch_id, title="job · run #1", repo_id="o/sched")
        empty = self._make_activity(batch_id, repo_id="o/r")

        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(title="LLM choice")):
            generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")

        prefilled.refresh_from_db()
        empty.refresh_from_db()
        assert prefilled.title == "job · run #1"
        assert empty.title == "LLM choice"

    def test_user_text_omits_repo_and_branch_context(self):
        """Batch titling spans multiple repos, so per-repo context is intentionally dropped."""
        batch_id = uuid.uuid4()
        self._make_activity(batch_id, repo_id="o/r1")
        self._make_activity(batch_id, repo_id="o/r2")
        capture: dict = {}

        with patch.object(titling_tasks.BaseAgent, "get_model", return_value=_fake_chain(capture=capture)):
            generate_batch_title_task.func(batch_id=str(batch_id), prompt="add login")

        human_text = capture["messages"][-1].content
        assert "Repository:" not in human_text
        assert "Branch:" not in human_text
        assert "Task: add login" in human_text

    def test_returns_when_model_not_configured(self):
        batch_id = uuid.uuid4()
        activity = self._make_activity(batch_id)
        with patch.object(titling_tasks.BaseAgent, "get_model", side_effect=RuntimeError("no key")):
            generate_batch_title_task.func(batch_id=str(batch_id), prompt="task")
        activity.refresh_from_db()
        assert activity.title == ""
