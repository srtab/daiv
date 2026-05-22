from django.apps import apps
from django.urls import reverse


def test_skills_app_is_registered():
    assert apps.is_installed("skills")
    assert apps.get_app_config("skills").verbose_name == "Skills"


def test_list_url_resolves():
    # URL conf exists even though the view will be a stub at this point.
    assert reverse("skills:list") == "/dashboard/skills/"
