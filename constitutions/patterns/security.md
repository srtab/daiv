# Security

DAIV runs an agent against other people's repositories with their credentials. Every rule
here fails closed: when the configuration a check needs is missing, the request is refused.

## Rules

1. **A shell exists only in a sandbox.** The agent gets a `bash` tool only when its
   `Workspace` has a sandbox session; a disk run has none. No code path runs
   repository-supplied commands, scripts or hooks on the DAIV host. _Prevents_: untrusted
   repository code running with DAIV's credentials and network.
2. **Egress is deny-by-default.** A sandbox environment without an `egress_policy` runs
   network-isolated (`SandboxEnvironment.is_networked`). Outbound access exists only through
   the environment's allow-list, enforced by the egress proxy; there is no unrestricted mode.
   DAIV adds the repository's own git platform at runtime so a run can clone and publish, and
   every other host is the operator's to allow. _Prevents_: an agent exfiltrating code or
   secrets, or fetching what the operator never approved.
3. **Least privilege, shortest life.** Git credentials are minted per repository where the
   platform allows it (GitLab project access tokens,
   `daiv/codebase/clients/gitlab/clone_tokens.py`), and a publish mints a fresh one
   (`Workspace.authenticated_git`). A new integration requests the narrowest scope that works.
   _Prevents_: a credential copied out of a workspace reaching the full API or other projects.
4. **Secrets stay out of every output.** No secret reaches a log line, a Sentry event, an
   agent transcript, a tool result the agent sees, an API response or a rendered page.
   _Prevents_: credential disclosure through diagnostics or the agent itself.
5. **Authenticate before parsing.** Webhooks verify the token or signature before reading the
   payload, and an unset secret rejects every request; the `webhooks.E001` and `webhooks.E002`
   system checks fail startup until the active platform's secret is set. A new inbound path
   follows the same pattern. _Prevents_: an unauthenticated request triggering git-platform
   API calls or an agent run.
6. **Access mirrors the platform.** A user sees a repository only with platform read access,
   and starts runs, chats or schedules on it only with write access
   (`daiv/codebase/authorization.py`); admins are exempt. A denied repository answers as not
   found. Every new entry point that names a repository checks access before acting.
   _Prevents_: users listing, or running the agent against, repositories they cannot reach on
   the platform.
