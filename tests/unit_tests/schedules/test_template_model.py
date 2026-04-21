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


class TestScheduleTemplateFrequencySummary:
    """`ScheduleTemplate.frequency_summary` renders a one-line human cadence."""

    def _make(self, **overrides):
        defaults = {"name": "Sample", "prompt": "Do work.", "frequency": Frequency.DAILY, "time": time(9, 0)}
        defaults.update(overrides)
        return ScheduleTemplate(**defaults)

    def test_hourly_returns_every_hour(self):
        tpl = self._make(frequency=Frequency.HOURLY, time=None)
        assert tpl.frequency_summary == "Every hour"

    def test_custom_embeds_cron(self):
        tpl = self._make(frequency=Frequency.CUSTOM, cron_expression="0 */6 * * *", time=None)
        assert tpl.frequency_summary == "Custom: 0 */6 * * *"

    def test_daily_with_time(self):
        tpl = self._make(frequency=Frequency.DAILY, time=time(9, 30))
        assert tpl.frequency_summary == "Daily at 09:30"

    def test_weekly_with_time(self):
        tpl = self._make(frequency=Frequency.WEEKLY, time=time(8, 0))
        assert tpl.frequency_summary == "Weekly at 08:00"

    def test_weekdays_with_time(self):
        tpl = self._make(frequency=Frequency.WEEKDAYS, time=time(18, 0))
        assert tpl.frequency_summary == "Weekdays at 18:00"

    def test_daily_without_time_falls_back_to_label(self):
        # Defensive branch: clean() normally forbids this, but the summary
        # should never raise on a partially-constructed instance.
        tpl = self._make(frequency=Frequency.DAILY, time=None)
        assert tpl.frequency_summary == "Daily"
