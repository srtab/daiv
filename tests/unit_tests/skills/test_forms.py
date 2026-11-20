from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile

from skills.forms import SkillUploadForm
from skills.services import SkillPackage


def test_form_valid_with_good_zip(build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    uploaded = SimpleUploadedFile("demo.zip", data, content_type="application/zip")
    form = SkillUploadForm(data={"force": ""}, files={"zip": uploaded})
    assert form.is_valid(), form.errors
    assert isinstance(form.cleaned_data["package"], SkillPackage)
    assert form.cleaned_data["package"].name == "demo"


def test_form_invalid_with_bad_zip():
    uploaded = SimpleUploadedFile("bad.zip", b"not a zip", content_type="application/zip")
    form = SkillUploadForm(data={"force": ""}, files={"zip": uploaded})
    assert not form.is_valid()
    assert "zip" in form.errors


def test_force_field_is_optional_boolean(build_skill_zip):
    data = build_skill_zip(skill_name="demo")
    uploaded = SimpleUploadedFile("demo.zip", data, content_type="application/zip")
    form = SkillUploadForm(data={"force": "true"}, files={"zip": uploaded})
    assert form.is_valid()
    assert form.cleaned_data["force"] is True


def test_form_rejects_zip_over_max_size(build_skill_zip, monkeypatch):
    data = build_skill_zip(skill_name="demo")
    monkeypatch.setattr("skills.forms.MAX_ZIP_BYTES", len(data) - 1)
    uploaded = SimpleUploadedFile("demo.zip", data, content_type="application/zip")
    form = SkillUploadForm(data={"force": ""}, files={"zip": uploaded})
    assert not form.is_valid()
    # The form's own check, not SkillPackage.inspect's ("zip exceeds ...", code zip_too_large), rejects it.
    assert form.errors["zip"] == [f"Zip exceeds maximum size of {len(data) - 1} bytes."]
