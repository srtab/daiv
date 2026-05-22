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
