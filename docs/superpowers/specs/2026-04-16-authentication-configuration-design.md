# Configurable social authentication through the website

## Problem

Today, social login providers (GitHub, GitLab) are configured via Docker secrets read
at Django startup in `daiv/daiv/settings/components/allauth.py:38-68`. Rotating OAuth
credentials, pointing at a different GitLab instance, or flipping between GitHub and
GitLab requires editing secrets and restarting the stack. We want operators of a DAIV
instance to configure social login from the admin UI, with the constraint that only
the provider matching `CODEBASE_CLIENT` is configurable at any given time.

## Goals

- Admins configure OAuth `client_id`, `client_secret`, and (for GitLab) `gitlab_url` /
  `gitlab_server_url` through the existing `/configuration/` page.
- Only the provider matching `CODEBASE_CLIENT` is exposed for configuration; the
  other's fields are neither rendered nor usable.
- Env-var / Docker-secret configuration continues to work as a bootstrap + IaC path,
  with clear precedence (env wins and locks the UI field).
- OAuth client secrets are encrypted at rest, consistent with how API keys are stored
  elsewhere in `SiteConfiguration`.
- Fresh installs have a way to mint the first admin without a working social provider.

## Non-goals

- Multi-tenant / multi-app-per-provider support. One provider is active at a time.
- Runtime switching of `CODEBASE_CLIENT`. It remains a boot-time pydantic setting in
  `daiv/codebase/conf.py:10`; flipping it still requires a restart.
- Testing the OAuth roundtrip end-to-end, or integration-testing allauth's templates.
- Replacing login-by-code or email-password login. Those continue to work unchanged.

## Architecture

Extend the existing `SiteConfiguration` singleton at `daiv/core/models.py:97` with an
`authentication` field group. Route allauth through a custom
`SocialAccountAdapter.list_apps()` that reads from `SiteSettings` and returns an
in-memory `SocialApp` when (a) `CODEBASE_CLIENT` maps to a real provider and (b)
credentials are present. Delete the old env-based `_register_provider` block in
settings. Add a management command to bootstrap the first admin on fresh installs.

No new view, no new URL, no new template, no parallel config layer. Everything
piggybacks on existing infrastructure.

## Data model

New fields on `core/models.py:SiteConfiguration`:

```python
# -- Authentication --
auth_client_id = CharField(
    _("OAuth client ID"), max_length=255, blank=True, null=True,
    help_text=_("OAuth application client ID for the configured Git platform."),
)
auth_gitlab_url = CharField(
    _("GitLab URL"), max_length=255, blank=True, null=True,
    help_text=_("Browser-facing URL of your GitLab instance. Default: https://gitlab.com"),
)
auth_gitlab_server_url = CharField(
    _("GitLab server URL"), max_length=255, blank=True, null=True,
    help_text=_("Server-to-server URL for token exchange (Docker-internal networks). Optional."),
)

# Encrypted secret using the existing EncryptedFieldDescriptor pattern
_auth_client_secret_encrypted = TextField(blank=True, null=True, editable=False)
auth_client_secret = EncryptedFieldDescriptor("auth_client_secret")
```

Added to module-level tuples on `SiteConfiguration`:

```python
ENCRYPTED_FIELDS = (..., "auth_client_secret")
FIELD_GROUPS = (..., FieldGroup(
    key="authentication", title=_("Authentication"),
    match=("auth_*",), icon="authentication",
))
```

Default added in `core/site_settings.py:_build_field_defaults()`:

```python
"auth_gitlab_url": "https://gitlab.com",
# auth_gitlab_server_url, auth_client_id, auth_client_secret: no defaults
```

**Rationale for one `auth_client_*` pair instead of provider-specific columns.**
Only one provider is active at a time. Separate columns would leak state from a
previous platform choice and create dead data. Switching `CODEBASE_CLIENT` legitimately
requires re-entering OAuth credentials (GitHub OAuth apps don't work on a GitLab
instance, and vice versa).

A single migration under `core/migrations/` adds these columns.

## Env var integration

Four entries added to `core/site_settings.py:SiteSettings.ENV_VAR_OVERRIDES`:

```python
ENV_VAR_OVERRIDES = {
    ...,
    "auth_client_id":         "ALLAUTH_CLIENT_ID",
    "auth_client_secret":     "ALLAUTH_CLIENT_SECRET",
    "auth_gitlab_url":        "ALLAUTH_GITLAB_URL",
    "auth_gitlab_server_url": "ALLAUTH_GITLAB_SERVER_URL",
}
```

**Breaking rename.** The existing env vars `ALLAUTH_GITHUB_CLIENT_ID` /
`ALLAUTH_GITHUB_SECRET` / `ALLAUTH_GITLAB_CLIENT_ID` / `ALLAUTH_GITLAB_SECRET`
collapse to generic `ALLAUTH_CLIENT_ID` / `ALLAUTH_CLIENT_SECRET`. Rationale: since
only one provider is active at a time, provider-qualified env var names encode
redundant state. Operators of existing deployments rename their Docker secret file
as a one-line migration step on upgrade.

With these entries in place, precedence for every auth field is automatic:

1. Docker secret / env var (UI shows field as locked — via existing `is_env_locked()`)
2. DB value from `SiteConfiguration`
3. Hardcoded default (only `auth_gitlab_url` has one)

No new fallback code — we use the existing chain in
`core/site_settings.py:100-128`.

## Allauth wiring

Extend `accounts/adapter.py:SocialAccountAdapter` with `list_apps`:

```python
from allauth.socialaccount.models import SocialApp
from codebase.conf import settings as codebase_settings
from codebase.base import GitPlatform
from core.site_settings import site_settings

_PLATFORM_TO_PROVIDER = {
    GitPlatform.GITLAB: "gitlab",
    GitPlatform.GITHUB: "github",
}

class SocialAccountAdapter(DefaultSocialAccountAdapter):
    # ... existing save_user / is_open_for_signup kept as-is ...

    def list_apps(self, request, provider=None, client_id=None):
        expected_provider = _PLATFORM_TO_PROVIDER.get(codebase_settings.CLIENT)
        if expected_provider is None:
            return []  # SWE or unknown → no social login
        if provider and provider != expected_provider:
            return []  # asked for the non-matching provider

        client_id_value = site_settings.auth_client_id
        secret = site_settings.auth_client_secret  # SecretStr | None
        if not client_id_value or secret is None:
            return []  # not configured → no apps → no login buttons, no OAuth URLs

        app_settings: dict[str, str] = {}
        if expected_provider == "gitlab":
            app_settings = {
                "gitlab_url": site_settings.auth_gitlab_url or "https://gitlab.com",
                "gitlab_server_url": site_settings.auth_gitlab_server_url or "",
            }

        secret_value = (
            secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret
        )
        return [SocialApp(
            provider=expected_provider,
            name=f"{expected_provider.capitalize()} (SiteConfiguration)",
            client_id=client_id_value,
            secret=secret_value,
            settings=app_settings,
        )]
```

**Design notes:**

- **Empty list, not exception, when not configured.** Allauth interprets "no apps
  for provider" as "provider unavailable" — the login button does not render and the
  OAuth URL does not resolve. Clean failure mode, no custom templates needed.
- **Strict enforcement lives in one place.** `CODEBASE_CLIENT` is the single gate;
  `list_apps` is the single allauth extension point consulted for both URL dispatch
  and template rendering.
- **Unsaved `SocialApp` instance.** Allauth's adapters read `.client_id`, `.secret`,
  `.settings` as value attributes; we never persist the `SocialApp` row. Behavior is
  validated in the unit tests described below.
- **`GitLabServerAwareAdapter` (`accounts/socialaccount.py:7`) keeps working
  unchanged** — it already reads settings via
  `get_adapter().get_app(request, provider_id)`, which delegates to `list_apps()`.
- **Handles both `SecretStr` (env path) and plain `str` (DB path)** for the secret,
  because `SiteSettings.__getattr__` returns `SecretStr` for encrypted fields
  regardless of source.

**Cache behavior.** `site_settings.auth_*` reads go through
`SiteConfiguration.get_cached()` (5-minute cache); saving new credentials through
the form invalidates the cache via `SiteConfiguration.save()`
(`core/models.py:465`). In-flight requests may see stale credentials for a few
minutes after a change — acceptable for OAuth config.

## UI form rendering

Two changes to `core/forms.py:SiteConfigurationForm`:

**1. Add to `Meta.fields`:**

```python
class Meta:
    model = SiteConfiguration
    fields = [
        ...,
        "auth_client_id",
        "auth_gitlab_url",
        "auth_gitlab_server_url",
    ]
# auth_client_secret is picked up automatically via SECRET_FIELDS = SiteConfiguration.ENCRYPTED_FIELDS
```

**2. Filter based on `CODEBASE_CLIENT` in `__init__`:**

```python
def _hide_inapplicable_auth_fields(self) -> None:
    from codebase.conf import settings as codebase_settings
    from codebase.base import GitPlatform

    client = codebase_settings.CLIENT
    if client == GitPlatform.GITHUB:
        for name in ("auth_gitlab_url", "auth_gitlab_server_url"):
            self.fields.pop(name, None)
    elif client not in (GitPlatform.GITHUB, GitPlatform.GITLAB):
        for name in ("auth_client_id", "auth_client_secret",
                     "auth_gitlab_url", "auth_gitlab_server_url"):
            self.fields.pop(name, None)
```

**Field-group filtering.** `SiteConfiguration.get_field_groups()` is extended with
a final pass that (a) drops dropped fields from the resolved `FieldGroup.fields`
tuple and (b) omits groups whose `fields` tuple is empty. This prevents rendering an
empty "Authentication" card when `CLIENT=swe`.

**Login page.** Allauth's existing template tags (`{% get_providers %}`,
`{% providers_media_js %}`) drive which buttons render based on `list_apps()`. Since
`list_apps` returns `[]` when unconfigured or when `CODEBASE_CLIENT` doesn't match,
the login page's social button appears if and only if (a) the matching provider is
configured and (b) `CODEBASE_CLIENT` permits it. No template edits.

## First-admin bootstrap

New command `daiv/accounts/management/commands/bootstrap_admin.py`:

```python
from django.core.management.base import BaseCommand, CommandError
from accounts.models import Role, User

class Command(BaseCommand):
    help = "Create the initial admin user on a fresh install."

    def add_arguments(self, parser):
        parser.add_argument("email")

    def handle(self, *args, email, **options):
        if User.objects.filter(role=Role.ADMIN).exists():
            raise CommandError("An admin user already exists. This command only bootstraps the first one.")
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(
                f"A user with email {email} already exists. Promote them via the admin UI instead."
            )
        user = User.objects.create_user(email=email, role=Role.ADMIN)
        self.stdout.write(self.style.SUCCESS(f"Admin user {email} created (pk={user.pk})."))
        self.stdout.write(
            "Log in via /accounts/login/ using login-by-code "
            "(they'll receive a one-time code by email)."
        )
```

**Fresh-install flow:**

1. Operator deploys, runs `manage.py bootstrap_admin admin@example.com`.
2. Admin visits `/accounts/login/`, enters email, receives a 6-digit code by email
   (login-by-code already enabled at `allauth.py:22`).
3. Admin logs in, navigates to `/configuration/`, fills in the Authentication group,
   saves.
4. Normal users can sign in via the configured social provider from then on.

The existing "first social login bootstraps admin" behavior in
`accounts/adapter.py:SocialAccountAdapter.save_user` is **retained** as a secondary
path so upgrading deployments with env-based OAuth already working keep functioning.

**Idempotency / safety:**

- Refuses to run if any admin exists — no accidental privilege escalation.
- Refuses to run if email collides with an existing user — operator resolves manually.
- Email verification is not required (`ACCOUNT_EMAIL_VERIFICATION = "none"`).

## Removals

In `daiv/daiv/settings/components/allauth.py`, lines 38–68 (the `_register_provider`
function and both call sites) are removed. `SOCIALACCOUNT_PROVIDERS` is replaced with
a static scope-only dict:

```python
# Provider scopes are static; credentials, URLs, and enablement come from
# SiteConfiguration via accounts.adapter.SocialAccountAdapter.list_apps().
SOCIALACCOUNT_PROVIDERS = {
    "github": {"SCOPE": ["user:email"]},
    "gitlab": {"SCOPE": ["read_user"]},
}
```

Allauth reads `SCOPE` from settings and apps from `list_apps()`. Credentials and URLs
become purely runtime-configurable; scopes remain a static contract in code.

Imports removed from `allauth.py`: the unused `logger`, `get_docker_secret` (if no
longer referenced — verify at implementation time), and `decouple.config` for
`ALLAUTH_GITLAB_URL` / `_SERVER_URL`.

## Testing

Unit tests only, scoped to new behavior:

- `tests/unit_tests/accounts/test_adapter.py` — `SocialAccountAdapter.list_apps`:
  - Returns `[]` when `CODEBASE_CLIENT=swe`.
  - Returns `[]` when `auth_client_id` is unset.
  - Returns `[]` when asked for a non-matching provider.
  - Returns a populated `SocialApp` when configured for the matching provider.
  - Includes `gitlab_url` / `gitlab_server_url` in `SocialApp.settings` for GitLab;
    empty settings for GitHub.
  - Handles both `SecretStr` and plain `str` for the secret.

- `tests/unit_tests/core/test_forms.py` — `_hide_inapplicable_auth_fields`:
  - `CODEBASE_CLIENT=gitlab` → all four auth fields present.
  - `CODEBASE_CLIENT=github` → GitLab-specific fields dropped, client fields kept.
  - `CODEBASE_CLIENT=swe` → all auth fields dropped.

- `tests/unit_tests/accounts/test_bootstrap_admin.py` — new:
  - Creates admin on empty system.
  - Refuses when any admin exists.
  - Refuses when email collides with an existing user.

Not tested (existing coverage or third-party behavior):

- `EncryptedFieldDescriptor` roundtrip for `auth_client_secret`.
- `SiteSettings` env → DB → default chain for `auth_*` fields.
- Allauth's button-rendering templates.
- The OAuth roundtrip itself.

## Deployment impact

Release notes for existing deployments must call out:

- **Rename env vars:** `ALLAUTH_{GITHUB,GITLAB}_CLIENT_ID` → `ALLAUTH_CLIENT_ID`, and
  the same for `_SECRET`. A one-line change in the Docker secrets manifest.
- `ALLAUTH_GITLAB_URL` and `ALLAUTH_GITLAB_SERVER_URL` unchanged.
- **Fresh installs:** run `manage.py bootstrap_admin <email>` once. Log in via
  login-by-code, then configure OAuth at `/configuration/`.
- Existing deployments that had social login working through env vars keep
  functioning **after the env var rename**, with no DB change required — renamed
  env values flow through `SiteSettings` and take precedence (locking the UI field).

## Open questions

None identified during brainstorming. All design decisions are captured above with
rationale. Implementation should verify at the start:

- That `allauth` accepts an unsaved `SocialApp` instance across the flows we care
  about (login URL resolution, template rendering, OAuth callback lookup). The
  adapter unit tests pin this behavior.
- That `get_docker_secret` and `decouple.config` imports in
  `daiv/daiv/settings/components/allauth.py` have no other references before
  removing them.
