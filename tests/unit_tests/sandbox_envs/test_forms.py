import json
import logging

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

import pytest
from sandbox_envs.forms import SandboxEnvironmentForm
from sandbox_envs.models import SandboxEnvironment, Scope

User = get_user_model()


@pytest.fixture
def user(db):
    from accounts.models import User as AccountsUser

    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    return AccountsUser.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106


@pytest.mark.django_db
def test_user_form_rejects_global_scope_for_non_admin():
    from accounts.models import User

    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={"name": "dev", "base_image": "alpine", "scope": Scope.GLOBAL}, user=user, is_admin=False
    )
    assert not form.is_valid()
    assert "scope" in form.errors


@pytest.mark.django_db
def test_form_validates_env_vars_shape():
    from accounts.models import User

    user = User.objects.create_user(username="u2", email="u2@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "env_vars_json": '[{"name": "bad name", "value": "v", "is_secret": false}]',
        },
        user=user,
        is_admin=False,
    )
    assert not form.is_valid()
    assert "env_vars" in form.errors or "env_vars_json" in form.errors or "__all__" in form.errors


@pytest.mark.django_db
def test_form_preserves_unchanged_secret_on_edit(db):
    from accounts.models import User

    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[{"name": "TOKEN", "value": "real-secret", "is_secret": True}],
    )
    form = SandboxEnvironmentForm(
        instance=env,
        data={
            "name": "dev",
            "base_image": "alpine:latest",
            "scope": Scope.USER,
            "env_vars_json": '[{"name": "TOKEN", "value": "", "is_secret": true}]',
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    saved = form.save()
    assert saved.env_vars == [{"name": "TOKEN", "value": "real-secret", "is_secret": True}]


@pytest.mark.django_db
def test_form_refuses_when_existing_secrets_cannot_be_decrypted(db):
    """If the stored ciphertext is unreadable, the form must fail validation
    rather than silently persist ``"******"`` (or empty) as the new secret value."""
    from accounts.models import User

    user = User.objects.create_user(username="u", email="u@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        env_vars=[{"name": "TOKEN", "value": "real-secret", "is_secret": True}],
    )
    # Corrupt the ciphertext to simulate key rotation / DB tamper.
    env._env_vars_encrypted = "not-a-fernet-token"
    env.save(update_fields=["_env_vars_encrypted"])

    form = SandboxEnvironmentForm(
        instance=env,
        data={
            "name": "dev",
            "base_image": "alpine:latest",
            "scope": Scope.USER,
            "env_vars_json": '[{"name": "TOKEN", "value": "", "is_secret": true}]',
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid() is False
    # Original ciphertext is unchanged.
    env.refresh_from_db()
    assert env._env_vars_encrypted == "not-a-fernet-token"


@pytest.mark.django_db
def test_form_memory_value_and_unit_set_memory_bytes():
    from accounts.models import User

    user = User.objects.create_user(username="m1", email="m1@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_value": "1",
            "memory_unit": "GiB",
            "network_choice": "default",
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    assert env.memory_bytes == 2**30


@pytest.mark.django_db
def test_form_empty_memory_value_maps_to_none():
    from accounts.models import User

    user = User.objects.create_user(username="m2", email="m2@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": "default",
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    assert env.memory_bytes is None


@pytest.mark.django_db
@pytest.mark.parametrize(("choice", "expected"), [("default", None), ("on", True), ("off", False)])
def test_form_network_choice_maps_to_network_enabled(choice, expected):
    from accounts.models import User

    user = User.objects.create_user(username=f"n-{choice}", email=f"n-{choice}@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": choice,
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    assert env.network_enabled is expected


@pytest.mark.django_db
def test_form_prefills_memory_and_network_from_instance():
    from accounts.models import User

    user = User.objects.create_user(username="p1", email="p1@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER, user=user, name="dev", base_image="alpine", memory_bytes=2 * 2**30, network_enabled=True
    )
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    assert form.initial["memory_value"] == 2
    assert form.initial["memory_unit"] == "GiB"
    assert form.initial["network_choice"] == "on"


@pytest.mark.django_db
def test_form_prefill_memory_in_mib_when_not_whole_gib():
    from accounts.models import User

    user = User.objects.create_user(username="p2", email="p2@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER, user=user, name="dev", base_image="alpine", memory_bytes=512 * 2**20, network_enabled=None
    )
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    assert form.initial["memory_value"] == 512
    assert form.initial["memory_unit"] == "MiB"
    assert form.initial["network_choice"] == "default"


@pytest.mark.django_db
def test_form_rejects_custom_memory_mode_without_value():
    from accounts.models import User

    user = User.objects.create_user(username="m3", email="m3@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_mode": "custom",
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": "default",
            "cpu_mode": "default",
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert not form.is_valid()
    assert "memory_value" in form.errors


@pytest.mark.django_db
def test_form_rejects_custom_cpu_mode_without_value():
    from accounts.models import User

    user = User.objects.create_user(username="c1", email="c1@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "dev",
            "base_image": "alpine",
            "scope": Scope.USER,
            "memory_mode": "default",
            "memory_value": "",
            "memory_unit": "MiB",
            "network_choice": "default",
            "cpu_mode": "custom",
            "cpus": "",
            "env_vars_json": "[]",
        },
        user=user,
        is_admin=False,
    )
    assert not form.is_valid()
    assert "cpus" in form.errors


@pytest.mark.django_db
class TestRepoIdsField:
    def _post_data(self, **overrides):
        base = {
            "name": "env",
            "description": "",
            "scope": "user",
            "base_image": "python:3.14",
            "network_choice": "default",
            "memory_mode": "default",
            "cpu_mode": "default",
            "memory_unit": "MiB",
            "env_vars_json": "[]",
            "repo_ids_json": "[]",
        }
        base.update(overrides)
        return base

    def test_repo_ids_default_empty(self):
        user = User.objects.create(username="u", email="u@x.test")
        form = SandboxEnvironmentForm(data=self._post_data(), user=user, is_admin=False)
        assert form.is_valid(), form.errors
        env = form.save()
        assert env.repo_ids == []

    def test_repo_ids_parsed_from_json(self):
        user = User.objects.create(username="u", email="u@x.test")
        form = SandboxEnvironmentForm(
            data=self._post_data(repo_ids_json='["acme/foo", "acme/bar"]'), user=user, is_admin=False
        )
        assert form.is_valid(), form.errors
        env = form.save()
        assert env.repo_ids == ["acme/foo", "acme/bar"]

    def test_repo_ids_invalid_json_rejected(self):
        user = User.objects.create(username="u", email="u@x.test")
        form = SandboxEnvironmentForm(data=self._post_data(repo_ids_json="not json"), user=user, is_admin=False)
        assert not form.is_valid()
        assert "repo_ids_json" in form.errors

    def test_repo_ids_invalid_format_surfaces_as_field_error(self):
        user = User.objects.create(username="u", email="u@x.test")
        form = SandboxEnvironmentForm(data=self._post_data(repo_ids_json='["not-a-path"]'), user=user, is_admin=False)
        assert not form.is_valid()
        assert "repo_ids_json" in form.errors

    def test_repo_ids_uniqueness_violation_surfaced_as_form_error(self):
        user = User.objects.create(username="u", email="u@x.test")
        SandboxEnvironment.objects.create(
            scope=Scope.USER, user=user, name="a", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        form = SandboxEnvironmentForm(
            data=self._post_data(name="b", repo_ids_json='["acme/foo"]'), user=user, is_admin=False
        )
        # Note: model validation runs inside .save(); the view layer catches
        # ValidationError and re-attaches via form.add_error. The form itself
        # accepts the JSON, then model-level full_clean blocks the save.
        assert form.is_valid()
        with pytest.raises(ValidationError):
            form.save()


@pytest.mark.django_db
def test_form_env_vars_json_initial_is_empty_list_for_unsaved_instance(user):
    form = SandboxEnvironmentForm(user=user, is_admin=False)
    assert form.fields["env_vars_json"].initial == "[]"


@pytest.mark.django_db
def test_form_repo_ids_json_initial_is_empty_list_for_unsaved_instance(user):
    form = SandboxEnvironmentForm(user=user, is_admin=False)
    assert form.fields["repo_ids_json"].initial == "[]"


@pytest.mark.django_db
def test_form_env_vars_json_initial_masks_secret_values(user):
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine",
        env_vars=[
            {"name": "PLAIN", "value": "v1", "is_secret": False},
            {"name": "TOKEN", "value": "real-secret", "is_secret": True},
        ],
    )
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    parsed = json.loads(form.fields["env_vars_json"].initial)
    assert parsed == [
        {"name": "PLAIN", "value": "v1", "is_secret": False, "has_existing_value": False},
        {"name": "TOKEN", "value": "", "is_secret": True, "has_existing_value": True},
    ]


@pytest.mark.django_db
def test_form_repo_ids_json_initial_reflects_instance(user):
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER, user=user, name="dev", base_image="alpine", repo_ids=["o/a", "o/b"]
    )
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    assert json.loads(form.fields["repo_ids_json"].initial) == ["o/a", "o/b"]


@pytest.mark.django_db
def test_form_builds_egress_policy_and_secrets_from_hosts():
    from accounts.models import User

    user = User.objects.create_user(username="ue1", email="ue1@e.com", password="x")  # noqa: S106
    egress = json.dumps({
        "default": "deny",
        "intercept": "credentialed",
        "hosts": [
            {
                "host": "github.com",
                "methods": ["*"],
                "header": "",
                "value": "",
                "secret_name": "",
                "has_existing_value": False,
            },
            {
                "host": "api.openai.com",
                "methods": ["GET", "post"],
                "header": "Authorization",
                "value": "sk-live",
                "secret_name": "s_abc",
                "has_existing_value": False,
            },
        ],
    })
    form = SandboxEnvironmentForm(
        data={"name": "dev", "base_image": "alpine:latest", "scope": Scope.USER, "egress_json": egress},
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    assert env.egress_policy["default"] == "deny"
    assert env.egress_policy["intercept"] == "credentialed"
    rules = env.egress_policy["rules"]
    assert rules[0] == {"host": "github.com", "methods": ["*"], "inject": None}
    assert rules[1]["host"] == "api.openai.com"
    assert rules[1]["inject"] == "s_abc"
    # methods are uppercased by the form normalisation layer (clean_egress_json)
    assert rules[1]["methods"] == ["GET", "POST"]
    assert env.egress_secrets == {"s_abc": {"header": "Authorization", "value": "sk-live"}}


@pytest.mark.django_db
def test_form_writes_no_policy_when_no_hosts():
    from accounts.models import User

    user = User.objects.create_user(username="ue2", email="ue2@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={"name": "dev", "base_image": "alpine:latest", "scope": Scope.USER, "egress_json": ""},
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    # An egress policy is an allow-list; with no allowed-host rules the form stores no policy
    # (None) rather than a deny-all, so re-saving an env that never configured egress does not
    # change its runtime network behavior.
    assert env.egress_policy is None
    assert env.egress_secrets == {}


@pytest.mark.django_db
def test_form_synthesises_secret_name_for_new_credentialed_host():
    """A brand-new credentialed host with a blank ``secret_name`` (the common case:
    add a host, type a header+value, never minting a name) must get a unique synthesised
    ``inject`` wired to exactly one stored secret."""
    from accounts.models import User

    user = User.objects.create_user(username="ue4", email="ue4@e.com", password="x")  # noqa: S106
    egress = json.dumps({
        "default": "deny",
        "intercept": "all",
        "hosts": [
            {
                "host": "api.openai.com",
                "methods": ["*"],
                "header": "Authorization",
                "value": "sk-live",
                "secret_name": "",
                "has_existing_value": False,
            }
        ],
    })
    form = SandboxEnvironmentForm(
        data={"name": "dev", "base_image": "alpine:latest", "scope": Scope.USER, "egress_json": egress},
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    env = form.save()
    rules = env.egress_policy["rules"]
    assert len(rules) == 1
    inject = rules[0]["inject"]
    assert inject and inject.startswith("s_")
    assert set(env.egress_secrets) == {inject}
    assert env.egress_secrets[inject] == {"header": "Authorization", "value": "sk-live"}


@pytest.mark.django_db
def test_form_rejects_invalid_credential_header():
    """A credential header name carrying CR/LF (header-injection attempt) is rejected before it
    can reach the sidecar."""
    from accounts.models import User

    user = User.objects.create_user(username="ue5", email="ue5@e.com", password="x")  # noqa: S106
    form = SandboxEnvironmentForm(
        data={
            "name": "d",
            "base_image": "alpine",
            "scope": Scope.USER,
            "egress_json": json.dumps({
                "default": "deny",
                "intercept": "all",
                "hosts": [
                    {
                        "host": "api.openai.com",
                        "methods": ["*"],
                        "header": "Authorization\r\nX-Evil",
                        "value": "sk-live",
                        "secret_name": "",
                        "has_existing_value": False,
                    }
                ],
            }),
        },
        user=user,
        is_admin=False,
    )
    assert not form.is_valid()
    assert "egress_json" in form.errors


@pytest.mark.django_db
def test_form_rejects_bad_egress_enums_and_blank_host():
    from accounts.models import User

    user = User.objects.create_user(username="ue3", email="ue3@e.com", password="x")  # noqa: S106
    bad_default = SandboxEnvironmentForm(
        data={
            "name": "d",
            "base_image": "alpine",
            "scope": Scope.USER,
            "egress_json": json.dumps({"default": "nope", "intercept": "all", "hosts": []}),
        },
        user=user,
        is_admin=False,
    )
    assert not bad_default.is_valid()
    assert "egress_json" in bad_default.errors

    blank_host = SandboxEnvironmentForm(
        data={
            "name": "d",
            "base_image": "alpine",
            "scope": Scope.USER,
            "egress_json": json.dumps({
                "default": "deny",
                "intercept": "all",
                "hosts": [{"host": "  ", "methods": ["*"]}],
            }),
        },
        user=user,
        is_admin=False,
    )
    assert not blank_host.is_valid()
    assert "egress_json" in blank_host.errors


@pytest.mark.django_db
def test_egress_initial_masks_secret_values():
    from accounts.models import User

    user = User.objects.create_user(username="ue4", email="ue4@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        egress_policy={
            "default": "deny",
            "intercept": "all",
            "rules": [{"host": "api.openai.com", "methods": ["*"], "inject": "s1"}],
        },
        egress_secrets={"s1": {"header": "Authorization", "value": "sk-secret"}},
    )
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    state = json.loads(form.fields["egress_json"].initial)
    host = state["hosts"][0]
    assert host["host"] == "api.openai.com"
    assert host["secret_name"] == "s1"  # noqa: S105
    assert host["header"] == "Authorization"
    assert host["value"] == ""  # masked — never leaks plaintext
    assert host["has_existing_value"] is True
    assert "sk-secret" not in form.fields["egress_json"].initial


@pytest.mark.django_db
@pytest.mark.parametrize("masked_value", ["", "******"])
def test_egress_preserves_unchanged_secret_on_edit(masked_value):
    from accounts.models import User

    user = User.objects.create_user(username="ue5", email="ue5@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        egress_policy={
            "default": "deny",
            "intercept": "all",
            "rules": [{"host": "api.openai.com", "methods": ["*"], "inject": "s1"}],
        },
        egress_secrets={"s1": {"header": "Authorization", "value": "sk-secret"}},
    )
    submitted = json.dumps({
        "default": "deny",
        "intercept": "all",
        "hosts": [
            {
                "host": "api.openai.com",
                "methods": ["*"],
                "header": "Authorization",
                "value": masked_value,
                "secret_name": "s1",
                "has_existing_value": True,
            }
        ],
    })
    form = SandboxEnvironmentForm(
        instance=env,
        data={"name": "dev", "base_image": "alpine:latest", "scope": Scope.USER, "egress_json": submitted},
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    saved = form.save()
    assert saved.egress_secrets == {"s1": {"header": "Authorization", "value": "sk-secret"}}


@pytest.mark.django_db
def test_egress_changed_secret_overwrites_on_edit():
    from accounts.models import User

    user = User.objects.create_user(username="ue6", email="ue6@e.com", password="x")  # noqa: S106
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        egress_policy={
            "default": "deny",
            "intercept": "all",
            "rules": [{"host": "api.openai.com", "methods": ["*"], "inject": "s1"}],
        },
        egress_secrets={"s1": {"header": "Authorization", "value": "old"}},
    )
    submitted = json.dumps({
        "default": "deny",
        "intercept": "all",
        "hosts": [
            {
                "host": "api.openai.com",
                "methods": ["*"],
                "header": "Authorization",
                "value": "new",
                "secret_name": "s1",
                "has_existing_value": True,
            }
        ],
    })
    form = SandboxEnvironmentForm(
        instance=env,
        data={"name": "dev", "base_image": "alpine:latest", "scope": Scope.USER, "egress_json": submitted},
        user=user,
        is_admin=False,
    )
    assert form.is_valid(), form.errors
    saved = form.save()
    assert saved.egress_secrets["s1"]["value"] == "new"


@pytest.mark.django_db
def test_egress_initial_returns_empty_credentials_on_decryption_error(user, mocker, caplog):
    """Exercises the swallow branch of ``_initial_egress_json``: when the
    ``egress_secrets`` getter raises ``DecryptionError``, the editor must still
    render the policy (host/methods) but with empty credentials, never raising."""
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine",
        egress_policy={
            "default": "deny",
            "intercept": "all",
            "rules": [{"host": "api.openai.com", "methods": ["*"], "inject": "s1"}],
        },
        egress_secrets={"s1": {"header": "Authorization", "value": "sk-secret"}},
    )
    from core.encryption import DecryptionError

    # Patch the descriptor: when accessed on the instance, raise DecryptionError.
    def _raise(instance):
        raise DecryptionError("bad key")

    mocker.patch.object(type(env), "egress_secrets", new_callable=lambda: property(fget=_raise))
    caplog.set_level(logging.ERROR, logger="daiv.sandbox_envs")
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    state = json.loads(form.fields["egress_json"].initial)
    host = state["hosts"][0]
    assert host["host"] == "api.openai.com"
    assert host["methods"] == ["*"]
    assert host["header"] == ""
    assert host["value"] == ""
    assert host["has_existing_value"] is False
    assert "decryption failed" in caplog.text


@pytest.mark.django_db
def test_egress_refuses_when_existing_secrets_cannot_be_decrypted(user, mocker):
    """Exercises the RAISE branch of ``_preserve_unchanged_egress_secrets``: a
    masked edit over unreadable ciphertext must refuse to persist rather than
    silently overwrite the still-valid secret with the mask."""
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine:latest",
        egress_policy={
            "default": "deny",
            "intercept": "all",
            "rules": [{"host": "api.openai.com", "methods": ["*"], "inject": "s1"}],
        },
        egress_secrets={"s1": {"header": "Authorization", "value": "sk-secret"}},
    )
    original_ciphertext = SandboxEnvironment.objects.get(pk=env.pk)._egress_secrets_encrypted
    from core.encryption import DecryptionError

    # Patch the descriptor: when accessed on the instance, raise DecryptionError.
    def _raise(instance):
        raise DecryptionError("bad key")

    mocker.patch.object(type(env), "egress_secrets", new_callable=lambda: property(fget=_raise))
    submitted = json.dumps({
        "default": "deny",
        "intercept": "all",
        "hosts": [
            {
                "host": "api.openai.com",
                "methods": ["*"],
                "header": "Authorization",
                "value": "",
                "secret_name": "s1",
                "has_existing_value": True,
            }
        ],
    })
    form = SandboxEnvironmentForm(
        instance=env,
        data={"name": "dev", "base_image": "alpine:latest", "scope": Scope.USER, "egress_json": submitted},
        user=user,
        is_admin=False,
    )
    # Validation guard: model-level _validate_egress also catches the unreadable
    # ciphertext during _post_clean, so the form never reports as valid and the
    # original ciphertext is left untouched.
    assert form.is_valid() is False
    env.refresh_from_db()
    assert env._egress_secrets_encrypted == original_ciphertext
    # Form-helper guard (defense in depth): _preserve_unchanged_egress_secrets raises
    # rather than letting a masked value overwrite the undecryptable stored secret.
    with pytest.raises(ValidationError):
        SandboxEnvironmentForm._preserve_unchanged_egress_secrets(env, {"s1": {"header": "Authorization", "value": ""}})


@pytest.mark.django_db
def test_form_env_vars_json_initial_returns_empty_on_decryption_error(user, mocker, caplog):
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="alpine",
        env_vars=[{"name": "TOKEN", "value": "secret", "is_secret": True}],
    )
    from core.encryption import DecryptionError

    # Patch the descriptor: when accessed on the instance, raise DecryptionError.
    def _raise(instance):
        raise DecryptionError("bad key")

    mocker.patch.object(type(env), "env_vars", new_callable=lambda: property(fget=_raise))
    caplog.set_level(logging.ERROR, logger="daiv.sandbox_envs")
    form = SandboxEnvironmentForm(instance=env, user=user, is_admin=False)
    assert form.fields["env_vars_json"].initial == "[]"
    assert "decryption failed" in caplog.text
