# Contract: Operator Configuration

**Consumers**: operators deploying DAIV. **Owners**: `daiv/core/models.py` (SiteConfiguration),
`daiv/daiv/settings/components/allauth.py` (scopes), `docs/`.

## Toggle

| Setting | Default | Effect |
|---|---|---|
| `SiteConfiguration.cross_project_access_enabled` | `False` | Off ⇒ the `project` argument is absent from both tool schemas and the cross-project branch is unreachable. Attached-project behaviour is byte-for-byte today's (FR-019). |

Default-off is deliberate: an upgrade must not silently widen what the agent can reach.

## OAuth scopes

Today (identity only — the reason every existing user must re-consent, FR-010):

```python
SOCIALACCOUNT_PROVIDERS = {"github": {"SCOPE": ["user:email"]}, "gitlab": {"SCOPE": ["read_user"]}}
```

**GitLab** — request `read_user` plus `api`. Operator-configurable, with `read_api` documented as the
narrower option for deployments that want read-only cross-project context (D4). Access tokens are
short-lived (2 hours by default) **with refresh-token rotation**, so refresh is mandatory, not
optional.

**GitHub** — the App's user-to-server flow, reusing the existing `GITHUB_APP_ID` /
`GITHUB_PRIVATE_KEY` credentials (D3). GitHub Apps **ignore the `scope` parameter**: reach is the
intersection of the person's own permissions and the App's installed permissions, which is tighter
than a classic OAuth `repo` scope. The operator must grant the App read on issues, pull requests, and
actions. Whether "Expire user authorization tokens" is enabled changes only whether refresh tokens
are issued; both configurations must work.

## Existing settings this feature reuses

| Setting | Role |
|---|---|
| `site_settings.auth_client_id` / `auth_client_secret` | The operator's own OAuth application — already used by login. No second credential pair. |
| `settings.CLIENT` | One platform per deployment. A `project` on another platform is refused (see the wrong-host refusal). |
| `DAIV_ENCRYPTION_KEY` | Already governs `Provider` secrets; now also the credential store. **Rotating it invalidates stored credentials** — everyone re-authorises. This must be in the docs. |

## Documentation obligations (Constitution: Documentation, and FR-017)

The change that ships this must also update `docs/` with: how to configure the OAuth application on
each platform, which scopes/permissions to grant, that existing users must re-consent and what they
see until they do, the encryption-key rotation consequence, and any GitLab/GitHub capability gap that
could not be closed. `CHANGELOG.md` carries an upgrade note — re-consent is a visible behaviour change
for every existing user.
