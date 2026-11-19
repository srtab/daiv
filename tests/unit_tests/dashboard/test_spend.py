from decimal import Decimal

from django.utils import timezone

import pytest
from dashboard.insights import window_for
from dashboard.spend import CACHE_FAIR, CACHE_GOOD, SHOWN_ROWS, SpendReport, cache_tag
from sessions.models import RunStatus, SessionOrigin, SpendRecord, SpendTask

from tests.unit_tests.dashboard.factories import spend_record, spend_run, spend_session
from tests.unit_tests.sessions.conftest import grant_read_access

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def other_user(django_user_model):
    return django_user_model.objects.create_user(username="other", email="other@x.io")


def _report(user, period="30d", scope="all") -> SpendReport:
    window = window_for(period)
    return SpendReport(user, window, window.previous(), scope)


class TestTotals:
    def test_every_figure_is_the_sum_of_the_period(self, admin_user, member_user):
        changed = spend_run(spend_session(member_user), code_changes=True)
        plain = spend_run(spend_session(admin_user), status=RunStatus.FAILED)
        spend_record(
            changed,
            cost="3",
            input_tokens=1_000,
            cache_read_tokens=900,
            cache_write_tokens=50,
            cache_reported=True,
            output_tokens=100,
            reasoning_tokens=40,
            cache_saving_usd=D("0.5"),
            reported_cost_usd=D("1"),
        )
        spend_record(
            plain, cost="1", input_tokens=500, cache_read_tokens=0, cache_reported=True, reported_cost_usd=D("0")
        )
        spend_record(
            None,
            task=SpendTask.MEMORY_CONSOLIDATION,
            repo_id="acme/api",
            cost="2",
            input_tokens=100,
            reported_cost_usd=D("0"),
        )
        spend_record(plain, days_ago=40, cost="9", model="older")

        totals = _report(admin_user).totals()

        assert totals["cost"] == D("6")
        assert totals["tokens"] == 1_100 + 510 + 110
        assert totals["hit_rate"] == round(900 / 1_500 * 100)
        assert totals["cache_saving"] == D("0.5")
        assert totals["runs"] == 2
        assert totals["avg_per_run"] == D("2")
        assert totals["avg_per_changed_run"] == D("3")
        tokens = {part["key"]: part["value"] for part in totals["token_split"]["parts"]}
        assert tokens == {"cache_read": "900", "uncached_input": "650", "cache_write": "50", "output": "120"}
        cost = {part["key"]: part["value"] for part in totals["cost_split"]["parts"]}
        assert cost == {"estimated": "$5.00", "reported": "$1.00"}

    def test_changes_compare_with_the_period_before_and_say_which_way_is_better(self, admin_user):
        run = spend_run(spend_session(admin_user))
        spend_record(run, cost="4", cache_reported=True, cache_read_tokens=90)
        spend_record(
            spend_run(spend_session(admin_user), days_ago=40),
            days_ago=40,
            cost="2",
            cache_reported=True,
            cache_read_tokens=50,
        )

        totals = _report(admin_user).totals()

        assert (totals["cost_change"]["amount"], totals["cost_change"]["tone"]) == ("100%", "bad")
        assert totals["cost_change"]["was"] == "$2.00"
        assert (totals["hit_rate_change"]["amount"], totals["hit_rate_change"]["tone"]) == ("40 pts", "good")
        assert totals["runs_change"]["tone"] == "neutral"

    def test_all_time_has_no_changes(self, admin_user):
        spend_record(spend_run(spend_session(admin_user)))

        totals = _report(admin_user, "all").totals()

        assert totals["cost_change"] is None and totals["hit_rate_change"] is None

    def test_a_hit_rate_no_record_reported_is_not_reported_and_a_saving_left_unpriced_is_a_floor(self, admin_user):
        spend_record(spend_run(spend_session(admin_user)), cache_read_tokens=10, cache_unpriced_tokens=10)

        totals = _report(admin_user).totals()

        assert totals["hit_rate"] is None
        assert totals["cache_saving_floor"] is True

    def test_backfilled_spend_shows_its_source_as_unknown(self, admin_user):
        spend_record(spend_run(spend_session(admin_user)), cost="2", reported_cost_usd=None)

        cost = {part["key"]: part["value"] for part in _report(admin_user).totals()["cost_split"]["parts"]}

        assert cost == {"estimated": "$0.00", "reported": "$0.00", "unknown": "$2.00"}

    def test_an_empty_period_is_empty(self, admin_user):
        assert _report(admin_user).totals()["empty"] is True


class TestChart:
    def test_columns_add_up_and_fold_past_five_models_into_other(self, admin_user):
        run = spend_run(spend_session(admin_user))
        for index, model in enumerate(["m1", "m2", "m3", "m4", "m5", "m6", "m7"]):
            spend_record(run, model=model, cost=str(10 - index), days_ago=index * 3)

        report = _report(admin_user)
        chart = report.chart()

        assert sum(column["total"] for column in chart["columns"]) == report.totals()["cost"]
        assert [item["label"] for item in chart["legend"]] == ["m1", "m2", "m3", "m4", "m5", "Other"]
        assert chart["legend"][-1]["count"] == D("5") + D("4")

    def test_each_hover_card_adds_up_to_its_column_and_opens_on_the_side_with_room(self, admin_user):
        run = spend_run(spend_session(admin_user))
        spend_record(run, model="a", cost="2", cache_reported=True, cache_read_tokens=80)
        spend_record(run, model="b", cost="6", provider="openrouter")
        spend_record(spend_run(spend_session(admin_user), days_ago=28), model="a", cost="1", days_ago=28)

        columns = _report(admin_user).chart()["columns"]
        cards = [(i, column["card"]) for i, column in enumerate(columns) if "card" in column]

        last_index, last = cards[-1]
        assert [(line["name"], line["cost"], line["share"]) for line in last["lines"]] == [
            ("b", "$6.00", "75%"),
            ("a", "$2.00", "25%"),
        ]
        assert (last["runs"], last["hit_rate"], last["side"]) == (1, 80, "right")
        assert cards[0][1]["side"] == "left"
        assert last_index == len(columns) - 1

    def test_today_has_no_chart(self, admin_user):
        spend_record(spend_run(spend_session(admin_user)))

        assert _report(admin_user, "today").chart() is None


class TestBreakdowns:
    @staticmethod
    def _sum(table) -> Decimal:
        rows = [row for row in table["rows"] if not row.get("hidden")]
        total = sum(row["cost"] for row in rows)
        if table["rest"]:
            total += D(table["rest"]["cost_text"].strip("$+"))
        return total

    def test_every_breakdown_adds_up_to_the_total(self, admin_user, member_user, other_user):
        for index, (user, repo) in enumerate([(admin_user, "a/1"), (member_user, "a/2"), (other_user, "a/3")] * 3):
            run = spend_run(
                spend_session(user, repo_id=f"{repo}{index}"),
                trigger=[SessionOrigin.CHAT, SessionOrigin.SCHEDULE][index % 2],
            )
            spend_record(run, model=f"m{index}", provider=["anthropic", "openrouter"][index % 2], cost=str(index + 1))
        spend_record(None, task=SpendTask.MEMORY_CONSOLIDATION, repo_id="a/1", cost="5")
        spend_record(None, task=SpendTask.TITLE, user=member_user, cost="0.5")

        report = _report(admin_user)
        total = report.totals()["cost"]

        for table in report.breakdowns():
            if table["key"] == "started_by":
                assert sum(row["cost"] for row in table["rows"]) == total
            else:
                assert self._sum(table) == total, table["key"]

    def test_started_by_lists_triggers_then_background_by_task_with_no_link_for_consolidation(self, admin_user):
        spend_record(spend_run(spend_session(admin_user)), cost="3")
        spend_record(None, task=SpendTask.MEMORY_CONSOLIDATION, repo_id="acme/api", cost="1")
        spend_record(None, task=SpendTask.RUN_CLASSIFICATION, repo_id="acme/api", cost="2")

        table = next(t for t in _report(admin_user).breakdowns() if t["key"] == "started_by")

        assert [row["name"] for row in table["rows"]] == ["Chat", "Background"]
        background = table["rows"][-1]
        assert (background["cost"], background["url"]) == (D("3"), "")
        assert [(sub["name"], bool(sub["url"]), bool(sub["why"])) for sub in background["subs"]] == [
            ("Run classification", True, False),
            ("Memory consolidation", False, True),
        ]
        assert table["foldable"] is False

    def test_rows_without_a_list_to_open_say_why(self, admin_user):
        spend_record(None, task=SpendTask.MEMORY_CONSOLIDATION, repo_id="acme/api", provider="")
        spend_record(None, task=SpendTask.TITLE, user=admin_user)

        tables = {table["key"]: table for table in _report(admin_user).breakdowns()}

        people = {row["name"]: row for row in tables["person"]["rows"]}
        assert (people["DAIV"]["url"], bool(people["DAIV"]["why"])) == ("", True)
        assert "spend_person=u" in people[admin_user.name or admin_user.username]["url"]
        repos = {row["name"]: row for row in tables["repository"]["rows"]}
        assert (repos["No repository"]["url"], bool(repos["No repository"]["why"])) == ("", True)
        providers = {row["name"]: row for row in tables["provider"]["rows"]}
        assert (providers["Unknown provider"]["url"], bool(providers["Unknown provider"]["why"])) == ("", True)

    def test_a_row_links_to_its_sessions_for_the_period(self, admin_user):
        spend_record(spend_run(spend_session(admin_user, repo_id="acme/web")))
        window = window_for("30d")

        repo = next(t for t in _report(admin_user).breakdowns() if t["key"] == "repository")["rows"][0]

        assert repo["url"] == (
            f"/dashboard/sessions/?scope=all&spend_to={window.end.isoformat()}"
            f"&spend_from={window.start.isoformat()}&spend_repo=acme%2Fweb"
        )

    def test_past_five_rows_a_rest_row_carries_the_remainder(self, admin_user):
        for index in range(7):
            spend_record(spend_run(spend_session(admin_user, repo_id=f"r/{index}")), cost=str(10 - index))

        table = next(t for t in _report(admin_user).breakdowns() if t["key"] == "repository")

        assert [row["hidden"] for row in table["rows"]] == [False] * 5 + [True] * 2
        assert table["rest"]["name"] == "Rest (2 repositories)"
        assert table["rest"]["cost_text"] == "$9.00"
        assert (table["foldable"], table["not_shown"]) == (True, 0)

    def test_past_a_hundred_rows_the_rest_are_counted_not_listed(self, admin_user):
        run = spend_run(spend_session(admin_user))
        SpendRecord.objects.bulk_create([
            SpendRecord(
                run=run,
                occurred_at=timezone.now(),
                trigger_type=SessionOrigin.CHAT,
                model=f"m{index:03}",
                cost_usd=D(1000 - index),
                total_tokens=1,
            )
            for index in range(SHOWN_ROWS + 3)
        ])

        table = next(t for t in _report(admin_user).breakdowns() if t["key"] == "model")

        assert (len(table["rows"]), table["groups"], table["not_shown"]) == (SHOWN_ROWS, SHOWN_ROWS + 3, 3)

    def test_an_unpriced_model_never_shows_as_free(self, admin_user):
        run = spend_run(spend_session(admin_user))
        spend_record(run, model="local", cost="0", unpriced_tokens=110)
        spend_record(run, model="mixed", cost="1", unpriced_tokens=5)

        rows = {
            row["name"]: row for row in next(t for t in _report(admin_user).breakdowns() if t["key"] == "model")["rows"]
        }

        assert (rows["local"]["cost_text"], rows["mixed"]["cost_text"]) == ("no price", "$1.00+")

    @pytest.mark.parametrize(
        ("rate", "tone"),
        [
            (CACHE_GOOD, "good"),
            (CACHE_GOOD - 1, "fair"),
            (CACHE_FAIR, "fair"),
            (CACHE_FAIR - 1, "attention"),
            (None, "unreported"),
        ],
    )
    def test_cache_tags_follow_the_thresholds(self, rate, tone):
        assert cache_tag(rate) == tone

    def test_a_row_whose_records_reported_no_cache_says_so(self, admin_user):
        spend_record(spend_run(spend_session(admin_user)), cache_read_tokens=0)

        row = next(t for t in _report(admin_user).breakdowns() if t["key"] == "model")["rows"][0]

        assert (row["hit_rate"], row["cache"]) == (None, "unreported")


class TestTopSessions:
    def test_the_ten_costliest_sessions_by_run_spend_in_the_period(self, admin_user):
        sessions = []
        for index in range(12):
            session = spend_session(admin_user, title=f"s{index}")
            spend_record(spend_run(session), cost=str(index + 1), model="a")
            spend_record(spend_run(session), cost="0.5", model="b")
            sessions.append(session)
        spend_record(None, task=SpendTask.TITLE, session=sessions[0], cost="100")
        spend_record(spend_run(sessions[1], days_ago=40), days_ago=40, cost="100")

        top = _report(admin_user).top_sessions()

        assert [row["title"] for row in top] == [f"s{index}" for index in range(11, 1, -1)]
        assert (top[0]["runs"], top[0]["models"], top[0]["cost_text"]) == (2, "a, b", "$12.50")

    def test_only_visible_sessions(self, member_user, other_user):
        grant_read_access(member_user, "acme/api")
        spend_record(spend_run(spend_session(other_user, repo_id="acme/api", title="shared")))
        spend_record(
            spend_run(spend_session(other_user, repo_id="acme/secret", title="private")), repo_id="acme/secret"
        )

        assert [row["title"] for row in _report(member_user).top_sessions()] == ["shared"]

    def test_the_sort_link_carries_the_period(self, admin_user):
        window = window_for("7d")

        url = SpendReport(admin_user, window, None, "all").sessions_by_cost_url()

        assert url == (
            f"/dashboard/sessions/?scope=all&spend_to={window.end.isoformat()}"
            f"&spend_from={window.start.isoformat()}&sort=cost"
        )

    def test_a_mine_report_links_to_the_viewers_own_sessions(self, member_user):
        spend_record(spend_run(spend_session(member_user, repo_id="acme/web")))
        report = SpendReport(member_user, window_for("30d"), None, "mine")

        repo = next(t for t in report.breakdowns() if t["key"] == "repository")["rows"][0]

        assert "scope=mine" in repo["url"]
        assert "scope=mine" in report.sessions_by_cost_url()
        assert all("scope=mine" in column["url"] for column in report.chart()["columns"])


class TestCoverage:
    def test_names_unpriced_models_and_counts_runs_with_no_spend_and_runs_still_running(self, admin_user):
        session = spend_session(admin_user)
        spend_record(spend_run(session), model="local", cost="0", unpriced_tokens=10)
        spend_run(session, status=RunStatus.FAILED)
        spend_record(spend_run(session, status=RunStatus.RUNNING))
        spend_run(session, status=RunStatus.QUEUED)

        coverage = _report(admin_user).coverage()

        assert coverage == {"unpriced_models": ["local"], "unrecorded_runs": 1, "running_runs": 1}

    def test_the_whole_report_runs_in_eleven_queries(self, admin_user, django_assert_max_num_queries):
        for index in range(8):
            run = spend_run(spend_session(admin_user, repo_id=f"r/{index}"), days_ago=index)
            spend_record(run, model=f"m{index}", days_ago=index)
        spend_record(None, task=SpendTask.TITLE, user=admin_user)

        with django_assert_max_num_queries(11):
            report = _report(admin_user)
            report.totals()
            report.chart()
            report.breakdowns()
            report.top_sessions()
            report.coverage()


class TestBackgroundSpend:
    def test_background_spend_counts_for_its_person_and_task_and_leaves_the_session_page_alone(
        self, admin_user, member_user
    ):
        from sessions.spend import build_session_spend

        session = spend_session(member_user)
        run = spend_run(session)
        spend_record(run, cost="4")
        spend_record(None, task=SpendTask.TITLE, session=session, user=member_user, repo_id="acme/api", cost="1")
        spend_record(run, task=SpendTask.RUN_CLASSIFICATION, trigger_type="", cost="2", model="haiku")
        spend_record(None, task=SpendTask.MEMORY_CONSOLIDATION, repo_id="acme/api", cost="3")
        before = build_session_spend(session.runs.all())

        tables = {table["key"]: table for table in _report(admin_user).breakdowns()}

        people = {row["name"]: row["cost"] for row in tables["person"]["rows"]}
        assert people == {member_user.name or member_user.username: D("7"), "DAIV": D("3")}
        background = tables["started_by"]["rows"][-1]
        assert {sub["name"]: sub["cost_text"] for sub in background["subs"]} == {
            "Memory consolidation": "$3.00",
            "Run classification": "$2.00",
            "Session titles": "$1.00",
        }
        assert build_session_spend(session.runs.all()) == before
