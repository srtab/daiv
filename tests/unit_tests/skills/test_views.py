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


@pytest.mark.django_db
def test_upload_collision_re_renders_confirm_partial(client, admin_user, storage, build_skill_zip):
    client.force_login(admin_user)
    data_v1 = build_skill_zip(skill_name="demo", description="v1")
    storage.replace(SkillPackage.inspect(io.BytesIO(data_v1)), uploaded_by=admin_user)

    data_v2 = build_skill_zip(skill_name="demo", description="v2")
    uploaded = SimpleUploadedFile("demo.zip", data_v2, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded, "force": ""})

    assert resp.status_code == 200
    assert b"already exists" in resp.content.lower() or b"replace" in resp.content.lower()
    # The existing skill is not yet replaced
    assert GlobalSkill.objects.get(name="demo").description == "v1"


@pytest.mark.django_db
def test_upload_with_force_overwrites(client, admin_user, storage, build_skill_zip):
    client.force_login(admin_user)
    data_v1 = build_skill_zip(skill_name="demo", description="v1")
    storage.replace(SkillPackage.inspect(io.BytesIO(data_v1)), uploaded_by=admin_user)

    data_v2 = build_skill_zip(skill_name="demo", description="v2")
    uploaded = SimpleUploadedFile("demo.zip", data_v2, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded, "force": "true"})

    assert resp.status_code == 204
    assert GlobalSkill.objects.get(name="demo").description == "v2"


@pytest.mark.django_db
def test_delete_get_returns_confirm_partial(client, admin_user, storage, build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(admin_user)

    resp = client.get(reverse("skills:delete", args=["demo"]))
    assert resp.status_code == 200
    assert b"demo" in resp.content
    assert b"<form" in resp.content


@pytest.mark.django_db
def test_delete_post_removes_skill(client, admin_user, storage, build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(admin_user)

    resp = client.post(reverse("skills:delete", args=["demo"]))
    assert resp.status_code == 204
    assert "HX-Trigger" in resp.headers
    assert not GlobalSkill.objects.filter(name="demo").exists()


@pytest.mark.django_db
def test_delete_post_unknown_name_returns_404(client, admin_user, storage):
    client.force_login(admin_user)
    resp = client.post(reverse("skills:delete", args=["does-not-exist"]))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_delete_post_member_denied(client, member_user, storage, admin_user, build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(member_user)
    resp = client.post(reverse("skills:delete", args=["demo"]))
    assert resp.status_code == 403
