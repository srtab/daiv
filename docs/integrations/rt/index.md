# Request Tracker Triage

Trigger DAIV automatically whenever a new ticket lands in Request Tracker. The DAIV agent reads the ticket, performs triage (bug / config / how-to / unclear) and — when the ticket is code-related — a root-cause analysis against the queue's repository. The report is posted back to the ticket as an **internal comment** (staff-only, not emailed to the requester).

The whole integration is a single RT Scrip. No bridge service, no webhook receiver, no polling.

## How it works

```
RT ticket Create
      │
      ▼
RT Scrip (Perl, "On Create", applies to allow-listed queues)
   1. Resolve queue → repo_id
   2. Build prompt (ticket id, url, queue, subject)
   3. POST $DAIV_URL/api/jobs
      │
      ▼
DAIV agent runs asynchronously
   - Reads the full ticket via its RT MCP tool
   - Does triage; if code-related, performs RCA against the repo
   - Posts the report as an internal comment on the ticket via RT MCP
```

The Scrip is fire-and-forget: it submits the job with a 5-second HTTP timeout and always returns success, so a DAIV outage never blocks ticket creation.

## Prerequisites

- A DAIV deployment reachable from the RT host, with the [Jobs API](../../features/jobs-api.md) enabled.
- The RT MCP server wired into the DAIV agent (see [MCP Tools](../../customization/mcp-tools.md)). The RT MCP must authenticate as a user with rights to **comment** on the target queues. A dedicated `daiv-bot` RT user is recommended so comments are clearly attributed.
- A DAIV API key for a dedicated service user:

  ```bash
  python manage.py create_api_key rt-triage --name rt-scrip
  ```

  Store the emitted `prefix.secret` string; it cannot be retrieved later.

## Configure RT

### 1. Add the DAIV endpoint and key to `RT_SiteConfig.pm`

```perl
Set($DAIV_URL,     'https://daiv.example.com');
Set($DAIV_API_KEY, 'prefix.secret');
```

Reload RT after editing: `apache2ctl graceful` (or whichever reload command your RT host uses).

### 2. Install the Scrip

1. Open **Admin → Scrips → Create** in the RT web UI.
2. Fill the form:

   | Field | Value |
   |---|---|
   | Description | `DAIV triage on create` |
   | Condition | `On Create` |
   | Action | `User Defined` |
   | Template | `Blank` |
   | Stage | `TransactionCreate` |
   | Applies To | *(leave empty for now — configured in step 3)* |

3. Paste the body of [`rt-daiv-triage.scrip.pl`](rt-daiv-triage.scrip.pl) into the **Custom action preparation code** field. Leave **Custom condition** and **Custom action cleanup code** empty. Save.

4. Edit the `%QUEUE_REPO_MAP` hash at the top of the pasted code so each queue you care about maps to the correct DAIV `repo_id` (e.g. `group/project`).

### 3. Attach the Scrip to queues

From the Scrip's **Applies To** tab in the RT admin UI, select the queues you want triaged. **The "Applies To" list and the keys of `%QUEUE_REPO_MAP` must stay in lockstep** — any queue where the Scrip runs but no repo is mapped will log a warning and skip (no ticket change, no job).

Start with a single pilot queue. Expand only after the pilot looks healthy.

## Verify the pilot

1. File a test ticket in the pilot queue.
2. `tail -f /var/log/request-tracker4/rt.log` (or your RT log path) — look for:

   ```
   daiv-triage: submitted job <uuid> for ticket <id> (queue=... repo=...)
   ```

3. Open the DAIV **Activity** page and confirm a new `API_JOB` run exists for the target repo.
4. Within a minute or two, an internal comment with the triage report should appear on the ticket. If it doesn't, check the Activity detail page — the agent may have errored mid-run or failed to post via the RT MCP.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No log line at all after ticket create | Scrip not attached to the queue, or condition/stage is wrong | Re-check the Scrip's **Applies To** list and confirm Condition=`On Create`, Stage=`TransactionCreate` |
| `DAIV_URL or DAIV_API_KEY not set` in `rt.log` | `RT_SiteConfig.pm` not reloaded | `apache2ctl graceful` |
| `queue '<name>' in Applies-To but missing from QUEUE_REPO_MAP` | "Applies To" and the inline map drifted apart | Add the queue to `%QUEUE_REPO_MAP` with its repo, or remove it from "Applies To" |
| `failed to submit job … 401` | Wrong or expired DAIV API key | Rotate via `python manage.py create_api_key` and update `RT_SiteConfig.pm` |
| `failed to submit job … 429` | Jobs API rate limit exceeded (default 20/hour per user) | Raise `DAIV_JOBS_THROTTLE_RATE` on the DAIV host, or use a separate API-key user per queue |
| Job completes in DAIV but no comment on the ticket | RT MCP auth user lacks comment rights on the queue, or MCP call errored | Grant the MCP's RT user the `CommentOnTicket` right on the queue; inspect the run's tool calls in Activity |

## Cost considerations

The Scrip submits with `use_max: true` — the more capable model with high thinking. This produces better triage but is more expensive. For high-volume queues, consider:

- Dropping `use_max` to `false` in the Scrip body for a cheaper first pass.
- Adding a queue-level rate limit via a separate DAIV API-key user with a lower `DAIV_JOBS_THROTTLE_RATE`.

## Files

- [`rt-daiv-triage.scrip.pl`](rt-daiv-triage.scrip.pl) — pasteable Scrip body.
