---
description: "Reference for every django-admin management command DAIV ships: what each one does, when to run it, its options and an example run inside the container."
---

# Management Commands

DAIV ships a set of `django-admin` commands for setup and operator repairs. Run them inside the app container:

```bash
docker compose exec app django-admin <command>
```

Most run automatically (on container start or on a schedule); the rest exist for one-off setup and for the repairs the log messages point at.

## archive_sessions

Copy the live checkpoint of every session without an archive into it, so the transcript outlives the checkpointer TTL. Run it once after migrating (sessions 0014), before those checkpoints expire. Running it again retries the sessions that failed.

This command takes no options.

```bash
docker compose exec app django-admin archive_sessions
```

## backfill_memory_entries

Rebuild a repository's memory entries by replaying its historical consolidated observations through the consolidation operations pass, oldest first. It makes LLM calls. Safe to re-run: only observations not yet linked to an entry are replayed. It ignores the memory enabled flags — this is an operator repair action.

| Option | Description |
|--------|-------------|
| `--repo-id` | Repository ID to backfill (e.g. `group/project`). Required. |
| `--batch-size` | Observations per operations round, 1–50 (default: 20). |
| `--reset-document` | Clear the stored memory document before replaying. The escape hatch for a repository whose document no replay can reconstruct. Destructive. |

```bash
docker compose exec app django-admin backfill_memory_entries --repo-id group/project
```

## bootstrap_admin

Create the initial admin user when no admin exists yet. The user signs in by login code; see [Deployment](../getting-started/deployment.md).

| Argument | Description |
|----------|-------------|
| `email` | Email address for the new admin user. |

```bash
docker compose exec app django-admin bootstrap_admin admin@example.com
```

## consolidate_memory

Consolidate pending memory observations into the repository memory document. Consolidation also runs on a schedule once enough observations are pending; run this to consolidate one repository on demand.

| Option | Description |
|--------|-------------|
| `--repo-id` | Repository ID to consolidate (e.g. `group/project`). Required. |
| `--force` | Consolidate even below the pending-observation threshold. |

```bash
docker compose exec app django-admin consolidate_memory --repo-id group/project
```

## create_api_key

Create an API key for a user, for the [Jobs API](../features/jobs-api.md) and other API clients. The key is printed once, on creation.

| Argument / option | Description |
|-------------------|-------------|
| `username` | Username of the user to create the API key for. |
| `--name` | Name for the API key (default: empty). |
| `--expires-at` | Expiration date for the API key in ISO 8601 format (e.g. `2026-12-31T23:59:59`). |

```bash
docker compose exec app django-admin create_api_key <username> --name "my-key"
```

## export_session_archives

Write each archived session's contexts and runs to `<output>/<thread_id>.json`, for offline analysis. See [Monitoring](monitoring.md#session-archive) for what an export holds and why the output is sensitive.

| Argument / option | Description |
|-------------------|-------------|
| `output` | Directory to write the files to; created if missing. |
| `--since` | Only sessions active on or after this date (`YYYY-MM-DD`). |
| `--repo` | Only sessions on this repository, e.g. `group/project`. |

```bash
docker compose exec app django-admin export_session_archives ./session-exports --since 2026-10-01 --repo group/project
```

## release_orphan_queued_sessions

Release QUEUED runs whose session has no active (READY/RUNNING) sibling. This mitigates a rare race where the dispatcher missed a terminal transition, or a run row was created QUEUED but never picked up; a sessions log message points here when it happens.

This command takes no options.

```bash
docker compose exec app django-admin release_orphan_queued_sessions
```

## setup_checkpoint_saver

Initialize the LangGraph Redis checkpointer indices. Run it when the checkpointer is first provisioned, or after its Redis store is reset.

This command takes no options.

```bash
docker compose exec app django-admin setup_checkpoint_saver
```

## setup_default_site

Update the default Django `Site` domain from `DAIV_EXTERNAL_URL`, so sign-in emails link to the right host. It runs on every container start; run it by hand after changing `DAIV_EXTERNAL_URL` outside a restart.

This command takes no options.

```bash
docker compose exec app django-admin setup_default_site
```

## setup_langsmith_dashboard

Create the LangSmith custom dashboard with the monitoring charts for DAIV. See [Monitoring](monitoring.md).

| Option | Description |
|--------|-------------|
| `--project` | LangSmith project name (default: from the `LANGCHAIN_PROJECT` or `LANGSMITH_PROJECT` environment variable). |
| `--recreate` | Delete the existing dashboard and recreate it from scratch. |

```bash
docker compose exec app django-admin setup_langsmith_dashboard --project <project-name>
```

## setup_webhooks

Set the DAIV webhook on every repository DAIV can access, so issue and merge/pull request events reach the instance. Run it after connecting a platform, and again with `--update` when the base URL changes.

| Option | Description |
|--------|-------------|
| `--base-url` | Base URL of the DAIV webapp (default: `DAIV_EXTERNAL_URL`). |
| `--repo-id` | Restrict setup to a specific repository ID. |
| `--secret-token` | Secret token for webhook validation (overrides settings). |
| `--disable-ssl-verification` | Disable SSL verification for the webhook. |
| `--update` | Update existing webhooks in addition to creating new ones. |

```bash
docker compose exec app django-admin setup_webhooks --update
```

## sync_stuck_runs

Reconcile non-terminal Run rows. Task-backed runs are re-synced from their linked task result; inline chat runs a worker crash left in RUNNING are failed once their session heartbeat goes stale; task-backed job and inbox runs a crash left RUNNING are failed the same way; and READY runs whose submitter stopped before queueing their task are failed once they are old enough.

This command takes no options.

```bash
docker compose exec app django-admin sync_stuck_runs
```
