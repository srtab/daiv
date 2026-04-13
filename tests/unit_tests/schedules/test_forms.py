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
        "notify_channels": [],
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestScheduledJobCreateForm:
    def test_accepts_never_with_no_channels(self):
        form = ScheduledJobCreateForm(data=_valid_data())
        assert form.is_valid(), form.errors

    def test_rejects_always_with_no_channels(self):
        form = ScheduledJobCreateForm(data=_valid_data(notify_on=NotifyOn.ALWAYS, notify_channels=[]))
        assert not form.is_valid()
        assert "notify_channels" in form.errors

    def test_accepts_always_with_email(self):
        form = ScheduledJobCreateForm(data=_valid_data(notify_on=NotifyOn.ALWAYS, notify_channels=["email"]))
        assert form.is_valid(), form.errors

    def test_rejects_unknown_channel(self):
        form = ScheduledJobCreateForm(data=_valid_data(notify_on=NotifyOn.ALWAYS, notify_channels=["bogus"]))
        assert not form.is_valid()
        assert "notify_channels" in form.errors
