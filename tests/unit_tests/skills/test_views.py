from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest
from skills.models import GlobalSkill
from skills.services import SkillPackage, SkillStorage


@pytest.fixture
def storage(tmp_path, monkeypatch):
    custom = tmp_path / "custom"
    cache = tmp_path / "cache"
    custom.mkdir()
    cache.mkdir()
    monkeypatch.setattr("skills.services.agent_settings.CUSTOM_SKILLS_PATH", custom)
    monkeypatch.setattr("skills.services.SKILLS_CACHE_PATH", cache)
    return SkillStorage()


@pytest.mark.django_db
def test_list_renders_with_uploaded_skill(client, admin_user, storage, build_skill_zip):
    data = build_skill_zip(skill_name="demo", description="A demo skill")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(admin_user)

    resp = client.get(reverse("skills:list"))

    assert resp.status_code == 200
    assert b"demo" in resp.content
    assert b"A demo skill" in resp.content


@pytest.mark.django_db
def test_list_denies_member(client, member_user):
    client.force_login(member_user)
    resp = client.get(reverse("skills:list"))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_upload_get_returns_modal_partial(client, admin_user):
    client.force_login(admin_user)
    resp = client.get(reverse("skills:upload"))
    assert resp.status_code == 200
    # The partial should not extend base_app.html
    assert b"<form" in resp.content
    assert b"<aside" not in resp.content


@pytest.mark.django_db
def test_upload_post_happy_path(client, admin_user, storage, build_skill_zip):
    client.force_login(admin_user)
    data = build_skill_zip(skill_name="demo")
    uploaded = SimpleUploadedFile("demo.zip", data, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded})
    assert resp.status_code == 204
    assert "HX-Trigger" in resp.headers
    assert GlobalSkill.objects.filter(name="demo").exists()


@pytest.mark.django_db
def test_upload_post_validation_error_re_renders_modal(client, admin_user, storage):
    client.force_login(admin_user)
    uploaded = SimpleUploadedFile("bad.zip", b"not a zip", content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded})
    assert resp.status_code == 200  # re-rendered modal with error
    assert b"<form" in resp.content
