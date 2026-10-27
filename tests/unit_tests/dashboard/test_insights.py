from datetime import date, datetime, timedelta

from django.utils import timezone

import pytest
from dashboard.insights import Window, _bucket_starts, _change, _signed_pct, window_for
from sessions.filters import local_midnight


class TestWindow:
    def test_seven_days_ends_today_and_starts_six_days_before(self, monkeypatch):
        monkeypatch.setattr("django.utils.timezone.localdate", lambda: date(2026, 10, 27))

        window = window_for("7d")

        assert (window.start, window.end) == (date(2026, 10, 21), date(2026, 10, 27))

    def test_previous_is_the_same_length_just_before(self, monkeypatch):
        monkeypatch.setattr("django.utils.timezone.now", lambda: timezone.make_aware(datetime(2026, 11, 5, 9)))
        window = Window(date(2026, 10, 21), date(2026, 10, 27))

        assert window.previous() == Window(date(2026, 10, 14), date(2026, 10, 20), local_midnight(date(2026, 10, 21)))

    def test_today_compares_with_yesterday_up_to_the_same_time(self, monkeypatch):
        now = timezone.make_aware(datetime(2026, 10, 27, 9, 30))
        monkeypatch.setattr("django.utils.timezone.now", lambda: now)
        window = Window(date(2026, 10, 27), date(2026, 10, 27))

        assert window.previous() == Window(date(2026, 10, 26), date(2026, 10, 26), now - timedelta(days=1))

    def test_all_time_has_nothing_to_compare_with(self):
        assert Window(None, date(2026, 10, 27)).previous() is None

    @pytest.mark.parametrize(
        ("start", "bucket"),
        [(date(2026, 10, 21), "day"), (date(2026, 9, 28), "week"), (date(2026, 7, 30), "week"), (None, "month")],
    )
    def test_bucket_size_follows_the_length(self, start, bucket):
        assert Window(start, date(2026, 10, 27)).bucket == bucket

    def test_list_params(self):
        window = Window(date(2026, 10, 14), date(2026, 10, 20))

        assert window.list_params() == {"date_from": "2026-10-14"}
        assert window.list_params(bounded=True) == {"date_from": "2026-10-14", "date_to": "2026-10-20"}

    def test_label(self):
        assert Window(date(2026, 10, 21), date(2026, 10, 27)).label == "21 Oct–27 Oct"
        assert Window(date(2026, 10, 27), date(2026, 10, 27)).label == "27 Oct"


class TestBucketStarts:
    def test_days(self):
        starts = _bucket_starts(Window(date(2026, 10, 21), date(2026, 10, 27)), None)

        assert starts == [date(2026, 10, d) for d in range(21, 28)]

    def test_weeks_start_on_the_monday_of_the_first_day(self):
        starts = _bucket_starts(Window(date(2026, 9, 28), date(2026, 10, 27)), None)

        mondays = [date(2026, 9, 28), date(2026, 10, 5), date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26)]
        assert starts == mondays

    def test_all_time_months_start_at_the_first_record(self):
        starts = _bucket_starts(Window(None, date(2027, 1, 3)), date(2026, 11, 15))

        assert starts == [date(2026, 11, 1), date(2026, 12, 1), date(2027, 1, 1)]

    def test_all_time_with_no_records_has_no_buckets(self):
        assert _bucket_starts(Window(None, date(2026, 10, 27)), None) == []


class TestChange:
    def test_up(self):
        assert _change(174, 156) == {"direction": "up", "amount": "18", "was": "156"}

    def test_down_with_unit(self):
        assert _change(90, 92, unit=" pts") == {"direction": "down", "amount": "2 pts", "was": "92 pts"}

    def test_a_rate_changes_in_points_and_was_a_percentage(self):
        assert _change(72, 100, unit=" pts", was_unit="%") == {"direction": "down", "amount": "28 pts", "was": "100%"}

    def test_nothing_to_compare(self):
        assert _change(5, None) is None
        assert _change(None, 5) is None

    def test_signed_pct(self):
        assert _signed_pct(28, 22) == "+27%"
        assert _signed_pct(12, 15) == "−20%"
        assert _signed_pct(3, 0) == ""
        assert _signed_pct(7, 7) == ""
