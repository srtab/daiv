from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount
from sessions.models import SessionOrigin, SpendRecord, SpendTask
from sessions.spend_ledger import SpendLedger

D = Decimal


def _spend(**fields) -> SpendRecord:
    fields.setdefault("trigger_type", SessionOrigin.CHAT)
    fields.setdefault("occurred_at", timezone.now())
    return SpendRecord.objects.create(model="m", **fields)


def _keyed(key: str) -> set[SpendRecord]:
    return set(SpendRecord.objects.filter(SpendLedger.person_filter(key)))


@pytest.mark.django_db
class TestPersonKeys:
    def test_each_kind_round_trips_through_its_filter(self, member_user):
        by_user = _spend(user=member_user, person_name=str(member_user))
        external = _spend(external_provider="gitlab", external_uid="77", external_username="ana")
        deleted = _spend(person_name="Old Name")
        daiv = _spend(task=SpendTask.MEMORY_CONSOLIDATION, trigger_type="")

        people = {record.pk: SpendLedger.person(record) for record in (by_user, external, deleted, daiv)}

        assert [(p.key, p.kind) for p in people.values()] == [
            (f"u{member_user.pk}", "user"),
            ("xgitlab:77", "external"),
            ("dOld Name", "deleted"),
            ("daiv", "daiv"),
        ]
        assert people[external.pk].name == "ana"
        assert people[deleted.pk].name == "Old Name"
        for record in (by_user, external, deleted, daiv):
            assert _keyed(people[record.pk].key) == {record}

    def test_an_external_key_never_matches_a_daiv_user_of_the_same_name(self, member_user):
        SocialAccount.objects.create(user=member_user, provider="gitlab", uid="77")
        _spend(user=member_user, external_provider="gitlab", external_uid="77")

        assert _keyed("xgitlab:77") == set()

    def test_a_deleted_users_rows_keep_their_name_once_the_user_is_gone(self, django_user_model):
        gone = django_user_model.objects.create_user(username="gone", email="g@x.io", name="Gone Person")
        record = _spend(user=gone, person_name=str(gone))
        gone.delete()
        record.refresh_from_db()

        person = SpendLedger.person(record)

        assert (person.key, person.name, person.kind) == ("dGone Person", "Gone Person", "deleted")

    def test_a_values_row_labels_a_user_by_name_then_username(self, member_user):
        _spend(user=member_user)

        [values] = SpendRecord.objects.values(*SpendLedger.PERSON_VALUES)

        assert SpendLedger.person(values).name == (member_user.name or member_user.username)

    @pytest.mark.parametrize("key", ["", "u", "uabc", "x", "xgitlab", "xgitlab:", "x:77", "d", "z1"])
    def test_anything_else_is_not_a_key(self, key):
        with pytest.raises(ValueError, match="person key"):
            SpendLedger.person_filter(key)


@pytest.mark.django_db
class TestRows:
    def test_rows_are_the_visible_records_in_the_days_oldest_first(self, member_user, other_user):
        today = timezone.localdate()
        now = timezone.now()
        later = _spend(user=member_user, occurred_at=now)
        earlier = _spend(user=member_user, occurred_at=now - timedelta(days=1))
        _spend(user=member_user, occurred_at=now - timedelta(days=5))
        _spend(user=other_user, repo_id="acme/secret", occurred_at=now)

        rows = SpendLedger.rows(member_user, "all", today - timedelta(days=1), today)

        assert list(rows) == [earlier, later]

    def test_the_mine_scope_keeps_only_what_the_user_started(self, admin_user, member_user):
        today = timezone.localdate()
        mine = _spend(user=admin_user)
        _spend(user=member_user)

        assert list(SpendLedger.rows(admin_user, "mine", today, today)) == [mine]
        assert len(SpendLedger.rows(admin_user, "all", today, today)) == 2


class TestCursor:
    def test_a_cursor_round_trips(self):
        now = timezone.now()

        assert SpendLedger.decode_cursor(SpendLedger.encode_cursor(now, 421)) == (now, 421)

    @pytest.mark.parametrize(
        "text", ["", "not base64!", "eyJ0IjoxfQ", "e30", "eyJ0IjoiMjAyNi0xMS0xNyIsImkiOiI0In0", "bnVsbA"]
    )
    def test_anything_else_is_rejected(self, text):
        with pytest.raises(ValueError, match="cursor"):
            SpendLedger.decode_cursor(text)


class TestCostSource:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"cost_usd": D("0.5"), "reported_cost_usd": D("0.5"), "cost_reported": True}, "reported"),
            ({"cost_usd": D(0), "reported_cost_usd": D(0), "cost_reported": True}, "reported"),
            ({"cost_usd": D("0.5"), "reported_cost_usd": D(0)}, "estimated"),
            ({"cost_usd": D("0.7"), "reported_cost_usd": D("0.5"), "cost_reported": True}, "mixed"),
            ({"cost_usd": D(0), "reported_cost_usd": D(0), "cost_reported": True, "unpriced_tokens": 9}, "mixed"),
            ({"cost_usd": D(0), "reported_cost_usd": D(0), "unpriced_tokens": 9}, "unpriced"),
            ({"cost_usd": D("0.5"), "reported_cost_usd": None}, "unknown"),
        ],
    )
    def test_the_source_of_a_records_cost(self, fields, expected):
        assert SpendLedger.cost_source(SpendRecord(model="m", **fields)) == expected
