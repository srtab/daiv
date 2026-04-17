# RT → DAIV Triage Scrip — Design

**Date:** 2026-04-17
**Status:** Approved (pending spec review)

## Problem

New tickets land in Request Tracker (`support.dipcode.com`) without any automated assessment. A support engineer has to read each ticket cold, classify it (bug / config / how-to), and — for code-related ones — go hunt through the relevant repo to form a hypothesis. This is slow, uneven, and delays the first substantive response.

DAIV already exposes a Jobs API (`POST /api/jobs`) that runs an agent against a repo and supports MCP tools. RT is already wired into the agent as an MCP server. The missing piece is the trigger: something that submits a triage job the moment a ticket is created.

## Goals

1. On every new ticket in an allow-listed queue, submit a DAIV job that produces a triage + RCA report.
2. Land the report on the ticket as an **internal comment** (staff-only, not emailed to the requester), with no bridge service in between.
3. Keep the whole integration inside Request Tracker: a single RT Scrip, configured through `RT_SiteConfig.pm` and RT's built-in queue filtering.
4. Fail safely — a DAIV outage must not block or delay the ticket creation transaction.

## Non-goals

- No bridge service, webhook receiver, or background worker.
- No polling of DAIV from RT. The agent writes the comment back itself via the RT MCP.
- No per-ticket opt-in UI, no CustomFields for status/job-id (can be added later if needed).
- No retry, deduplication, or job cancellation logic. `On Create` fires exactly once.
- No changes to the DAIV codebase. This integration consumes DAIV's public Jobs API only.

## Design

### Architecture

```
RT ticket Create
      │
      ▼
RT Scrip (Perl, "On Create", applies to allow-listed queues)
   1. Resolve queue → repo_id from inline %QUEUE_REPO_MAP
   2. Build prompt (ticket id, url, queue, subject + instructions)
   3. POST $DAIV_URL/api/jobs  (Bearer, use_max=true, short timeout)
   4. Log + return (fire-and-forget)
      │
      ▼
DAIV agent runs asynchronously
   - Reads full ticket via RT MCP (requestor, body, attachments, CFs)
   - Performs triage; if code-related, RCA against resolved repo
   - Posts the report as an internal comment on ticket {id} via RT MCP
```

The only RT-side artifact is the Scrip. The only DAIV-side artifact is a documented prompt shape. No new services, no new infrastructure.

### The Scrip

**Location in RT:** `Admin → Scrips → Create`.

- **Description:** `DAIV triage on create`
- **Condition:** `On Create`
- **Action:** `User Defined`
- **Template:** `Blank` (Scrip builds its own JSON body)
- **Stage:** `TransactionCreate`
- **Applies to queues:** the allow-list (managed in RT's native "Applies To" UI — no code-level queue filtering). This list and the keys of `%QUEUE_REPO_MAP` in the Scrip body must be kept in lockstep: the "Applies To" list gates whether the Scrip runs at all, and the map resolves the queue to a repo. A queue present in "Applies To" but missing from the map is treated as a misconfiguration and logged (see Failure handling).

**Required CPAN modules** — all ship with a standard RT install:

- `LWP::UserAgent`
- `HTTP::Request`
- `JSON` (or `JSON::PP`)

**Custom action code:** the canonical body lives at `docs/integrations/rt/rt-daiv-triage.scrip.pl`. Its skeleton:

1. Read `DAIV_URL` / `DAIV_API_KEY` from `RT->Config->Get(...)`. If either is missing, log `error` and `return 1`.
2. Enter `eval { local $SIG{ALRM} = sub { die "daiv-triage timeout\n" }; alarm(10); ... };` — everything that touches `$self->TicketObj` or makes HTTP / JSON calls runs inside this block. Both the success tail and the `or do { ... }` failure tail call `alarm(0)` so a SIGALRM can't leak into a later Scrip.
3. Inside the eval: look up the queue → repo via `%QUEUE_REPO_MAP`. If absent, log `warning` and let the eval fall through (no HTTP call, but still runs the `alarm(0)` cleanup).
4. Build the prompt from `Ticket ID / URL / Queue / Subject / repo`.
5. `POST $DAIV_URL/api/jobs` via `LWP::UserAgent->new(timeout => 5)`. On `is_success`, guard `JSON::decode_json` in its own inner `eval` so a malformed 2xx body doesn't masquerade as a submit failure — the job *was* accepted. On HTTP 429 log at `warning` (routine throttling, not an operational error). On any other non-2xx, log `error`.
6. If the outer `eval` catches a die (HTTP client crashed, DNS hung past the 10 s ceiling, RT object died), log `error` with the captured `$@`.
7. Final unconditional `return 1;` — RT sees success regardless.

**Return value** — the Scrip always returns `1`. It must never abort the transaction: DAIV outages, DNS stalls, malformed responses, and ticket/queue object dies are all trapped and logged. Ticket creation still succeeds.

### Configuration in `RT_SiteConfig.pm`

```perl
Set($DAIV_URL,     'https://daiv.internal.dipcode.com');
Set($DAIV_API_KEY, 'prefix.secret');   # created via: python manage.py create_api_key <user>
```

Values are read at Scrip runtime (not cached), so rotation is a config-edit + `apache2ctl graceful` away.

### The prompt contract

Fixed inputs passed from the Scrip:

| Field | Source |
|---|---|
| Ticket ID | `$ticket->id` |
| URL | `RT->Config->Get('WebURL') . "Ticket/Display.html?id=$id"` |
| Queue | `$ticket->QueueObj->Name` |
| Subject | `$ticket->Subject` |
| Repo | `%QUEUE_REPO_MAP{$queue}` |

Anything else the agent needs (body, requestor, attachments, CustomFields, related tickets) it fetches itself via the RT MCP. This keeps the Scrip dumb and the prompt small.

### Failure handling

| Failure mode | Behaviour |
|---|---|
| Queue not in `%QUEUE_REPO_MAP` (but Scrip ran because queue is in "Applies To") | Scrip logs at `warning` level — this is a misconfiguration — and returns `1`. No ticket change. |
| `DAIV_URL` / `DAIV_API_KEY` unset in `RT_SiteConfig` | Scrip logs at `error`, returns `1`. No ticket change. |
| DAIV `/api/jobs` returns non-2xx other than 429 | Scrip logs response status + body at `error`, returns `1`. |
| DAIV returns HTTP 429 (rate limit) | Scrip logs at `warning` with the status line, returns `1`. Alerting wired to `error` does not page on routine throttling. |
| DAIV unreachable (LWP socket timeout at 5s, or 10s wall-clock alarm fires) | Logged at `error` with the prefix `exception in triage scrip: daiv-triage timeout`. Returns `1`. |
| DAIV returns a 2xx with an empty or malformed body | Inner `eval` around `JSON::decode_json` contains the die; Scrip logs `info` with `job_id=?` instead of a false "submit failure". |
| RT ticket / queue object access dies (e.g. pathological queue, ACL edge) | Outer `eval` traps the die and logs at `error` via the `exception in triage scrip:` prefix. Ticket creation still succeeds. |
| DAIV job submitted but agent fails or never posts | Invisible on the ticket. Detectable via DAIV's activity UI by searching for the logged `job_id`. Acceptable for v1. |
| RT MCP call from the agent fails during comment post | Report shows up in the DAIV activity log but not on the ticket. Same remediation path. |

No retries. RT's logs (`rt.log`) are the single source of operational truth for the RT side; DAIV's Activity page is the source of truth for the agent side.

### Layout

Inside the DAIV repo:

```
docs/integrations/rt/
  index.md                    ← overview + install steps (RT_SiteConfig, Scrip install, queue allow-list)
  rt-daiv-triage.scrip.pl     ← pasteable Perl body for the "User Defined" action
```

`docs/index.md` (mkdocs nav) gets one new entry under an **Integrations** section pointing at `integrations/rt/index.md`. The existing `docs/features/jobs-api.md` is cross-linked from the new page (the RT triage is a concrete example of the Jobs API in action).

### Testing

The Scrip is ~130 lines of Perl (the HTTP call itself is a handful of lines; most of the body is the `eval`/`alarm` safety wrapper and the prompt heredoc) that runs inside RT. Testing approach:

1. **Manual smoke test in a staging RT** — create a ticket in an allow-listed queue, confirm:
   - `rt.log` shows `submitted job <uuid>`
   - DAIV activity page shows a running `API_JOB` activity for the repo
   - Within a minute or two, an internal comment appears on the ticket
2. **Negative paths verified manually:**
   - Ticket in a non-allow-listed queue → `skipping` log line, no job.
   - `DAIV_URL` pointed at an unreachable host → error log line, ticket still created cleanly.

No automated test harness is proposed. The Scrip has no logic worth unit-testing in isolation (it's config lookup + HTTP POST); integration behaviour is covered by the manual checks above. If the Scrip grows (e.g. repo inference rules), we revisit.

### Rollout

1. Create a DAIV API key for a dedicated service user (`rt-triage`) via `python manage.py create_api_key rt-triage --name rt-scrip`. Store it in `RT_SiteConfig.pm`.
2. Add `DAIV_URL` and `DAIV_API_KEY` entries to `RT_SiteConfig.pm` on the RT host.
3. Paste the Scrip body into `Admin → Scrips → Create`; set condition/action/stage; attach to a **single** pilot queue first.
4. File a test ticket in the pilot queue; confirm the internal comment appears.
5. Extend the Scrip's "Applies To" queue list as confidence grows. Extend `%QUEUE_REPO_MAP` in lockstep.

## Open questions

- **Service user for comment posting.** The agent posts the comment through the RT MCP. Which RT user do those MCP calls authenticate as? If it's a personal user, comments will be attributed to them; a shared `daiv-bot` RT user is probably preferable. This is an RT MCP config concern, not a Scrip concern, but worth confirming before pilot.
- **Cost ceiling.** `use_max: true` on every new ticket in every allow-listed queue could be expensive. No throttling proposed for v1; if volume turns out to be high, either drop `use_max` to `false` for the initial classification pass or add a queue-level rate limit later.
