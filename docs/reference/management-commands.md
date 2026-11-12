---
description: "Reference for every django-admin management command DAIV ships: what each one does, whether and when it runs on its own, its options and an example run inside the container."
---

# Management commands

DAIV ships twelve `django-admin` commands for setup and operator repairs. Run them inside the app container:

```bash
docker compose exec app django-admin <command>
```

Four of the twelve run on their own, so a manual run is the exception:

| Command | Runs on its own |
|---------|-----------------|
| [`setup_default_site`](#setup_default_site) | On every container start |
| [`setup_checkpoint_saver`](#setup_checkpoint_saver) | On every container start |
| [`sync_stuck_runs`](#sync_stuck_runs) | Every 5 minutes |
| [`setup_webhooks`](#setup_webhooks) | On the `CODEBASE_WEBHOOK_SETUP_CRON` schedule, on GitLab only |

The other eight run only when you run them.

## `archive_sessions`

Copies the live checkpoint of every session that has no archive into the session archive, so the transcript outlives the checkpointer TTL. Run it once after upgrading to the release that archives sessions, before those checkpoints expire; the upgrade note of that release in `CHANGELOG.md` says so. Running it again retries the sessions that failed. It does not run on its own.

This command takes no options.

```bash
docker compose exec app django-admin archive_sessions
```

## `backfill_memory_entries`

Rebuilds a repository's memory entries by replaying its historical consolidated observations through the consolidation operations pass, oldest first. It makes LLM calls. Running it again replays only the observations that no entry covers. It ignores the memory enabled flags, because it is an operator repair action. It does not run on its own.

!!! warning
    `--reset-document` clears the repository's stored memory document before replaying. DAIV keeps the old text only in a WARNING log line, so copy it from the command's output or your log pipeline if you may need it.

| Option | Description |
|--------|-------------|
| `--repo-id` | Repository ID to backfill, for example `group/project`. Required. |
| `--batch-size` | Observations per operations round, from 1 to 50. Default: `20`. |
| `--reset-document` | Clear the stored memory document before replaying. Use it for a repository whose document no replay can reconstruct. |

```bash
docker compose exec app django-admin backfill_memory_entries --repo-id group/project
```

## `bootstrap_admin`

Creates the initial admin user when no admin exists yet. The command refuses to run if an admin exists, or if a user with that email exists. The new admin signs in with a one-time code sent by email; see [Deployment](../getting-started/deployment.md). It does not run on its own.

| Argument | Description |
|----------|-------------|
| `email` | Email address of the new admin user. |

```bash
docker compose exec app django-admin bootstrap_admin admin@example.com
```

## `consolidate_memory`

Consolidates the pending memory observations of one repository into its memory document, in the foreground. The shell waits for the consolidation model's LLM call, and the call is billed like any other. DAIV's own hourly sweep does not use this command; it enqueues consolidation for the repositories that are due. This command does not run on its own.

Without `--force`, the command refuses to run while fewer than 10 observations are pending.

| Option | Description |
|--------|-------------|
| `--repo-id` | Repository ID to consolidate, for example `group/project`. Required. |
| `--force` | Consolidate even below the pending-observation threshold. |

```bash
docker compose exec app django-admin consolidate_memory --repo-id group/project
```

## `create_api_key`

Creates an API key for a user, for the [Jobs API](../features/jobs-api.md) and other API clients. The key is printed once, on creation. It does not run on its own.

A time in `--expires-at` without an offset is read in `Europe/Lisbon`, not in the zone of your server or shell; see [Time zone](env-variables.md#time-zone). Give an explicit offset to avoid the ambiguity.

| Argument / option | Description |
|-------------------|-------------|
| `username` | Username of the user to create the key for. |
| `--name` | Name of the key. Default: empty. |
| `--expires-at` | Expiry in ISO 8601 format, for example `2026-12-31T23:59:59+00:00`. Default: the key does not expire. |

```bash
docker compose exec app django-admin create_api_key <username> --name "my-key" --expires-at 2026-12-31T23:59:59+00:00
```

Replace `<username>` with the username of the key's owner.

## `export_session_archives`

Writes each archived session's contexts and runs to `<output>/<thread_id>.json`, for offline analysis. See [Session archive](monitoring.md#session-archive) for what an export holds. It does not run on its own.

The exports hold full conversations and run prompts. Inside the app container, a relative `output` resolves in the container, so the files never reach the host on their own. Write them to a temporary directory, copy them out, and delete the directory in the container.

| Argument / option | Description |
|-------------------|-------------|
| `output` | Directory to write the files to. DAIV creates it if missing. |
| `--since` | Only sessions active on or after this date, as `YYYY-MM-DD`. |
| `--repo` | Only sessions on this repository, for example `group/project`. |

```bash
docker compose exec app django-admin export_session_archives /tmp/session-exports --since 2026-10-01 --repo group/project
docker compose cp app:/tmp/session-exports ./session-exports
docker compose exec app rm -rf /tmp/session-exports
```

## `release_orphan_queued_sessions`

Releases queued runs whose session has no active sibling (a ready or running run). It mitigates a rare race where the dispatcher missed a terminal transition, or a run was created queued and never picked up; a sessions log message points here when that happens. It does not run on its own.

This command takes no options.

```bash
docker compose exec app django-admin release_orphan_queued_sessions
```

## `setup_checkpoint_saver`

Initializes the LangGraph Redis checkpointer indices. It runs on every container start, so run it by hand only after you reset the checkpointer's Redis store while DAIV is running.

This command takes no options.

```bash
docker compose exec app django-admin setup_checkpoint_saver
```

## `setup_default_site`

Updates the default Django `Site` domain from `DAIV_EXTERNAL_URL`, so sign-in emails link to the right host. It runs on every container start; run it by hand after you change `DAIV_EXTERNAL_URL` without restarting.

This command takes no options.

```bash
docker compose exec app django-admin setup_default_site
```

## `setup_langsmith_dashboard`

Creates the LangSmith custom dashboard **DAIV Monitoring**, with the monitoring charts for DAIV. See [Monitoring](monitoring.md#custom-dashboard). It needs `LANGSMITH_API_KEY` set in the container. It does not run on its own.

!!! warning
    `--recreate` deletes the existing **DAIV Monitoring** dashboard, including any chart you added to it by hand, and creates it again.

| Option | Description |
|--------|-------------|
| `--project` | LangSmith project name. Default: the `LANGCHAIN_PROJECT` environment variable, then `LANGSMITH_PROJECT`, then `default`. |
| `--recreate` | Delete the existing dashboard and create it again. Without it, the command fails when the dashboard exists. |

```bash
docker compose exec app django-admin setup_langsmith_dashboard --project <project-name>
```

Replace `<project-name>` with your LangSmith project.

## `setup_webhooks`

Sets the DAIV webhook on every repository DAIV can access, so issue and merge request events reach the instance.

This command is GitLab only. GitHub webhooks are set in the GitHub App's configuration, so on GitHub the command logs a warning and returns; see [Platform setup](../getting-started/platform-setup.md). On GitLab it also runs on its own on the `CODEBASE_WEBHOOK_SETUP_CRON` schedule, so run it by hand only to add webhooks right away, or with `--update` to change the existing ones.

!!! warning
    `--disable-ssl-verification` makes GitLab post webhook payloads, with the secret token in the `X-Gitlab-Token` header, without checking DAIV's certificate. Use it for local setups with self-signed certificates only. The scheduled run sets it only when `DEBUG` is on.

!!! warning
    A token passed with `--secret-token` lands in your shell history and shows in the process list while the command runs. Set `CODEBASE_GITLAB_WEBHOOK_SECRET` instead, and keep the flag for one-off overrides.

| Option | Description |
|--------|-------------|
| `--base-url` | Base URL of DAIV. Default: `DAIV_EXTERNAL_URL`. |
| `--repo-id` | Only set up this repository ID. |
| `--secret-token` | Secret token for webhook validation. Overrides `CODEBASE_GITLAB_WEBHOOK_SECRET`. |
| `--disable-ssl-verification` | Tell GitLab not to verify DAIV's SSL certificate for the webhook. |
| `--update` | Update existing webhooks as well as creating missing ones. |

```bash
docker compose exec app django-admin setup_webhooks --update
```

## `sync_stuck_runs`

Reconciles runs that are not in a terminal state:

- Task-backed runs are re-synced from their linked task result.
- Inline chat runs that a worker crash left running are failed once their session heartbeat goes stale. Task-backed job and inbox runs in the same state are failed the same way.
- Ready runs whose submitter stopped before queueing their task are failed once they are old enough.

It runs on its own every 5 minutes, so run it by hand only to reconcile right away.

This command takes no options.

```bash
docker compose exec app django-admin sync_stuck_runs
```
