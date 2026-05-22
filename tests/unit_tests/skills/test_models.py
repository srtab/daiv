from __future__ import annotations

import pytest
from skills.models import GlobalSkill


@pytest.mark.django_db
def test_str_returns_name(admin_user):
    skill = GlobalSkill.objects.create(
        name="demo", description="d", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="x"
    )
    assert str(skill) == "demo"
