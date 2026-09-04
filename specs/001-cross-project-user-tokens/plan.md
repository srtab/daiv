# Implementation Plan: Cross-Project Git Platform Access Under the Requesting User's Identity

**Branch**: `001-cross-project-user-tokens` | **Date**: 2026-09-03 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-cross-project-user-tokens/spec.md`

## Summary

Give the agent's `gitlab` and `gh` tools an optional `project` argument. Left empty, nothing changes:
the call targets the attached repository under the deployment's service identity, exactly as today.
Set to another project, the call is executed with an OAuth credential belonging to the person the run
acts for, so the git platform's own permission check — not a DAIV-side rule — decides what comes back.

Three pieces of machinery are new: a per-person OAuth credential obtained at sign-in and stored
encrypted; the cross-project branch inside the two CLI runners; and the missing `acting_user_id` hop
that gives webhook-triggered runs an acting person. Everything else already exists — the
platform-user↔DAIV-user mapping, the Fernet field pattern, the `SiteConfiguration` toggle pattern,
and the bot's own self-event recognition.

## Technical Context

**Language/Version**: Python 3.14 only (`requires-python = ">=3.14,<3.15"`)

**Primary Dependencies**: `django==6.1`, `django-allauth[socialaccount]==65.19.1`,
`python-gitlab==8.5.0`, `PyGithub`, `deepagents==0.7.11`, `langchain==1.3.18`, `cryptography`.
**No new dependency is required** — every capability this feature needs is already pinned.

**Storage**: PostgreSQL (Django ORM) for the credential row and audit record; Redis
(`django.core.cache`) for the short-lived per-identity token cache. Secrets encrypted with the
existing Fernet helpers in `daiv/core/encryption.py`.

**Testing**: `pytest` via `make test` (`asyncio_mode = "auto"`; test settings `daiv.settings.test`;
`pythonpath = ["daiv", "."]`, so imports omit the `daiv.` prefix). Unit tests only — the constitution
prohibits re-testing framework behaviour, so allauth's OAuth dance is exercised at DAIV's seam, not
end to end against a live platform.

**Target Platform**: Self-hosted Linux (Docker Swarm / compose); a deployment targets **one** git
platform, selected by `settings.CLIENT`.

**Project Type**: Server-rendered Django web application plus a LangGraph/deepagents agent runtime.

**Performance Goals**: No regression on the attached-project path — it must take the same code path
and make no additional queries. A cross-project call adds at most one credential read (cached) before
the existing 30-second CLI timeout budget.

**Constraints**: The person's credential must never enter agent state, a checkpoint, a log line, a
diff, or published content (FR-012, Principle II). Cross-identity cache reuse is prohibited (FR-013).
The attached-project path must be byte-for-byte unchanged when the feature is off (FR-019).

**Scale/Scope**: One credential row per user per provider; cross-project calls are a minority of tool
calls. Two CLI runners, two webhook callback modules, one settings surface, one account-settings page.

## Constitution Check

*GATE: evaluated before Phase 0 and re-evaluated after Phase 1 design. Result: **PASS with one
justified deviation**, recorded in Complexity Tracking.*

| Principle | Verdict | Evidence |
|---|---|---|
| **I. Self-Hosted Sovereignty** | PASS | The OAuth application is the operator's own, configured through the existing `site_settings.auth_client_id` / `auth_client_secret` the login flow already uses. No maintainer-run service is introduced. |
| **II. Sandboxed Execution & Least-Privilege** | **PASS with deviation** | Strengthened in one respect, strained in another — see Complexity Tracking. Strengthened: "a user MUST NOT be able to act on a repository beyond the access the git platform grants them" becomes enforced by the platform itself rather than by DAIV's mirror. Strained: a per-person OAuth token is inherently wider than a repository-scoped one. Mitigations in D3/D6 of [research.md](./research.md). |
| **III. Platform Parity** | PASS | GitLab and GitHub reach the same capability (FR-017). Asymmetric effort — GitHub needs a user-to-server adapter (D3) — but no functional gap ships. All platform interaction stays behind `daiv/codebase/clients/`. |
| **IV. Green Gates** | PASS | Standard `make lint` + `make test`. No gate is narrowed or skipped. |
| **V. Deterministic, Pinned Environments** | PASS | No dependency added or changed; `pyproject.toml` is not touched. |
| **VI. Failures Are Visible** | PASS | FR-006 and FR-011 mandate a named cause and a next step at the point of failure; FR-006 forbids the silent broader-identity retry that would be the natural shortcut. Refusals reach the agent *and* the reader. |

**Architecture placement** (Additional Constraints): tool changes in the existing
`daiv/automation/agent/middlewares/git_platform.py`; platform interaction stays inside
`daiv/codebase/clients/`; the credential model and its settings page follow the app layout already in
`daiv/accounts/`; the operator toggle joins `SiteConfiguration`. Nothing lands outside the structure
the constitution names.

**Agent state**: the cross-project path returns no `Command(update=...)` carrying a credential —
deliberately, per D6. Tools still never mutate `runtime.state` directly.

## Project Structure

### Documentation (this feature)

```text
specs/001-cross-project-user-tokens/
├── plan.md              # This file
├── spec.md              # Feature specification
├── research.md          # Phase 0 output — 10 decisions, grounded in current code
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── agent-tools.md       # The `project` argument and its refusal vocabulary
│   ├── credential-store.md  # Internal credential-resolution contract
│   └── configuration.md     # Operator-facing settings and OAuth scopes
├── checklists/
│   └── requirements.md  # Spec quality checklist (passing)
└── tasks.md             # Phase 2 — created by /speckit-tasks, NOT by this command
```

### Source Code (repository root)

```text
daiv/
├── accounts/
│   ├── models.py                     # + PlatformCredential (encrypted, per user+provider)
│   ├── migrations/                   # + credential table
│   ├── socialaccount.py              # + GitHub App user-to-server adapter (alongside GitLabServerAwareAdapter)
│   ├── adapter.py                    # SocialAccountAdapter: capture the token on login
│   ├── credentials.py                # NEW — resolve, refresh, revoke; the only reader of the secret
│   ├── views.py                      # + credential status / disconnect on account settings
│   └── templates/                    # + credential status partial
│
├── automation/agent/middlewares/
│   └── git_platform.py               # `project` argument; cross-project branch in both CLI runners;
│                                     #   per-identity token cache; refusal messages
│
├── codebase/
│   ├── context.py                    # RuntimeCtx: acting person is now load-bearing, not MCP-only
│   ├── tasks.py                      # forward acting_user_id into set_runtime_ctx (FR-014)
│   ├── clients/gitlab/api/callbacks.py   # pass the resolved user through; recognise cross-project marker
│   ├── clients/github/api/callbacks.py   # same
│   └── models.py                     # + CrossProjectAccessRecord (FR-016)
│
├── core/
│   └── models.py                     # SiteConfiguration: + cross_project_access_enabled (FR-019)
│
└── daiv/settings/components/
    └── allauth.py                    # SOCIALACCOUNT_PROVIDERS scopes (FR-007)

tests/unit_tests/
├── automation/agent/middlewares/test_git_platform.py   # extend: project arg, identity selection, refusals
├── accounts/test_credentials.py                        # NEW — resolve/refresh/revoke/expiry
├── codebase/test_cross_project_authorization.py        # NEW — the FR-005 leak tests
└── codebase/clients/…/test_callbacks.py                # extend: acting_user_id hop, self-marker

docs/                                  # operator-facing: OAuth setup, scopes, re-consent, the toggle
```

**Structure Decision**: no new Django app and no new top-level directory. The feature is three edits
to existing seams — the CLI runners in `automation/agent/middlewares/`, the account/credential surface
in `accounts/`, and the webhook→context hop in `codebase/` — plus one settings field. A new app would
add an import boundary between the credential and the two places that read it without separating
anything that is actually independent.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| **Principle II** — a per-person OAuth credential is broader than the repository-scoped, short-lived token the principle asks for | It is the feature. Binding calls to the person is the only way the git platform can apply *their* permissions; a repository-scoped token cannot answer "what may this person see elsewhere". The blast radius is bounded by what the person could already do by hand. | *Repo-scoped ephemeral tokens per target project* — DAIV can only mint these for projects it is installed on, which is the opposite of the cross-project case (D8). *Gating on the `RepositoryAccess` mirror* — its universe is bot-visible repos only, so it denies exactly the projects this feature exists to reach, and it is a second permission model the spec rules out. |
| **New persistent secret store** (`PlatformCredential`) | FR-002 needs a credential at tool-call time, long after the request that obtained it. Nothing in the repo stores per-user platform tokens today. | *`SOCIALACCOUNT_STORE_TOKENS=True`* — one line, but plaintext at rest, no revocation state for FR-008, and allauth recreates the row on re-login. *Pasted PATs* — ruled out during clarification. |
| **Two identities live in one run** (service identity for the attached project, acting person beyond it) | The direct consequence of the chosen answer to the attached-project question: today's authorship, publishing path, and bot self-recognition all stay untouched. | *One identity everywhere* — was evaluated and rejected by the requester; it changes who authors every comment and merge request and re-opens the self-comment loop as a P1 risk. |

**Accepted consequence, not a violation**: because two identities coexist, the audit record (FR-016)
is written **per call**, not per run. A per-run record could not answer "under whose identity was this
project reached", which is the question SC-007 asks.
