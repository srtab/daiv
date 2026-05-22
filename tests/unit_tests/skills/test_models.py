from __future__ import annotations

from django.db import IntegrityError, transaction

import pytest
from skills.models import GlobalSkill


@pytest.mark.django_db
def test_str_returns_name(admin_user):
    skill = GlobalSkill.objects.create(
        name="demo", description="d", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="x"
    )
    assert str(skill) == "demo"


@pytest.mark.django_db
def test_name_is_unique(admin_user):
    GlobalSkill.objects.create(
        name="demo", description="d", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="x"
    )
    with transaction.atomic(), pytest.raises(IntegrityError):
        GlobalSkill.objects.create(
            name="demo", description="d2", uploaded_by=admin_user, size_bytes=2, file_count=1, checksum="y"
        )


@pytest.mark.django_db
def test_ordering_is_by_name(admin_user):
    GlobalSkill.objects.create(
        name="zebra", description="d", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="a"
    )
    GlobalSkill.objects.create(
        name="alpha", description="d", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="b"
    )
    names = list(GlobalSkill.objects.values_list("name", flat=True))
    assert names == ["alpha", "zebra"]


@pytest.mark.django_db
def test_user_deletion_nulls_uploaded_by(admin_user):
    skill = GlobalSkill.objects.create(
        name="demo", description="d", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="x"
    )
    admin_user.delete()
    skill.refresh_from_db()
    assert skill.uploaded_by is None
