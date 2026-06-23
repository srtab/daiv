from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command

import pytest
from memory.models import MemoryObservation, ObservationCategory
from memory.tasks import CONSOLIDATION_MIN_PENDING


def _create_pending(repo_id, n):
    for i in range(n):
        MemoryObservation.objects.create(
            repo_id=repo_id, category=ObservationCategory.CODEBASE_FACT, content=f"observation {i} with some detail"
        )


@pytest.mark.django_db
class TestConsolidateMemoryCommand:
    def test_runs_consolidation_when_threshold_met(self):
        _create_pending("group/project", CONSOLIDATION_MIN_PENDING)
        out = StringIO()
        with patch("memory.management.commands.consolidate_memory.consolidate_memory_task") as task_mock:
            call_command("consolidate_memory", "--repo-id", "group/project", stdout=out)
        task_mock.call.assert_called_once_with("group/project")

    def test_below_threshold_requires_force(self):
        _create_pending("group/project", 2)
        with patch("memory.management.commands.consolidate_memory.consolidate_memory_task") as task_mock:
            with pytest.raises(CommandError, match="--force"):
                call_command("consolidate_memory", "--repo-id", "group/project")
            task_mock.call.assert_not_called()

            call_command("consolidate_memory", "--repo-id", "group/project", "--force", stdout=StringIO())
        task_mock.call.assert_called_once_with("group/project")

    def test_noop_when_nothing_pending(self, caplog):
        with (
            patch("memory.management.commands.consolidate_memory.consolidate_memory_task") as task_mock,
            caplog.at_level("WARNING", logger="daiv.memory"),
        ):
            call_command("consolidate_memory", "--repo-id", "group/empty")
        task_mock.call.assert_not_called()
        assert "No pending observations" in caplog.text
