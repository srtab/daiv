import pytest
from notifications.choices import NotifyOn

from schedules.forms import ScheduledJobCreateForm


def _valid_data(**overrides):
    data = {
        "name": "s",
        "prompt": "p",
        "repo_id": "x/y",
        "ref": "",
        "frequency": "daily",
        "cron_expression": "",
        "time": "12:00",
        "use_max": False,
        "notify_on": NotifyOn.NEVER,
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestScheduledJobCreateForm:
    def test_valid_with_notify_never(self):
        form = ScheduledJobCreateForm(data=_valid_data())
        assert form.is_valid(), form.errors

    def test_valid_with_notify_always(self):
        form = ScheduledJobCreateForm(data=_valid_data(notify_on=NotifyOn.ALWAYS))
        assert form.is_valid(), form.errors
