from datetime import time

from django.core.exceptions import ValidationError

import pytest
from notifications.choices import NotifyOn

from schedules.models import Frequency, ScheduleTemplate


@pytest.mark.django_db
class TestScheduleTemplateClean:
    def _make(self, **overrides):
        defaults = {
            "name": "Nightly scan",
            "prompt": "Scan the repo for issues.",
            "repo_id": "owner/repo",
            "frequency": Frequency.DAILY,
            "time": time(2, 0),
            "notify_on": NotifyOn.NEVER,
        }
        defaults.update(overrides)
        return ScheduleTemplate(**defaults)

    def test_daily_requires_time(self):
        tpl = self._make(time=None)
        with pytest.raises(ValidationError) as exc:
            tpl.full_clean()
        assert "time" in exc.value.message_dict

    def test_custom_requires_cron(self):
        tpl = self._make(frequency=Frequency.CUSTOM, cron_expression="", time=None)
        with pytest.raises(ValidationError) as exc:
            tpl.full_clean()
        assert "cron_expression" in exc.value.message_dict

    def test_custom_rejects_invalid_cron(self):
        tpl = self._make(frequency=Frequency.CUSTOM, cron_expression="not a cron", time=None)
        with pytest.raises(ValidationError) as exc:
            tpl.full_clean()
        assert "cron_expression" in exc.value.message_dict

    def test_hourly_does_not_require_time(self):
        tpl = self._make(frequency=Frequency.HOURLY, time=None)
        tpl.full_clean()

    def test_repo_id_optional(self):
        tpl = self._make(repo_id="")
        tpl.full_clean()

    def test_valid_daily_template(self):
        tpl = self._make()
        tpl.full_clean()
