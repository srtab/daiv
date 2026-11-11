import io
import zipfile

from django.urls import reverse

import pytest
from skills.services import SkillPackage, SkillStorage, skill_subagents

from core.models import Provider, SiteConfiguration

MODELS_URL = reverse("skills:models", args=["code-review"])
SKILL_FIELD = "agent_skill_models__code-review"
SECURITY_FIELD = "agent_subagent_models__cr-security"


@pytest.fixture
def usable_openai(db):
    provider = Provider.objects.get(slug="openai")
    provider.api_key = "k"
    provider.is_enabled = True
    provider.save()
    return provider


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr("skills.services.settings.CUSTOM_SKILLS_PATH", tmp_path / "custom")
    monkeypatch.setattr("skills.services.SKILLS_CACHE_PATH", tmp_path / "cache")
    (tmp_path / "custom").mkdir()
    (tmp_path / "cache").mkdir()
    return SkillStorage()


def _post(client, **fields):
    data = {SKILL_FIELD: "", **{f"agent_subagent_models__{s.name}": "" for s in skill_subagents("code-review")}}
    return client.post(MODELS_URL, {**data, **fields})


@pytest.mark.django_db
def test_the_tab_lists_the_skill_and_each_detector(admin_client):
    content = admin_client.get(MODELS_URL).content.decode()

    assert f'name="{SKILL_FIELD}"' in content
    for detector in skill_subagents("code-review"):
        assert f'name="agent_subagent_models__{detector.name}"' in content
    assert "Save models" in content


def test_the_detectors_are_read_from_the_code_review_charters():
    names = [detector.name for detector in skill_subagents("code-review")]

    assert "cr-security" in names
    assert all(detector.summary and not detector.summary.endswith(".") for detector in skill_subagents("code-review"))
    assert skill_subagents("plan") == ()


@pytest.mark.django_db
def test_saving_writes_the_entries_beside_the_others(admin_client, usable_openai):
    config = SiteConfiguration.objects.get_instance()
    config.agent_skill_models = {"plan": "openai:gpt-5.4-mini"}
    config.agent_subagent_models = {"explore": "openai:gpt-5.4-mini"}
    config.save()

    response = _post(admin_client, **{SKILL_FIELD: "openai:gpt-5.4", SECURITY_FIELD: "openai:gpt-5.4"})

    assert response.status_code == 302
    assert response.url == MODELS_URL
    config = SiteConfiguration.objects.get_instance()
    assert config.agent_skill_models == {"plan": "openai:gpt-5.4-mini", "code-review": "openai:gpt-5.4"}
    assert config.agent_subagent_models == {"explore": "openai:gpt-5.4-mini", "cr-security": "openai:gpt-5.4"}


@pytest.mark.django_db
def test_a_model_no_provider_serves_saves_nothing(admin_client):
    response = _post(admin_client, **{SKILL_FIELD: "nosuch:model"})

    assert response.status_code == 200
    assert "Unsupported model" in response.content.decode()
    assert SiteConfiguration.objects.get_instance().agent_skill_models == {}


@pytest.mark.django_db
def test_an_entry_whose_provider_went_away_is_flagged(admin_client, usable_openai):
    config = SiteConfiguration.objects.get_instance()
    config.agent_skill_models = {"code-review": "openai:gpt-5.4"}
    config.save()
    usable_openai.is_enabled = False
    usable_openai.save()

    content = admin_client.get(MODELS_URL).content.decode()

    assert "agent-pill--stale" in content
    assert "Runs use the default until you pick another model." in content


@pytest.mark.django_db
def test_the_skill_s_map_locked_by_its_variable_leaves_the_detectors_editable(admin_client, usable_openai, monkeypatch):
    from core import site_settings as ss_module

    monkeypatch.setenv("DAIV_AGENT_SKILL_MODELS", '{"code-review": "openai:gpt-5.4-mini"}')
    ss_module._docker_secret_cache.clear()

    content = admin_client.get(MODELS_URL).content.decode()
    _post(admin_client, **{SKILL_FIELD: "openai:gpt-5.4", SECURITY_FIELD: "openai:gpt-5.4"})

    assert "DAIV_AGENT_SKILL_MODELS" in content
    assert f'name="{SKILL_FIELD}"' not in content
    assert "Save models" in content
    config = SiteConfiguration.objects.get_instance()
    assert config.agent_skill_models == {}
    assert config.agent_subagent_models == {"cr-security": "openai:gpt-5.4"}


def _skill_zip(skill_md: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes/SKILL.md", skill_md)
    return buffer.getvalue()


@pytest.mark.django_db
def test_a_skill_without_detectors_names_the_model_its_skill_md_declares(admin_client, admin_user, storage):
    data = _skill_zip("---\nname: notes\ndescription: Takes notes\nmetadata:\n  model: zai:glm-5.2\n---\n\nBody.\n")
    storage.replace(SkillPackage.inspect(io.BytesIO(data)), uploaded_by=admin_user)

    response = admin_client.get(reverse("skills:models", args=["notes"]))

    content = response.content.decode()
    assert "Detectors" not in content
    assert "zai:glm-5.2" in content
    assert response.context["form"].fields["agent_skill_models__notes"].widget.placeholder == "glm-5.2"


@pytest.mark.django_db
def test_the_tab_is_for_admins_only(client, member_user):
    client.force_login(member_user)

    assert client.get(MODELS_URL).status_code == 403
