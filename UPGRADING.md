# Upgrading

This guide lists what you must do to upgrade across a release with breaking changes. Each
section covers one upgrade; [`CHANGELOG.md`](CHANGELOG.md) lists every change.

## Upgrade from 2.0.0

DAIV now runs every agent run in the sandbox, adds a web dashboard with sign-in, and moves
most configuration from environment variables into the database. Do these steps in order.

### Before you upgrade

1. Back up PostgreSQL and Redis. Several migrations cannot be reversed, so going back to
   2.0.0 means restoring the backup.
2. Let the worker finish every queued task, then stop it. Task names changed, so a task that
   2.0.0 queued does not run after the upgrade.
3. Decide on `DAIV_ENCRYPTION_KEY`. The first start encrypts the provider API keys, and later
   the other secrets you enter in the dashboard, with this key, or with a key derived from
   `DJANGO_SECRET_KEY` when it is unset. Changing either key afterwards makes those secrets
   unreadable.
4. Upgrade daiv-sandbox to 0.5.0 or later, and give it an egress CA through
   `DAIV_SANDBOX_EGRESS_CA_CERT_FILE` and `DAIV_SANDBOX_EGRESS_CA_KEY_FILE`, as in steps 2 and
   3 of [Deploy with Docker Swarm](https://docs.daivagent.com/dev/getting-started/deployment/#deploy-with-docker-swarm).
   Every run fails without the CA, because git traffic goes through the egress proxy.
   Multi-repository sessions, and stopping a bash command that timed out, need a daiv-sandbox
   release after 0.5.0. Setting `DAIV_SANDBOX_REDIS_URL` on the sandbox turns on its session
   locks and idle reaping.
5. Run Redis 8 or later, with the agent's checkpoints on database `0`: leave
   `DJANGO_REDIS_CHECKPOINT_URL` unset, or point it at `/0`.

### Update the deployment

Compare your stack or Compose file with the samples in
[Deployment](https://docs.daivagent.com/dev/getting-started/deployment/), then:

- Remove the `mcp-proxy` service and its secrets, and revoke the DAIV API key it used.
- Run the scheduler, one replica, and the sandbox. Both are required.
- Mount one shared volume at `/home/daiv/data/media` on the `app` container and every
  worker, for run artifacts and tool files.
- Optionally, add a worker that runs `sh /home/daiv/start-worker interactive`, and pass
  `default` to the existing worker, so titles and notifications don't wait behind agent runs.
- Raise the open-files limit (`nofile`) to 65536, as the samples do.
- Serve DAIV over HTTPS: the dashboard's sign-in cookie is `Secure`.
- Set the `EMAIL_*` variables so DAIV can send sign-in codes and notifications.

### Update environment variables

DAIV ignores a removed variable without a warning, so check each one:

| 2.0.0 variable | Action |
|---|---|
| `CODEBASE_GITLAB_WEBHOOK_SECRET` or `CODEBASE_GITHUB_WEBHOOK_SECRET` | Set the one for your platform. Without it, the `app` container fails to start and every webhook gets `401`. |
| `AUTOMATION_WEB_SEARCH_*` | Rename to `DAIV_WEB_SEARCH_*`. |
| `AUTOMATION_WEB_FETCH_*` | Rename to `DAIV_WEB_FETCH_*`. `AUTOMATION_WEB_FETCH_PROXY_URL` keeps its name. |
| `DIFF_TO_METADATA_MODEL_NAME`, `DIFF_TO_METADATA_FALLBACK_MODEL_NAME` | Rename to `DAIV_DIFF_TO_METADATA_MODEL_NAME` and `DAIV_DIFF_TO_METADATA_FALLBACK_MODEL_NAME`. |
| `DAIV_AGENT_EXPLORE_MODEL_NAME` | Replace with `DAIV_AGENT_SUBAGENT_MODELS='{"explore": "<model>"}'`. DAIV still reads it for now, and `django-admin check` warns with `core.W001`. |
| `DAIV_AGENT_THINKING_LEVEL` or `DAIV_AGENT_MAX_THINKING_LEVEL` set to `None` | Set `off` instead. |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY` | Keep them on the `app` container for the first start, which copies them into the database. Change them later under **Configuration → Providers**. |
| `OPENROUTER_API_BASE` | Set the base URL under **Configuration → Providers → OpenRouter**. |
| `DAIV_SANDBOX_BASE_IMAGE`, `DAIV_SANDBOX_CPU`, `DAIV_SANDBOX_MEMORY`, `DAIV_SANDBOX_NETWORK_ENABLED`, `DAIV_SANDBOX_EPHEMERAL` | Remove. Set the image, CPUs, memory and network on the **Default** sandbox environment instead. |
| `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` | Check `git` entries: the first argument must be the git subcommand, so `git -c …` no longer blocks anything. |
| `DAIV_SANDBOX_COMMAND_POLICY_ALLOW` | Remove. It has no effect. |
| `MCP_PROXY_*`, `MCP_SENTRY_*`, `MCP_CONTEXT7_*` | Remove. Configure the `sentry` and `context7` servers under **MCP servers** after the upgrade. |
| `DAIV_AGENT_DOCS_RESEARCH_MODEL_NAME` | Remove. |
| `CODEBASE_CLIENT=swe` | Use `gitlab` or `github`. |

A setting you leave unset takes the new default, including the default models, which all run
through OpenRouter. Without an OpenRouter key, set every model setting listed in
[Environment variables](https://docs.daivagent.com/dev/reference/env-variables/). A setting you
set in the environment overrides the dashboard and locks its field there.

### Update `.daiv.yml`

- DAIV ignores the `sandbox:` block. Create a sandbox environment for a repository that needs
  its own image, resources or network, and assign the repository to it; see
  [Sandbox environments](https://docs.daivagent.com/dev/features/sandbox-environments/). A
  repository can no longer turn the sandbox off.
- Move `sandbox.command_policy.disallow` entries to `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW`,
  which applies to every repository. Per-repository `allow` entries have no replacement.
- Memory, pipeline watch, orchestration, merge request session links and the `AGENTS.md`
  suggestion are on by default. Turn them off per repository with `memory.enabled`,
  `pipeline_watch.enabled`, `orchestration.enabled`, `session_link` or `suggest_context_file`.

### Update the git platform

On GitLab:

1. After the first start, run `django-admin setup_webhooks --update`, so existing project
   webhooks get the secret and the merge request and pipeline events.
2. DAIV clones and pushes with short-lived project access tokens at the Developer role. Let
   Developers push to the branches DAIV creates. Minting these tokens needs the DAIV user to
   be a Maintainer, and on GitLab.com the Premium or Ultimate tier; without them, DAIV falls
   back to its personal access token.

On GitHub, in the GitHub App's settings:

1. Set the webhook secret to the value of `CODEBASE_GITHUB_WEBHOOK_SECRET`.
2. Add the **Actions: Read-only** permission, and **Workflows: Read and write** if DAIV should
   change files under `.github/workflows/`. Each installation owner must accept the new
   permissions.
3. Subscribe to the **Pull request**, **Pull request review comment** and **Workflow run**
   events.

See [Platform setup](https://docs.daivagent.com/dev/getting-started/platform-setup/).

### After the first start

1. Sign in to the dashboard. Every user from 2.0.0 becomes an admin, so `bootstrap_admin` is
   not needed: sign in with an emailed code, which needs the user's email address, or with
   GitLab or GitHub; see
   [Accounts and roles](https://docs.daivagent.com/dev/getting-started/accounts/).
2. Under **Users**, turn users who should not administer DAIV into members. A member reaches
   only the repositories their linked GitLab or GitHub account can reach.
3. Every user with an email address receives notification emails about their runs that found
   issues, need attention or failed.
4. Under **MCP servers**, add an `Authorization: Sentry-Bearer <token>` header to the `sentry`
   server and enable it; see [MCP tools](https://docs.daivagent.com/dev/customization/mcp-tools/).
   The `context7` server is on, without a key.
5. Recreate any custom sandbox image, resources or network access on the **Default** sandbox
   environment. Its image, `ghcr.io/astral-sh/uv:python3.14-bookworm`, is about 1.1 GB; pull
   it on the sandbox host ahead of the first run.

### Update API clients

- `POST /api/chat/completions` no longer speaks the OpenAI chat-completions format, and it
  counts against `DAIV_JOBS_THROTTLE_RATE`. `GET /api/models` is gone. Drive DAIV from code
  with the [Jobs API](https://docs.daivagent.com/dev/features/jobs-api/) or the
  [MCP endpoint](https://docs.daivagent.com/dev/features/mcp-endpoint/).
- `GET /api/automation/mcp-proxy/config/` is gone.
- Worker logs come from the `django.tasks` logger instead of `django_tasks`, and commands
  blocked by `DAIV_SANDBOX_COMMAND_POLICY_DISALLOW` are reported as `global_disallow`
  instead of `repo_disallow`. Update log queries and alerts that match the old names.
