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
            "repos": [{"repo_id": "owner/repo", "ref": ""}],
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

    def test_repos_optional(self):
        tpl = self._make(repos=[])
        tpl.full_clean()

    def test_repos_rejects_malformed_shape(self):
        tpl = self._make(repos=[{"repo_id": ""}])
        with pytest.raises(ValidationError) as exc:
            tpl.full_clean()
        assert "repos" in exc.value.message_dict

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


@pytest.mark.django_db
def test_once_template_frequency_summary_prompts_for_date():
    tpl = ScheduleTemplate.objects.create(name="oneoff-tpl", prompt="p", frequency=Frequency.ONCE)
    assert tpl.frequency_summary == "Once (pick a date)"


class TestScheduleTemplateReposSummary:
    """`ScheduleTemplate.repos_summary` renders a one-line summary of default repos."""

    def _make(self, **overrides):
        defaults = {"name": "Sample", "prompt": "Do work.", "frequency": Frequency.DAILY, "time": time(9, 0)}
        defaults.update(overrides)
        return ScheduleTemplate(**defaults)

    def test_empty_falls_back_to_any_repo(self):
        tpl = self._make(repos=[])
        assert tpl.repos_summary == "Any repo"

    def test_single_repo_no_ref(self):
        tpl = self._make(repos=[{"repo_id": "owner/repo", "ref": ""}])
        assert tpl.repos_summary == "owner/repo"

    def test_single_repo_with_ref(self):
        tpl = self._make(repos=[{"repo_id": "owner/repo", "ref": "main"}])
        assert tpl.repos_summary == "owner/repo @ main"

    def test_multiple_repos_truncates_with_counter(self):
        tpl = self._make(
            repos=[{"repo_id": "a/b", "ref": ""}, {"repo_id": "c/d", "ref": "dev"}, {"repo_id": "e/f", "ref": ""}]
        )
        assert tpl.repos_summary == "a/b +2 more"


@pytest.mark.django_db
class TestScheduleTemplateToPickerDict:
    """`ScheduleTemplate.to_picker_dict` returns the exact shape the gallery consumes."""

    def _make_tpl(self, **kwargs):
        defaults = {
            "name": "Nightly scan",
            "description": "Audit the codebase.",
            "prompt": "Scan for issues.",
            "repos": [{"repo_id": "owner/repo", "ref": "main"}],
            "frequency": Frequency.DAILY,
            "time": time(2, 0),
            "notify_on": NotifyOn.ALWAYS,
            "use_max": True,
        }
        defaults.update(kwargs)
        return ScheduleTemplate.objects.create(**defaults)

    def test_keys(self):
        tpl = self._make_tpl()
        row = tpl.to_picker_dict()
        assert set(row.keys()) == {
            "id",
            "name",
            "description",
            "repos",
            "repos_summary",
            "frequency_display",
            "frequency_summary",
            "notify_on_display",
            "use_max",
        }

    def test_excludes_prompt(self):
        tpl = self._make_tpl()
        assert "prompt" not in tpl.to_picker_dict()

    def test_values(self):
        tpl = self._make_tpl(name="Weekly audit", repos=[], use_max=False)
        row = tpl.to_picker_dict()
        assert row["id"] == tpl.id
        assert row["name"] == "Weekly audit"
        assert row["repos"] == []
        assert row["repos_summary"] == "Any repo"
        assert row["use_max"] is False
        assert row["frequency_display"] == "Daily"
        assert row["frequency_summary"] == "Daily at 02:00"
        assert row["notify_on_display"] == "Always"
