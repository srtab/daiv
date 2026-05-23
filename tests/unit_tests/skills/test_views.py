from __future__ import annotations

import io
import uuid
from unittest.mock import patch

from django.contrib.messages import get_messages
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
def test_upload_get_renders_full_page(client, admin_user):
    client.force_login(admin_user)
    resp = client.get(reverse("skills:upload"))
    assert resp.status_code == 200
    # Full-page render via base_app.html includes the breadcrumb nav and a form.
    assert b"app-breadcrumb" in resp.content
    assert b"<form" in resp.content
    # No modal overlay scaffolding remains.
    assert b"close-skills-modal" not in resp.content


@pytest.mark.django_db
def test_upload_post_happy_path(client, admin_user, storage, build_skill_zip):
    client.force_login(admin_user)
    data = build_skill_zip(skill_name="demo")
    uploaded = SimpleUploadedFile("demo.zip", data, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded})
    assert resp.status_code == 302
    assert resp.url == reverse("skills:list")
    assert GlobalSkill.objects.filter(name="demo").exists()
    msgs = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("demo" in m for m in msgs)


@pytest.mark.django_db
def test_upload_post_validation_error_re_renders_full_page(client, admin_user, storage):
    client.force_login(admin_user)
    uploaded = SimpleUploadedFile("bad.zip", b"not a zip", content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded})
    assert resp.status_code == 200
    assert b"app-breadcrumb" in resp.content
    assert b"<form" in resp.content


@pytest.mark.django_db
def test_upload_collision_re_renders_with_conflict_banner(client, admin_user, storage, build_skill_zip):
    client.force_login(admin_user)
    data_v1 = build_skill_zip(skill_name="demo", description="v1")
    storage.replace(SkillPackage.inspect(io.BytesIO(data_v1)), uploaded_by=admin_user)

    data_v2 = build_skill_zip(skill_name="demo", description="v2")
    uploaded = SimpleUploadedFile("demo.zip", data_v2, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded, "force": ""})

    assert resp.status_code == 200
    assert b"already exists" in resp.content.lower() or b"replace" in resp.content.lower()
    # The hidden force input flips to true so the resubmit overwrites.
    assert b'name="force" value="true"' in resp.content
    # The existing skill is not yet replaced.
    assert GlobalSkill.objects.get(name="demo").description == "v1"


@pytest.mark.django_db
def test_upload_with_force_overwrites(client, admin_user, storage, build_skill_zip):
    client.force_login(admin_user)
    data_v1 = build_skill_zip(skill_name="demo", description="v1")
    storage.replace(SkillPackage.inspect(io.BytesIO(data_v1)), uploaded_by=admin_user)

    data_v2 = build_skill_zip(skill_name="demo", description="v2")
    uploaded = SimpleUploadedFile("demo.zip", data_v2, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded, "force": "true"})

    assert resp.status_code == 302
    assert resp.url == reverse("skills:list")
    assert GlobalSkill.objects.get(name="demo").description == "v2"


@pytest.mark.django_db
def test_delete_get_renders_full_page(client, admin_user, storage, build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(admin_user)

    resp = client.get(reverse("skills:delete", args=["demo"]))
    assert resp.status_code == 200
    assert b"app-breadcrumb" in resp.content
    assert b"demo" in resp.content
    assert b"<form" in resp.content


@pytest.mark.django_db
def test_delete_post_removes_skill(client, admin_user, storage, build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(admin_user)

    resp = client.post(reverse("skills:delete", args=["demo"]))
    assert resp.status_code == 302
    assert resp.url == reverse("skills:list")
    assert not GlobalSkill.objects.filter(name="demo").exists()
    msgs = [str(m) for m in get_messages(resp.wsgi_request)]
    assert any("demo" in m for m in msgs)


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


@pytest.mark.django_db
def test_upload_get_denies_member(client, member_user, storage):
    client.force_login(member_user)
    resp = client.get(reverse("skills:upload"))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_download_denies_member(client, member_user, storage, admin_user, build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(member_user)
    resp = client.get(reverse("skills:download", args=["demo"]))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_upload_collision_when_dir_present_but_row_missing(client, admin_user, storage, build_skill_zip):
    """Orphan-on-disk: directory exists without a DB row → still trigger conflict banner."""
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    GlobalSkill.objects.filter(name="demo").delete()  # leave the directory orphaned
    assert (storage.root / "demo").exists()

    client.force_login(admin_user)
    uploaded = SimpleUploadedFile("demo.zip", data, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded, "force": ""})

    assert resp.status_code == 200
    assert b"already exists" in resp.content.lower() or b"replace" in resp.content.lower()


@pytest.mark.django_db
def test_detail_shows_skill_body_and_tree(client, admin_user, storage, build_skill_zip):
    data = build_skill_zip(
        skill_name="demo",
        description="A demo skill",
        skill_md_extra_body="Hello body.",
        extra_files={"scripts/foo.py": b"print('hi')\n"},
    )
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(admin_user)

    resp = client.get(reverse("skills:detail", args=["demo"]))
    assert resp.status_code == 200
    assert b"demo" in resp.content
    assert b"A demo skill" in resp.content
    assert b"Hello body" in resp.content
    assert b"scripts/foo.py" in resp.content


@pytest.mark.django_db
def test_detail_404_for_unknown_name(client, admin_user, storage):
    client.force_login(admin_user)
    resp = client.get(reverse("skills:detail", args=["nope"]))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_download_serves_original_zip(client, admin_user, storage, build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(admin_user)

    resp = client.get(reverse("skills:download", args=["demo"]))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("application/zip")
    assert "attachment" in resp["Content-Disposition"]
    assert "demo.zip" in resp["Content-Disposition"]
    body = b"".join(resp.streaming_content)
    assert body == data


@pytest.mark.django_db
def test_download_404_when_zip_missing(client, admin_user, storage):
    # Create a row but no zip on disk to simulate hand-edit drift
    GlobalSkill.objects.create(
        name="orphan", description="", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="x"
    )
    client.force_login(admin_user)
    resp = client.get(reverse("skills:download", args=["orphan"]))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_upload_refuses_builtin_name(client, admin_user, storage, build_skill_zip, monkeypatch):
    """Uploading a skill whose name collides with a built-in must be rejected with an inline error."""
    monkeypatch.setattr("skills.services.BUILTIN_SKILL_NAMES", frozenset({"demo"}))
    client.force_login(admin_user)
    data = build_skill_zip(skill_name="demo")
    uploaded = SimpleUploadedFile("demo.zip", data, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded})
    assert resp.status_code == 200
    assert b"built-in" in resp.content.lower()
    assert not GlobalSkill.objects.filter(name="demo").exists()


@pytest.mark.django_db
def test_detail_404_when_skill_md_not_utf8(client, admin_user, storage, build_skill_zip):
    """Hand-edited non-UTF-8 SKILL.md on disk must return 404, not 500."""
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    (storage.root / "demo" / "SKILL.md").write_bytes(b"\xff\xfe not utf-8")
    client.force_login(admin_user)
    resp = client.get(reverse("skills:detail", args=["demo"]))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_detail_404_when_disk_missing(client, admin_user, storage):
    """If the DB row exists but the on-disk tree is gone, return 404 (not 500)."""
    GlobalSkill.objects.create(
        name="orphan", description="", uploaded_by=admin_user, size_bytes=1, file_count=1, checksum="x"
    )
    client.force_login(admin_user)
    resp = client.get(reverse("skills:detail", args=["orphan"]))
    assert resp.status_code == 404


class _SqlCounter:
    def __init__(self, table: str):
        self.table = table
        self.matched = 0

    def __call__(self, execute, sql, params, many, context):
        if self.table in sql:
            self.matched += 1
        return execute(sql, params, many, context)


@pytest.mark.django_db
def test_list_view_annotates_invocations_count(admin_client):
    from skills.models import SkillInvocation

    custom = GlobalSkill.objects.create(name="custom-one", description="x", size_bytes=10, file_count=1, checksum="c")
    # 2 invocations for the custom skill, 3 for a built-in named "plan".
    for _ in range(2):
        SkillInvocation.objects.create(
            name=custom.name, source=SkillInvocation.Source.GLOBAL, repo_slug="r", thread_id=uuid.uuid4()
        )
    for _ in range(3):
        SkillInvocation.objects.create(
            name="plan", source=SkillInvocation.Source.BUILTIN, repo_slug="r", thread_id=uuid.uuid4()
        )

    response = admin_client.get(reverse("skills:list"))
    assert response.status_code == 200

    custom_rows = response.context["custom_skills"]
    builtin_rows = response.context["builtin_skills"]

    assert next(s for s in custom_rows if s.name == "custom-one").invocations_count == 2
    assert next(s for s in builtin_rows if s["name"] == "plan")["invocations_count"] == 3


@pytest.mark.django_db
def test_list_view_aggregate_runs_once(admin_client):
    from django.db import connection

    from skills.models import SkillInvocation  # noqa: F401 (ensure table exists)

    GlobalSkill.objects.create(name="a", description="x", size_bytes=1, file_count=1, checksum="c")
    GlobalSkill.objects.create(name="b", description="x", size_bytes=1, file_count=1, checksum="c")

    # One aggregate query for invocations, on top of the existing list queries.
    _count_invocation_queries = _SqlCounter("skills_skillinvocation")
    with connection.execute_wrapper(_count_invocation_queries):
        admin_client.get(reverse("skills:list"))
    assert _count_invocation_queries.matched == 1


@pytest.mark.django_db
def test_upload_post_surfaces_storage_oserror(client, admin_user, storage, build_skill_zip):
    """A disk failure inside SkillStorage.replace must re-render the form with an inline error, not 500."""
    client.force_login(admin_user)
    data = build_skill_zip(skill_name="demo")
    uploaded = SimpleUploadedFile("demo.zip", data, content_type="application/zip")
    with patch("skills.views.SkillStorage.replace", side_effect=OSError("disk full")):
        resp = client.post(reverse("skills:upload"), data={"zip": uploaded})
    assert resp.status_code == 200
    assert b"<form" in resp.content
    assert b"Could not save the skill" in resp.content


@pytest.mark.django_db
def test_delete_post_surfaces_storage_oserror(client, admin_user, storage, build_skill_zip):
    """A disk failure inside SkillStorage.delete must re-render the confirm page with an inline error."""
    data = build_skill_zip(skill_name="demo")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)
    client.force_login(admin_user)
    with patch("skills.views.SkillStorage.delete", side_effect=OSError("disk failure")):
        resp = client.post(reverse("skills:delete", args=["demo"]))
    assert resp.status_code == 500
    assert b"Could not delete the skill" in resp.content
    # Row still present because delete failed.
    assert GlobalSkill.objects.filter(name="demo").exists()


@pytest.mark.django_db
def test_full_admin_journey(client, admin_user, storage, build_skill_zip):
    """Upload → list → detail → download → delete."""
    client.force_login(admin_user)

    data = build_skill_zip(skill_name="journey", description="end to end", extra_files={"scripts/foo.py": b"x"})
    uploaded = SimpleUploadedFile("journey.zip", data, content_type="application/zip")
    resp = client.post(reverse("skills:upload"), data={"zip": uploaded})
    assert resp.status_code == 302
    assert resp.url == reverse("skills:list")

    resp = client.get(reverse("skills:list"))
    assert resp.status_code == 200
    assert b"journey" in resp.content
    assert b"end to end" in resp.content

    resp = client.get(reverse("skills:detail", args=["journey"]))
    assert resp.status_code == 200

    resp = client.get(reverse("skills:download", args=["journey"]))
    assert resp.status_code == 200

    resp = client.post(reverse("skills:delete", args=["journey"]))
    assert resp.status_code == 302
    assert resp.url == reverse("skills:list")
    assert not GlobalSkill.objects.filter(name="journey").exists()
