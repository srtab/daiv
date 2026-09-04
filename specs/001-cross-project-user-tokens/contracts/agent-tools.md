# Contract: Agent Tool Surface (`gitlab`, `gh`)

**Consumers**: the LLM agent. **Owner**: `daiv/automation/agent/middlewares/git_platform.py`.

The tools' externally visible contract is their argument schema and the strings they return. Both are
model-facing, so wording is part of the contract: the agent decides its next move from these strings.

## New argument

Added to **both** tools, and present **only** when `SiteConfiguration.cross_project_access_enabled`
is on (FR-019).

| Argument | Type | Default | Meaning |
|---|---|---|---|
| `project` | `str` | `""` | The project to target. Empty ⇒ the attached repository, under the service identity (FR-003, FR-004). A different project ⇒ that project, under the acting person's identity (FR-002). |

Description supplied to the model must state: the default targets the current project; naming another
project means the call is made as the requesting person and sees only what they can see; a project
they cannot access is refused rather than silently empty.

**Unchanged**: `subcommand` still rejects `--project-id` (GitLab) and `--repo` / `-R` / `--hostname`
(GitHub); the GitHub `api` resource stays off the allowlist; `output_mode`, `output_to_file`, the
30-second timeout, the allowlist policy, and the large-result eviction all behave as they do today
(FR-018).

## Validation of `project`

Rejected before any credential is read or any subprocess is spawned. `argv` is passed to
`create_subprocess_exec` without a shell, so the risk is flag confusion, not quoting.

| Condition | Result |
|---|---|
| Empty after strip | Attached-project path (not an error) |
| Starts with `-` | `error: The project must be a project path, not a flag.` |
| Contains whitespace or control characters | `error: The project must be a single project path (e.g. 'group/name').` |
| Equal to the attached repository | Attached-project path — the identity does not switch |

## Identity selection

```text
project empty or == attached repo  ─▶ service identity   (GITLAB_PRIVATE_TOKEN / GitHub App installation token)
project names another repository   ─▶ acting person's credential
                                       └─ absent / expired / revoked / insufficient ─▶ refuse (below)
```

The service identity is **never** used for a project other than the attached one, and a refused
cross-project call is never retried under it (FR-006). This is the single most important line in
this contract: the natural "fall back so the agent gets an answer" instinct is precisely the leak
User Story 2 forbids.

## Refusal vocabulary

Every refusal names the project and the cause, and is returned to the agent as a normal tool result
(not an exception) so the run continues with what it can do (Principle VI). All begin `error: ` to
match the existing convention that `test_git_platform.py` asserts on.

| Cause | Returned string |
|---|---|
| Capability off | `error: Cross-project access is not enabled on this DAIV deployment. Only <attached repo> can be reached.` |
| No acting person (FR-014 fallback) | `error: This run has no requesting user, so only <attached repo> can be reached. Cross-project access needs a run started by a signed-in user, or by a platform user with a linked DAIV account.` |
| No credential | `error: <person> has not authorised DAIV to access other projects on <provider>. They can authorise it from their DAIV account settings.` |
| Expired, refresh failed | `error: <person>'s <provider> authorisation has expired and could not be renewed. They need to re-authorise from their DAIV account settings.` |
| Revoked | `error: <person>'s <provider> authorisation was revoked. They need to re-authorise from their DAIV account settings.` |
| Identity not linked | `error: This run was attributed to <person> by name, but the <provider> account that triggered it is not linked to their DAIV account, ...` |
| Insufficient scope | `error: <person>'s <provider> authorisation does not permit this operation on <project>. Re-authorising from DAIV account settings will request the required access.` |
| Platform denied (404/403) | `error: <project> is not accessible to <person> on <provider>. It may not exist, or they may not have access.` |
| Wrong host | `error: <project> is not on <host>, which is the only platform this deployment is configured for.` |

**Deliberate ambiguity**: the platform-denied string does not distinguish "does not exist" from "you
may not see it". That mirrors what the platform itself returns and stops the tool becoming an
existence oracle for private projects — the spec's edge case.

**No credential material** appears in any string, and platform stderr is not echoed verbatim on the
cross-project path where it could carry token fragments or repository names the person cannot see.

## Result shape

Success is indistinguishable in shape from an attached-project call: the same inline text, the same
`output_to_file` confirmation, the same automatic eviction of oversized results. A cross-project call
returns **no** `Command(update=...)` carrying credential material — the acting person's token never
enters agent state (FR-012, D6).

## Observability

Every cross-project attempt writes one `CrossProjectAccessRecord` (FR-016), allowed or refused,
before returning. A failure to write the record is logged and does not fail the call — but a call
that was *refused* must still be recorded, because a pattern of refusals is exactly what an auditor
is looking for.
