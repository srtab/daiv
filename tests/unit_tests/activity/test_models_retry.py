import pytest
from activity.models import Activity, ActivityStatus, TriggerType


@pytest.mark.django_db
class TestIsRetryable:
    def _make(self, status: str, trigger: str) -> Activity:
        return Activity(status=status, trigger_type=trigger, repo_id="acme/repo")

    @pytest.mark.parametrize(
        "trigger", [TriggerType.API_JOB, TriggerType.MCP_JOB, TriggerType.SCHEDULE, TriggerType.UI_JOB]
    )
    @pytest.mark.parametrize("status", [ActivityStatus.SUCCESSFUL, ActivityStatus.FAILED])
    def test_terminal_non_webhook_is_retryable(self, status, trigger):
        assert self._make(status, trigger).is_retryable is True

    @pytest.mark.parametrize("status", [ActivityStatus.READY, ActivityStatus.RUNNING])
    def test_non_terminal_not_retryable(self, status):
        assert self._make(status, TriggerType.API_JOB).is_retryable is False

    @pytest.mark.parametrize("trigger", [TriggerType.ISSUE_WEBHOOK, TriggerType.MR_WEBHOOK])
    @pytest.mark.parametrize("status", [ActivityStatus.SUCCESSFUL, ActivityStatus.FAILED])
    def test_webhook_not_retryable_even_when_terminal(self, status, trigger):
        assert self._make(status, trigger).is_retryable is False
