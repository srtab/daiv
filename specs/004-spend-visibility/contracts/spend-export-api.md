# Contract: spend rows (CSV export and API)

The CSV export and the API serve the same rows from `SpendLedger.rows(user, scope, start, end)`, ordered by `occurred_at`, then `id`. One row is one `SpendRecord` ([data-model.md](../data-model.md)).

## Row fields

| CSV column / JSON key | Type | Meaning |
|---|---|---|
| `occurred_at` | ISO 8601 datetime, UTC | When it counts (R5) |
| `kind` | `run` or `background` | |
| `task` | `SpendTask` value, or empty | Background task |
| `trigger_type` | `SessionOrigin` value, or empty | Run's trigger type |
| `person` | string | DAIV username; the external username for a git platform account; `DAIV` for no person |
| `person_kind` | `user`, `external`, `deleted`, `daiv` | |
| `repo_id` | string, may be empty | |
| `session_id` | string, may be empty | `thread_id` |
| `run_id` | UUID string, may be empty | Equals the jobs API's `job_id` |
| `schedule_id` | integer, may be empty | The session's schedule |
| `model` | string | |
| `provider` | string, may be empty | Provider slug when the call was made; empty only in backfilled rows |
| `provider_name` | string, may be empty | Its display name then |
| `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `total_tokens` | integer | See the token kinds in the data model |
| `cost_usd` | decimal string, 6 places | Priced part |
| `reported_cost_usd` | decimal string, or empty | Empty when the split is unknown |
| `unpriced_tokens` | integer | |
| `cache_reported` | boolean | Whether the provider reported cache tokens for this row; when false, `cache_read_tokens` 0 means unknown, not a miss |
| `cache_saving_usd` | decimal string, 6 places | Estimated cost the cache hits avoided, from list prices |
| `cache_unpriced_tokens` | integer | Cache-read tokens whose saving could not be priced |
| `cost_source` | `reported`, `estimated`, `mixed`, `unknown`, `unpriced` | A label derived from the three columns above, for spreadsheet filtering |

The CSV has a header row with these names, in this order. The JSON uses the same keys, with `null` for empty values.

## `GET /api/spend/records`

**Auth**: `Authorization: Bearer <API key>`, or the browser session (`[AuthBearer(), django_auth]`). The rows are those the key's user may see (FR-017, R6).

**Throttle**: `SpendRateThrottle` (`core/api/throttling.py`), fixed at 60 requests per minute per user. Its bucket is keyed on the user's primary key, as `JobsRateThrottle` does. There is no site setting: a read starts no agent work, so the rate guards only against runaway clients.

| Query param | Type | Required | Rules |
|---|---|---|---|
| `from` | date (`YYYY-MM-DD`) | yes | Local midnight of that date, inclusive |
| `to` | date | yes | Through the end of that local day; `to − from` ≤ 92 days |
| `scope` | `mine`, `all` | no | Default `all`; `mine` = records the user started |
| `cursor` | opaque string | no | From the previous page's `next_cursor` |

**200**:

```json
{
  "items": [ { "occurred_at": "2026-11-17T09:14:03Z", "kind": "run", "...": "..." } ],
  "next_cursor": "eyJ0IjoiMjAyNi0xMS0xN1QwOToxNDowM1oiLCJpIjo0MjF9"
}
```

Each page holds up to 500 items. `next_cursor` is `null` on the last page. The cursor encodes the last row's `(occurred_at, id)`, so pages stay consistent while running runs rewrite their rows. A row that changes after it was paged past isn't sent again.

**Errors** (`{"detail": "..."}`, with no internals in the text):

| Status | When |
|---|---|
| 400 | Missing or invalid `from`/`to`, `to` before `from`, a range over 92 days, or a malformed cursor |
| 401 | No or invalid credentials |
| 429 | Throttled |

**Not in scope**: aggregates over the API (clients sum the rows), and an MCP tool.
