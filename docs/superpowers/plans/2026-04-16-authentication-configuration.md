# Authentication Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make social authentication (GitHub/GitLab OAuth) configurable through the existing `/configuration/` admin UI, gated by `CODEBASE_CLIENT`.

**Architecture:** Extend the `SiteConfiguration` singleton with `auth_*` fields, wire allauth via a synthetic `SocialApp` returned from an overridden `list_apps()`, and delete the old env-based `_register_provider` code. Add a management command to bootstrap the first admin on fresh installs.

**Tech Stack:** Django, django-allauth, existing `SiteConfiguration`/`SiteSettings`/`EncryptedFieldDescriptor` infrastructure.

**Spec:** `docs/superpowers/specs/2026-04-16-authentication-configuration-design.md`

---

## File map

| Action | File | Responsibility |
|--------|------|----------------|
| Modify | `daiv/core/models.py` | Add `auth_*` fields, encrypted descriptor, `ENCRYPTED_FIELDS`, `FIELD_GROUPS` |
| Modify | `daiv/core/site_settings.py` | Add `ENV_VAR_OVERRIDES` + default for `auth_gitlab_url` |
| Modify | `daiv/core/forms.py` | Add auth fields to `Meta.fields`, implement `_hide_inapplicable_auth_fields` |
| Modify | `daiv/core/views.py` | Filter empty field groups from template context |
| Modify | `daiv/accounts/adapter.py` | Add `list_apps()` to `SocialAccountAdapter` |
| Modify | `daiv/daiv/settings/components/allauth.py` | Replace `_register_provider` with scopes-only dict |
| Create | `daiv/accounts/management/commands/bootstrap_admin.py` | First-admin seeding command |
| Create | `daiv/core/migrations/NNNN_add_auth_fields.py` | Auto-generated migration |
| Modify | `tests/unit_tests/accounts/test_adapter.py` | Tests for `list_apps()` |
| Modify | `tests/unit_tests/core/test_configuration_views.py` | Tests for conditional form filtering |
| Create | `tests/unit_tests/accounts/management/commands/test_bootstrap_admin.py` | Tests for the new command |

---

### Task 1: Add auth fields to SiteConfiguration and wire SiteSettings

**Files:**
- Modify: `daiv/core/models.py:270-325`
- Modify: `daiv/core/site_settings.py:28-68` and `86-93`

- [ ] **Step 1: Add auth model fields and encrypted descriptor to `SiteConfiguration`**

In `daiv/core/models.py`, add after the sandbox fields block (after line 244, before the `# -- Features --` comment at line 247):

```python
    # -- Authentication --
    auth_client_id = models.CharField(
        _("OAuth client ID"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("OAuth application client ID for the configured Git platform."),
    )
    auth_gitlab_url = models.CharField(
        _("GitLab URL"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Browser-facing URL of your GitLab instance."),
    )
    auth_gitlab_server_url = models.CharField(
        _("GitLab server URL"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Server-to-server URL for token exchange in Docker-internal networks. Leave empty to use the GitLab URL."),
    )
```

Add the encrypted column and descriptor after the existing `_sandbox_api_key_encrypted` line (after line 277):

```python
    _auth_client_secret_encrypted = models.TextField(blank=True, null=True, editable=False)
```

Add the descriptor after the existing `sandbox_api_key` descriptor (after line 285):

```python
    auth_client_secret = EncryptedFieldDescriptor("auth_client_secret")
```

- [ ] **Step 2: Update `ENCRYPTED_FIELDS` and `FIELD_GROUPS`**

In `daiv/core/models.py`, update `ENCRYPTED_FIELDS` (line 298) to append `"auth_client_secret"`:

```python
    ENCRYPTED_FIELDS: ClassVar[tuple[str, ...]] = (
        "anthropic_api_key",
        "openai_api_key",
        "google_api_key",
        "openrouter_api_key",
        "web_search_api_key",
        "sandbox_api_key",
        "auth_client_secret",
    )
```

Update `FIELD_GROUPS` (line 307) to append the authentication group:

```python
    FIELD_GROUPS: ClassVar[tuple[FieldGroup, ...]] = (
        FieldGroup(key="agent", title=_("Agent"), match=("agent_*", "suggest_context_file_enabled"), icon="agent"),
        FieldGroup(
            key="diff_to_metadata",
            title=_("Commit & PR Writer"),
            match=("diff_to_metadata_*",),
            icon="diff-to-metadata",
        ),
        FieldGroup(
            key="providers",
            title=_("Providers"),
            match=("anthropic_*", "openai_*", "google_*", "openrouter_*"),
            icon="providers",
        ),
        FieldGroup(key="web_search", title=_("Web Search"), match=("web_search_*",), icon="web-search"),
        FieldGroup(key="web_fetch", title=_("Web Fetch"), match=("web_fetch_*",), icon="web-fetch"),
        FieldGroup(key="sandbox", title=_("Sandbox"), match=("sandbox_*",), icon="sandbox"),
        FieldGroup(key="jobs", title=_("Jobs"), match=("jobs_*",), icon="jobs"),
        FieldGroup(key="authentication", title=_("Authentication"), match=("auth_*",), icon="lock-closed"),
    )
```

- [ ] **Step 3: Add env var overrides and defaults to `SiteSettings`**

In `daiv/core/site_settings.py`, add to `ENV_VAR_OVERRIDES` (line 88):

```python
    ENV_VAR_OVERRIDES: ClassVar[dict[str, str]] = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "google_api_key": "GOOGLE_API_KEY",
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "auth_client_id": "ALLAUTH_CLIENT_ID",
        "auth_client_secret": "ALLAUTH_CLIENT_SECRET",
        "auth_gitlab_url": "ALLAUTH_GITLAB_URL",
        "auth_gitlab_server_url": "ALLAUTH_GITLAB_SERVER_URL",
    }
```

In `_build_field_defaults()` (line 28), add after the `"jobs_throttle_rate"` entry:

```python
        # Authentication
        "auth_gitlab_url": "https://gitlab.com",
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python daiv/manage.py makemigrations core --name add_auth_fields`

Expected: creates `daiv/core/migrations/NNNN_add_auth_fields.py` with `AddField` operations for `auth_client_id`, `auth_gitlab_url`, `auth_gitlab_server_url`, and `_auth_client_secret_encrypted`.

- [ ] **Step 5: Run the migration**

Run: `uv run python daiv/manage.py migrate --settings=daiv.settings.test`

Expected: `Applying core.NNNN_add_auth_fields... OK`

- [ ] **Step 6: Verify existing tests still pass**

Run: `uv run pytest tests/unit_tests/core/test_site_configuration.py tests/unit_tests/core/test_configuration_views.py -v`

Expected: all existing tests pass.

- [ ] **Step 7: Commit**

```bash
git add daiv/core/models.py daiv/core/site_settings.py daiv/core/migrations/
git commit -m "feat(core): add auth fields to SiteConfiguration and SiteSettings"
```

---

### Task 2: TDD — SocialAccountAdapter.list_apps

**Files:**
- Modify: `tests/unit_tests/accounts/test_adapter.py`
- Modify: `daiv/accounts/adapter.py`

- [ ] **Step 1: Write failing tests for `list_apps`**

Add the following to `tests/unit_tests/accounts/test_adapter.py`:

```python
from pydantic import SecretStr


class TestSocialAccountAdapterListApps:
    @pytest.fixture(autouse=True)
    def _clear_site_config_cache(self, db):
        from django.core.cache import cache
        cache.clear()

    def test_returns_empty_when_codebase_client_is_swe(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform
            mock_codebase.CLIENT = GitPlatform.SWE
            mock_site.auth_client_id = "some-id"
            mock_site.auth_client_secret = SecretStr("some-secret")
            result = adapter.list_apps(Mock())
            assert result == []

    def test_returns_empty_when_client_id_is_none(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform
            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = None
            mock_site.auth_client_secret = SecretStr("some-secret")
            result = adapter.list_apps(Mock())
            assert result == []

    def test_returns_empty_when_secret_is_none(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform
            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = "some-id"
            mock_site.auth_client_secret = None
            result = adapter.list_apps(Mock())
            assert result == []

    def test_returns_empty_for_non_matching_provider(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform
            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = "some-id"
            mock_site.auth_client_secret = SecretStr("some-secret")
            result = adapter.list_apps(Mock(), provider="github")
            assert result == []

    def test_returns_app_for_matching_gitlab(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform
            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = "gl-client-id"
            mock_site.auth_client_secret = SecretStr("gl-secret")
            mock_site.auth_gitlab_url = "https://gitlab.example.com"
            mock_site.auth_gitlab_server_url = "http://gitlab:8080"
            result = adapter.list_apps(Mock())
            assert len(result) == 1
            app = result[0]
            assert app.provider == "gitlab"
            assert app.client_id == "gl-client-id"
            assert app.secret == "gl-secret"
            assert app.settings["gitlab_url"] == "https://gitlab.example.com"
            assert app.settings["gitlab_server_url"] == "http://gitlab:8080"

    def test_returns_app_for_matching_github(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform
            mock_codebase.CLIENT = GitPlatform.GITHUB
            mock_site.auth_client_id = "gh-client-id"
            mock_site.auth_client_secret = SecretStr("gh-secret")
            result = adapter.list_apps(Mock())
            assert len(result) == 1
            app = result[0]
            assert app.provider == "github"
            assert app.client_id == "gh-client-id"
            assert app.secret == "gh-secret"
            assert app.settings == {}

    def test_handles_plain_string_secret(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform
            mock_codebase.CLIENT = GitPlatform.GITHUB
            mock_site.auth_client_id = "gh-client-id"
            mock_site.auth_client_secret = "plain-secret"
            result = adapter.list_apps(Mock())
            assert len(result) == 1
            assert result[0].secret == "plain-secret"

    def test_gitlab_defaults_url_when_none(self, adapter):
        with (
            patch("accounts.adapter.codebase_settings") as mock_codebase,
            patch("accounts.adapter.site_settings") as mock_site,
        ):
            from codebase.base import GitPlatform
            mock_codebase.CLIENT = GitPlatform.GITLAB
            mock_site.auth_client_id = "gl-client-id"
            mock_site.auth_client_secret = SecretStr("gl-secret")
            mock_site.auth_gitlab_url = None
            mock_site.auth_gitlab_server_url = None
            result = adapter.list_apps(Mock())
            assert result[0].settings["gitlab_url"] == "https://gitlab.com"
            assert result[0].settings["gitlab_server_url"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit_tests/accounts/test_adapter.py::TestSocialAccountAdapterListApps -v`

Expected: all tests FAIL — `list_apps` does not exist yet on our adapter, or `accounts.adapter.codebase_settings` / `accounts.adapter.site_settings` are not importable from that module.

- [ ] **Step 3: Implement `list_apps` on `SocialAccountAdapter`**

In `daiv/accounts/adapter.py`, add imports at the top (after the existing `logging` import):

```python
from allauth.socialaccount.models import SocialApp

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings
from core.site_settings import site_settings
```

Add a module-level mapping after the `logger` definition:

```python
_PLATFORM_TO_PROVIDER: dict[GitPlatform, str] = {
    GitPlatform.GITLAB: "gitlab",
    GitPlatform.GITHUB: "github",
}
```

Add the `list_apps` method to `SocialAccountAdapter` (after the existing `is_open_for_signup` method):

```python
    def list_apps(self, request, provider=None, client_id=None):
        expected_provider = _PLATFORM_TO_PROVIDER.get(codebase_settings.CLIENT)
        if expected_provider is None:
            return []
        if provider and provider != expected_provider:
            return []

        client_id_value = site_settings.auth_client_id
        secret = site_settings.auth_client_secret
        if not client_id_value or secret is None:
            return []

        app_settings: dict[str, str] = {}
        if expected_provider == "gitlab":
            app_settings = {
                "gitlab_url": site_settings.auth_gitlab_url or "https://gitlab.com",
                "gitlab_server_url": site_settings.auth_gitlab_server_url or "",
            }

        secret_value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
        return [
            SocialApp(
                provider=expected_provider,
                name=f"{expected_provider.capitalize()} (SiteConfiguration)",
                client_id=client_id_value,
                secret=secret_value,
                settings=app_settings,
            )
        ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit_tests/accounts/test_adapter.py::TestSocialAccountAdapterListApps -v`

Expected: all 9 tests PASS.

- [ ] **Step 5: Run the full adapter test suite for regressions**

Run: `uv run pytest tests/unit_tests/accounts/test_adapter.py -v`

Expected: all tests PASS (existing + new).

- [ ] **Step 6: Commit**

```bash
git add daiv/accounts/adapter.py tests/unit_tests/accounts/test_adapter.py
git commit -m "feat(accounts): add list_apps to SocialAccountAdapter for DB-driven OAuth"
```

---

### Task 3: TDD — SiteConfigurationForm conditional auth field filtering

**Files:**
- Modify: `tests/unit_tests/core/test_configuration_views.py`
- Modify: `daiv/core/forms.py`

- [ ] **Step 1: Write failing tests for `_hide_inapplicable_auth_fields`**

Add the following to `tests/unit_tests/core/test_configuration_views.py`:

```python
from unittest.mock import patch


class TestAuthFieldFiltering:
    def _get_form_fields(self, platform):
        from codebase.base import GitPlatform
        from core.forms import SiteConfigurationForm
        from core.models import SiteConfiguration

        config = SiteConfiguration.objects.get_instance()
        with patch("codebase.conf.settings") as mock_codebase:
            mock_codebase.CLIENT = getattr(GitPlatform, platform)
            form = SiteConfigurationForm(instance=config)
        return set(form.fields.keys())

    def test_gitlab_shows_all_auth_fields(self, db):
        fields = self._get_form_fields("GITLAB")
        assert "auth_client_id" in fields
        assert "auth_client_secret" in fields
        assert "auth_gitlab_url" in fields
        assert "auth_gitlab_server_url" in fields

    def test_github_hides_gitlab_specific_fields(self, db):
        fields = self._get_form_fields("GITHUB")
        assert "auth_client_id" in fields
        assert "auth_client_secret" in fields
        assert "auth_gitlab_url" not in fields
        assert "auth_gitlab_server_url" not in fields

    def test_swe_hides_all_auth_fields(self, db):
        fields = self._get_form_fields("SWE")
        assert "auth_client_id" not in fields
        assert "auth_client_secret" not in fields
        assert "auth_gitlab_url" not in fields
        assert "auth_gitlab_server_url" not in fields
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit_tests/core/test_configuration_views.py::TestAuthFieldFiltering -v`

Expected: FAIL — auth fields are not in `Meta.fields` yet, or the filtering method does not exist.

- [ ] **Step 3: Add auth fields to `SiteConfigurationForm.Meta.fields` and implement filtering**

In `daiv/core/forms.py`, add the auth fields to `Meta.fields` (after `"jobs_throttle_rate"` at line 120):

```python
            "auth_client_id",
            "auth_gitlab_url",
            "auth_gitlab_server_url",
```

Add the filtering method to `SiteConfigurationForm` (after `_apply_defaults`, before the `# Validation` section):

```python
    def _hide_inapplicable_auth_fields(self) -> None:
        """Remove auth fields that don't apply to the current CODEBASE_CLIENT."""
        from codebase.base import GitPlatform
        from codebase.conf import settings as codebase_settings

        client = codebase_settings.CLIENT
        if client == GitPlatform.GITHUB:
            for name in ("auth_gitlab_url", "auth_gitlab_server_url"):
                self.fields.pop(name, None)
        elif client not in (GitPlatform.GITHUB, GitPlatform.GITLAB):
            for name in ("auth_client_id", "auth_client_secret", "auth_gitlab_url", "auth_gitlab_server_url"):
                self.fields.pop(name, None)
```

Call it at the end of `__init__` (after `self._apply_defaults(field_defaults or {})`):

```python
        self._hide_inapplicable_auth_fields()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit_tests/core/test_configuration_views.py::TestAuthFieldFiltering -v`

Expected: all 3 tests PASS.

- [ ] **Step 5: Run the full configuration views test suite for regressions**

Run: `uv run pytest tests/unit_tests/core/test_configuration_views.py -v`

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add daiv/core/forms.py tests/unit_tests/core/test_configuration_views.py
git commit -m "feat(core): add auth fields to SiteConfigurationForm with conditional filtering"
```

---

### Task 4: Filter empty groups from view context

**Files:**
- Modify: `daiv/core/views.py:73-75`

- [ ] **Step 1: Update `_build_context` to filter empty groups**

In `daiv/core/views.py`, replace the `_build_context` method (line 73):

```python
    @staticmethod
    def _build_context(form: SiteConfigurationForm) -> dict:
        groups = [g for g in SiteConfiguration.get_field_groups() if any(f in form.fields for f in g.fields)]
        return {"form": form, "field_groups": groups}
```

- [ ] **Step 2: Verify the configuration page still works**

Run: `uv run pytest tests/unit_tests/core/test_configuration_views.py -v`

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add daiv/core/views.py
git commit -m "fix(core): filter empty field groups from configuration page context"
```

---

### Task 5: Simplify allauth settings — remove `_register_provider`

**Files:**
- Modify: `daiv/daiv/settings/components/allauth.py`

- [ ] **Step 1: Replace lines 38–68 with scopes-only dict**

Replace the entire block from `SOCIALACCOUNT_PROVIDERS = {}` (line 40) through the two `_register_provider(...)` calls (lines 58–68) with:

```python
# Provider scopes are static; credentials, URLs, and enablement come from
# SiteConfiguration via accounts.adapter.SocialAccountAdapter.list_apps().
SOCIALACCOUNT_PROVIDERS = {
    "github": {"SCOPE": ["user:email"]},
    "gitlab": {"SCOPE": ["read_user"]},
}
```

Remove the `_register_provider` function (lines 43–55) and the empty dict assignment on line 40.

Remove the now-unused imports: `logging`, `get_docker_secret`, and `config` from `decouple`. The file should start with:

```python
# ---------------------------------------------------------------------------
# django-allauth
# ---------------------------------------------------------------------------

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
```

- [ ] **Step 2: Run the full test suite to verify nothing breaks**

Run: `uv run pytest tests/unit_tests/ -x -q`

Expected: all tests PASS. The adapter's `list_apps` now provides apps; the removed `_register_provider` code is no longer needed.

- [ ] **Step 3: Commit**

```bash
git add daiv/daiv/settings/components/allauth.py
git commit -m "refactor(settings): replace _register_provider with scopes-only SOCIALACCOUNT_PROVIDERS

Credentials and URLs are now served at runtime by
SocialAccountAdapter.list_apps() from SiteConfiguration."
```

---

### Task 6: TDD — `bootstrap_admin` management command

**Files:**
- Create: `tests/unit_tests/accounts/management/commands/test_bootstrap_admin.py`
- Create: `daiv/accounts/management/commands/bootstrap_admin.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit_tests/accounts/management/commands/test_bootstrap_admin.py`:

```python
from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from accounts.models import Role, User


@pytest.mark.django_db
class TestBootstrapAdmin:
    def test_creates_admin_on_empty_system(self):
        call_command("bootstrap_admin", "admin@example.com")
        user = User.objects.get(email="admin@example.com")
        assert user.role == Role.ADMIN
        assert user.is_active

    def test_refuses_when_admin_exists(self):
        User.objects.create_user(username="existing-admin", email="admin@example.com", role=Role.ADMIN)
        with pytest.raises(CommandError, match="admin user already exists"):
            call_command("bootstrap_admin", "new@example.com")

    def test_refuses_when_email_collides(self):
        User.objects.create_user(username="member", email="taken@example.com", role=Role.MEMBER)
        with pytest.raises(CommandError, match="already exists"):
            call_command("bootstrap_admin", "taken@example.com")

    def test_prints_success_message(self, capsys):
        call_command("bootstrap_admin", "admin@example.com")
        captured = capsys.readouterr()
        assert "admin@example.com" in captured.out
        assert "login-by-code" in captured.out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit_tests/accounts/management/commands/test_bootstrap_admin.py -v`

Expected: FAIL — `No module named 'accounts.management.commands.bootstrap_admin'` or `Unknown command: 'bootstrap_admin'`.

- [ ] **Step 3: Implement the management command**

Create `daiv/accounts/management/commands/bootstrap_admin.py`:

```python
from django.core.management.base import BaseCommand, CommandError

from accounts.models import Role, User


class Command(BaseCommand):
    help = "Create the initial admin user on a fresh install."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email address for the new admin user.")

    def handle(self, *args, email, **options):
        if User.objects.filter(role=Role.ADMIN).exists():
            raise CommandError("An admin user already exists. This command only bootstraps the first one.")

        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(f"A user with email '{email}' already exists. Promote them via the admin UI instead.")

        user = User.objects.create_user(username=email, email=email, role=Role.ADMIN)
        self.stdout.write(self.style.SUCCESS(f"Admin user '{email}' created (pk={user.pk})."))
        self.stdout.write("Log in via /accounts/login/ using login-by-code (a one-time code sent by email).")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit_tests/accounts/management/commands/test_bootstrap_admin.py -v`

Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add daiv/accounts/management/commands/bootstrap_admin.py tests/unit_tests/accounts/management/commands/test_bootstrap_admin.py
git commit -m "feat(accounts): add bootstrap_admin management command for fresh installs"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/unit_tests/ -x -q`

Expected: all tests PASS with zero failures.

- [ ] **Step 2: Run linting and type checking**

Run: `make lint-fix && make lint-typing`

Expected: no new errors.

- [ ] **Step 3: Verify the migration applies cleanly from scratch**

Run: `uv run python daiv/manage.py migrate --settings=daiv.settings.test --run-syncdb`

Expected: clean migration with no errors.
