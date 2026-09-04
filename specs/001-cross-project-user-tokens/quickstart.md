# Quickstart: Validating Cross-Project Access

**Date**: 2026-09-03 | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

How to prove the feature works end to end. Scenarios map to the spec's success criteria; run them in
order — the negative cases matter more than the positive one.

## Prerequisites

- A DAIV deployment with `settings.CLIENT` set, and its OAuth application configured per
  [contracts/configuration.md](./contracts/configuration.md).
- `SiteConfiguration.cross_project_access_enabled` on.
- **Project A** — attached to the run, DAIV installed.
- **Project B** — a different project the test person *can* read.
- **Project C** — a different project the test person *cannot* read. Borrow one from a colleague; do
  not create it under the test account, or it proves nothing.

## Unit tests

```bash
make test
```

```bash
uv run pytest tests/unit_tests/automation/agent/middlewares/test_git_platform.py tests/unit_tests/accounts/test_credentials.py tests/unit_tests/codebase/test_cross_project_authorization.py
```

Lint gate (CI runs it before tests):

```bash
make lint
```

## Scenario 1 — Consent (SC-003)

Sign in as a user with no credential. Confirm the consent screen names what DAIV will reach, and that
account settings then shows the credential connected with its expiry. Time the whole path: sign-in to
first successful cross-project answer must be under 3 minutes.

## Scenario 2 — Read across projects (SC-001, FR-001, FR-002)

Start a run on project A as that person. Ask something answerable only from project B's issues or
merge requests. Confirm the answer carries information that is not in project A, and that the audit
record names project B and the person.

## Scenario 3 — Refusal, and no leak (SC-002, FR-005, FR-006) — the important one

Same run, ask about **project C**. Confirm:

1. The tool returns the platform-denied refusal naming project C.
2. **No project C content appears anywhere** — agent answer, transcript, produced diff, published
   comment. Grep the run's stored messages, do not eyeball the reply.
3. **No retry under the service identity.** The audit record shows one `denied_*` row for project C
   and no `service` row for it. This is the leak the whole feature guards against; a passing eyeball
   check is not evidence.

## Scenario 4 — Attached project is untouched (SC-005, FR-003)

Run the normal issue-addressing flow on project A with no `project` argument. Confirm the resulting
comment and merge request are authored by the **service identity**, exactly as before, and the run
succeeds identically. Then turn the capability off and confirm the `project` argument is gone from
the tool schema and the flow is unchanged (FR-019).

## Scenario 5 — Credential lifecycle (FR-008 to FR-011)

- **Expired**: age the credential past `expires_at` with a dead refresh token. Cross-project is
  refused with the expired message; attached-project work still succeeds.
- **Revoked**: revoke from account settings. Same shape, revoked message, secrets cleared.
- **Never authorised**: a pre-existing user who has not re-consented keeps full attached-project
  capability and gets the no-credential message on a cross-project attempt (FR-010).

Each message must name the cause and the next step without reading logs (SC-004).

## Scenario 6 — Webhook-triggered run (FR-014)

Add a bot label to an issue in project A **as a platform user with a linked DAIV account and a
credential**. Confirm the run reaches project B. Repeat as a platform user with no linked account:
the run completes inside project A and states that cross-project access was unavailable.

## Scenario 7 — No self-triggered runs (SC-006, FR-015)

Have the agent publish a comment in project B (DAIV-watched) while acting for a person. Confirm no
new run starts. Then have that person write a comment there themselves and confirm normal handling —
the marker must suppress DAIV's own output, not that person's.

## Scenario 8 — Concurrent identities (FR-013)

Two people run against project A at once, one with access to project B and one without. Confirm the
second is refused. Then resume a thread as a *different* acting person and confirm the first person's
token is not reused — the cache is keyed on identity, not thread.

## What "done" looks like

- [ ] `make lint` and `make test` green
- [ ] Scenarios 1–8 pass on both a GitLab and a GitHub deployment (SC-008)
- [ ] Audit records answer "which projects, under whose identity" for every run above (SC-007)
- [ ] `docs/` and `CHANGELOG.md` updated, including the re-consent upgrade note
