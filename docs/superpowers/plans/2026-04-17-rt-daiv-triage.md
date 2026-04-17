# RT → DAIV Triage Scrip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a Request Tracker Scrip (Perl) that fires on ticket create, submits a triage job to the DAIV Jobs API, and lets the DAIV agent post the result back as an internal comment via its RT MCP — all documented under `docs/integrations/rt/` and wired into the mkdocs site.

**Architecture:** No runtime code in the DAIV repo. The deliverable is a pasteable Perl Scrip body plus an install guide. The Scrip resolves a queue → `repo_id` from an inline hash, builds a prompt (ticket id/url/queue/subject + instructions to finish by posting an internal comment via RT MCP), and POSTs to `$DAIV_URL/api/jobs` with a 5-second timeout. All failures are logged via `$RT::Logger` and return `1` so the ticket-create transaction is never blocked.

**Tech Stack:** Perl 5 (`LWP::UserAgent`, `HTTP::Request`, `JSON` — all shipped with RT). mkdocs-material for docs. DAIV Jobs API (`POST /api/jobs`).

---

## Spec

Source spec: `docs/superpowers/specs/2026-04-17-rt-daiv-triage-design.md`. Review it before starting — the Scrip body, `RT_SiteConfig` keys, failure table, and open questions live there.

## File Structure

Files created or modified by this plan:

| Path | Role |
|---|---|
| `docs/integrations/rt/rt-daiv-triage.scrip.pl` (create) | Pasteable Perl body for the RT "User Defined" action. Only real artifact of the integration. |
| `docs/integrations/rt/index.md` (create) | Install/configure guide — `RT_SiteConfig.pm` keys, Scrip setup clicks, queue allow-list management, pilot rollout steps, troubleshooting. |
| `mkdocs.yml` (modify) | Add an **Integrations** top-level nav section and register the same page in the `llmstxt` plugin `sections:` map (the site is built with `strict: true`, so both must stay in sync). |
| `docs/features/jobs-api.md` (modify) | Add a short cross-link at the bottom pointing at the new RT integration page (it's the flagship example of the Jobs API). |

Nothing under `daiv/` changes. No new Python, no new tests.

## Testing approach

The Scrip runs inside RT, not in this repo, so there is no unit-test harness. Per the spec, v1 relies on manual smoke testing in staging. What this plan *can* verify locally:

- `perl -c` on the Scrip file: catches syntax errors before anyone pastes it into RT.
- `uv run --only-group=docs mkdocs build --strict`: the mkdocs config is `strict: true`, so any nav entry pointing at a missing file, or any file not referenced in nav, fails the build.

Those two checks gate every doc-touching task.

---

## Task 1: Create the Perl Scrip file

**Files:**
- Create: `docs/integrations/rt/rt-daiv-triage.scrip.pl`

- [ ] **Step 1: Create the directory**

```bash
mkdir -p docs/integrations/rt
```

- [ ] **Step 2: Write the Scrip body**

Create `docs/integrations/rt/rt-daiv-triage.scrip.pl` with this exact content:

```perl
#!/usr/bin/env perl
# RT Scrip — DAIV triage on ticket create.
#
# Install:
#   Admin → Scrips → Create
#     Description:   DAIV triage on create
#     Condition:     On Create
#     Action:        User Defined
#     Template:      Blank
#     Stage:         TransactionCreate
#     Applies To:    (select the queues you want triaged)
#
# Paste the body below into the "Custom action preparation code" field
# (leave "Custom condition" and "Custom action cleanup code" empty).
#
# Requires two entries in RT_SiteConfig.pm:
#   Set($DAIV_URL,     'https://daiv.example.com');
#   Set($DAIV_API_KEY, 'prefix.secret');
#
# The "Applies To" queue list and %QUEUE_REPO_MAP below MUST be kept in
# lockstep. A queue present in "Applies To" but missing from the map is
# logged as a warning and skipped.

use strict;
use warnings;

use LWP::UserAgent ();
use HTTP::Request  ();
use JSON           ();

my %QUEUE_REPO_MAP = (
    'support-webapp' => 'group/webapp',
    'support-api'    => 'group/api',
    # add more queues here as they come online
);

my $ticket = $self->TicketObj;
my $queue  = $ticket->QueueObj->Name;
my $repo   = $QUEUE_REPO_MAP{$queue};

unless ($repo) {
    $RT::Logger->warning(
        "daiv-triage: queue '$queue' in Applies-To but missing from QUEUE_REPO_MAP; skipping"
    );
    return 1;
}

my $daiv_url = RT->Config->Get('DAIV_URL');
my $daiv_key = RT->Config->Get('DAIV_API_KEY');

unless ($daiv_url && $daiv_key) {
    $RT::Logger->error(
        "daiv-triage: DAIV_URL or DAIV_API_KEY not set in RT_SiteConfig.pm; skipping"
    );
    return 1;
}

my $id      = $ticket->id;
my $subject = $ticket->Subject // '';
my $url     = RT->Config->Get('WebURL') . "Ticket/Display.html?id=$id";

my $prompt = <<"PROMPT";
A new Request Tracker ticket was just created.

- Ticket ID: $id
- URL: $url
- Queue: $queue
- Subject: $subject

Use the RT MCP to load the full ticket (requestor, first correspondence,
attachments, CustomFields), then:

1. Classify: bug / config / how-to / unclear.
2. If code-related, perform RCA against repo `$repo`: likely file + function,
   root cause hypothesis, fix sketch.
3. If not code-related, stop after triage and state what information is
   missing from the requester.

When finished, post your report as an **internal comment** (not
correspondence) on RT ticket $id using the RT MCP. Use markdown.
End with a one-line **Recommendation** (e.g. "assign to backend",
"needs more info from requester").
PROMPT

my $payload = JSON::encode_json({
    repo_id => $repo,
    prompt  => $prompt,
    use_max => JSON::true,
});

my $ua  = LWP::UserAgent->new(timeout => 5);
my $req = HTTP::Request->new(POST => "$daiv_url/api/jobs");
$req->header('Authorization' => "Bearer $daiv_key");
$req->header('Content-Type'  => 'application/json');
$req->content($payload);

my $res = $ua->request($req);
if ($res->is_success) {
    my $body   = JSON::decode_json($res->decoded_content);
    my $job_id = $body->{job_id} // '?';
    $RT::Logger->info(
        "daiv-triage: submitted job $job_id for ticket $id (queue=$queue repo=$repo)"
    );
}
else {
    $RT::Logger->error(
        "daiv-triage: failed to submit job for ticket $id: "
        . $res->status_line . ' ' . ($res->decoded_content // '')
    );
}

return 1;
```

- [ ] **Step 3: Syntax-check the file**

Run: `perl -c docs/integrations/rt/rt-daiv-triage.scrip.pl`

Expected output: `docs/integrations/rt/rt-daiv-triage.scrip.pl syntax OK`

If Perl isn't installed locally, skip this step and note it — the RT host will catch syntax errors on save.

- [ ] **Step 4: Commit**

```bash
git add docs/integrations/rt/rt-daiv-triage.scrip.pl
git commit -m "feat(integrations): add RT scrip body for DAIV triage on ticket create"
```

---

## Task 2: Create the install/configure guide

**Files:**
- Create: `docs/integrations/rt/index.md`

- [ ] **Step 1: Write the guide**

Create `docs/integrations/rt/index.md` with this exact content:

````markdown
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
````

- [ ] **Step 2: Commit**

```bash
git add docs/integrations/rt/index.md
git commit -m "docs(integrations): add RT triage install and configuration guide"
```

---

## Task 3: Wire the new page into mkdocs nav and llmstxt

**Files:**
- Modify: `mkdocs.yml`

- [ ] **Step 1: Add the Integrations nav section**

Open `mkdocs.yml`. Find the `nav:` block (around line 119). Insert a new `Integrations` entry between `Features:` and `Customization:` so integrations sit with feature-level docs:

Replace this block (around lines 125-136):

```yaml
  - Features:
    - Issue Addressing: features/issue-addressing.md
    - Pull Request Assistant: features/pull-request-assistant.md
    - Jobs API: features/jobs-api.md
    - Scheduled Jobs: features/scheduled-jobs.md
    - MCP Endpoint: features/mcp-endpoint.md
    - Activity Tracking: features/activity-tracking.md
    - Merge Metrics: features/merge-metrics.md
    - Slash Commands & Skills: features/slash-commands.md
    - Subagents: features/subagents.md
    - Sandbox: features/sandbox.md
  - Customization:
```

With:

```yaml
  - Features:
    - Issue Addressing: features/issue-addressing.md
    - Pull Request Assistant: features/pull-request-assistant.md
    - Jobs API: features/jobs-api.md
    - Scheduled Jobs: features/scheduled-jobs.md
    - MCP Endpoint: features/mcp-endpoint.md
    - Activity Tracking: features/activity-tracking.md
    - Merge Metrics: features/merge-metrics.md
    - Slash Commands & Skills: features/slash-commands.md
    - Subagents: features/subagents.md
    - Sandbox: features/sandbox.md
  - Integrations:
    - Request Tracker Triage: integrations/rt/index.md
  - Customization:
```

- [ ] **Step 2: Register the page in the `llmstxt` plugin sections**

In the same file, find the `plugins: - llmstxt: sections:` block (around lines 66-89). After the `Features:` list (ends with `features/sandbox.md`) and before `Customization:`, insert a new section:

Replace this block:

```yaml
        - features/sandbox.md: Secure sandboxed code execution
        Customization:
```

With:

```yaml
        - features/sandbox.md: Secure sandboxed code execution
        Integrations:
        - integrations/rt/index.md: Request Tracker triage on ticket create via RT Scrip
        Customization:
```

- [ ] **Step 3: Verify the site still builds in strict mode**

Run: `uv run --only-group=docs mkdocs build --strict`

Expected: `INFO    -  Documentation built in <N>.<NN> seconds` and no warnings. `strict: true` fails the build on any broken link, missing nav target, or orphaned file.

If the build fails with "The following pages exist in the docs directory, but are not included in the 'nav' configuration" referencing `rt-daiv-triage.scrip.pl` — that's expected for a non-markdown file. Either mkdocs will ignore it (common) or the fix is to add `exclude_docs: rt-daiv-triage.scrip.pl` under the top-level keys. Apply the minimal fix needed to get a clean build; do not suppress real errors.

- [ ] **Step 4: Commit**

```bash
git add mkdocs.yml
git commit -m "docs(nav): register Integrations section for RT triage guide"
```

---

## Task 4: Cross-link from the Jobs API page

**Files:**
- Modify: `docs/features/jobs-api.md`

- [ ] **Step 1: Add a "See also" section**

Open `docs/features/jobs-api.md`. Append a short cross-link section at the very end of the file (after the existing GitLab CI example block, after the `!!! tip` admonition):

```markdown

## See also

- [Request Tracker Triage](../integrations/rt/index.md) — an end-to-end example of using the Jobs API from an RT Scrip to triage new support tickets automatically.
```

- [ ] **Step 2: Re-verify the site builds**

Run: `uv run --only-group=docs mkdocs build --strict`

Expected: clean build with no warnings. If the relative link is wrong, `strict` will flag it.

- [ ] **Step 3: Commit**

```bash
git add docs/features/jobs-api.md
git commit -m "docs(jobs-api): cross-link Jobs API page to RT triage integration"
```

---

## Task 5: Final verification

- [ ] **Step 1: Full strict build one more time**

Run: `uv run --only-group=docs mkdocs build --strict`

Expected: clean, 0 warnings, 0 errors.

- [ ] **Step 2: Perl syntax check**

Run: `perl -c docs/integrations/rt/rt-daiv-triage.scrip.pl`

Expected: `... syntax OK`. (Skip if Perl isn't installed locally; the RT host will validate on paste.)

- [ ] **Step 3: Confirm diff is scoped**

Run: `git log --oneline main..HEAD` and `git diff --stat main..HEAD`

Expected: 4 commits, files touched strictly under `docs/integrations/rt/`, `docs/features/jobs-api.md`, and `mkdocs.yml`. Anything else is scope creep and should be reverted.

---

## Out of scope for this plan

These are intentionally deferred — do **not** add them:

- A CustomField for storing the DAIV `job_id` per ticket.
- Retries of failed DAIV submissions.
- Automated (non-manual) tests for the Scrip.
- Any change to `daiv/` source code.
- Decisions about which RT user the RT MCP authenticates as — that's a DAIV MCP configuration concern flagged as an open question in the spec and owned by the operator running the pilot.
