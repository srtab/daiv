from __future__ import annotations

import uuid

import pytest
from skills.models import GlobalSkill, SkillInvocation


@pytest.mark.django_db
def test_str_returns_name(admin_user):
    skill = GlobalSkill.objects.create(
        name="demo", description="d", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="x"
    )
    assert str(skill) == "demo"


@pytest.mark.django_db
def test_skill_invocation_persists_and_defaults_timestamps():
    inv = SkillInvocation.objects.create(
        name="code-review", source=SkillInvocation.Source.BUILTIN, repo_slug="org/repo", thread_id=uuid.uuid4()
    )
    assert inv.pk is not None
    assert inv.created is not None
    assert inv.modified is not None
    assert inv.source == "builtin"


@pytest.mark.django_db
def test_skill_invocation_source_choices():
    assert set(SkillInvocation.Source.values) == {"builtin", "global", "repo"}
