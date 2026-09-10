# Consolidation corpus provenance

The consolidation cases in `consolidation/cases.jsonl` are drawn from a real production sample.
The extraction cases in `extraction/` are hand-authored synthetic message lists — they have no
production provenance and are not covered by this record.

| Field | Value |
|---|---|
| Source | DAIV production on the `daivagent` deployment, all repositories |
| Export date | 2026-09-07 |
| Active `MemoryEntry` rows | 57 |
| `MemoryObservation` rows | 267 |

The sample spans two repositories, both owned by the deployment's operator (not client work):
`srtab/daiv` and `srtab/daiv-sandbox`.

Row labels in this record (`E000`–`E056`, `O000`–`O266`) are zero-based indices into the export's
`entries` and `observations` arrays respectively — `E013` is `entries[13]`, `O218` is
`observations[218]`. Each row in the export also carries a `mode` field (one of the five modes
below) set to match this record, so the counts here are auditable directly from the export rather
than only from this document.

## Observed failure modes in the sample

Every entry and observation (324 rows total) was read and classified into exactly one mode.
Percentages are of the 324-row total.

| Mode | Rows | Share |
|---|---|---|
| generic advice, not hard-won | 0 | 0.0% |
| ephemeral / run-specific | 12 | 3.7% |
| duplicates and fragments | 67 | 20.7% |
| wrong or stale facts | 1 | 0.3% |
| good | 244 | 75.3% |

Method: each row was read individually; rows restating a fact already present elsewhere in the
sample were grouped into topic clusters (confirmed with an automated within-repo near-duplicate
scan over token overlap, then reviewed by hand), and within each cluster the earliest row is
counted as `good` and later restatements as `duplicates and fragments`. This is a judgment call,
not an exact algorithm — a different reviewer could draw cluster boundaries slightly differently,
but the order of magnitude (duplicates and fragments are the dominant bad mode, at roughly a
fifth of the sample) is not sensitive to that.

**Two counts contradict the spec's premise, and are flagged here as instructed:**

1. **`generic_advice` was not observed at all** (0 rows), despite the spec expecting it to be
   present. Nothing in the sample resembles "write tests" or "follow the style guide" — every
   row, including the `reviewer_preference` rows, is anchored to a specific file, tool, or
   command in one of the two repositories. This is plausibly because the sample comes from a
   single experienced operator's own agent runs on their own well-documented repos, not because
   the failure mode can't occur. Per the brief's fallback: the `generic_advice` case in the eval
   corpus must be built from a paraphrase of the nearest real row (e.g. `E012`'s "reuse existing
   shared classifiers" trimmed of its concrete anchor) and marked **synthetic** — it is a valid
   regression guard, not evidence the failure happens in this production sample.

2. **`wrong_or_stale` was not zero.** One row, a `MemoryObservation` on `srtab/daiv-sandbox`
   (`O218`, `status=discarded`, created 2026-08-25), asserts the Sentry org slug for that project
   is `daiv`; a later, consolidated observation in the same sample (and the active `MemoryEntry`
   it fed) both correctly state `srtab`. Two things keep this from actually reopening the "no
   decay" scope decision in the plan: the row is a raw `MemoryObservation`, not an active
   `MemoryEntry` — it never reached the rendered document — and its `status` is already
   `discarded`, i.e. the existing extraction/consolidation pipeline caught and dropped it on its
   own, without any decay or re-verification mechanism. **All 57 active `MemoryEntry` rows are
   internally consistent; zero are contradicted by another row in the sample.** So: the raw count
   is non-zero and is reported honestly, but it does not demonstrate that active memory decays,
   which is the specific risk the plan's scope decision is about.

   A related, softer tension worth recording for whoever authors the corpus cases: active entry
   `E041` (environment-tag filtering on the `daiv` Sentry project being unreliable) disagrees with
   one observation in the same sample, `O200` (created 2026-08-21, mode `ephemeral`): "running
   `sentry_search_issues` with and without `environment:production` returned identical results, so
   there is no environment-tagging gap." Two other observations agree with `E041` instead —
   `O111` (2026-08-10) and `O235` (2026-08-28, worded almost identically to `E041` and the more
   plausible source of its content). `E041`'s `created_at` and `last_confirmed_at` are identical
   to the microsecond (2026-08-31 09:00:38.261117) — this is a **creation** event, not a
   reconfirmation; `E041` has never been reconfirmed since (see the measured note below). So the
   accurate framing is about creation-time reconciliation, not reconfirmation behavior: at the
   moment `E041` was created, the agreeing `O235` was three days old and the contradicting `O200`
   was already ten days old, and nothing in the sample shows the contradiction being reconciled
   either then or since. The contradiction also isn't confined to before creation — `O263`
   (2026-09-07) echoes `O200`'s "identical results" finding again about a week *after* `E041`'s
   creation, so contradicting evidence brackets `E041`'s one timestamp on both sides, not just
   the historical one. `O263`'s primary content and its `duplicate_or_fragment` mode are about an
   unrelated fact (`sentry_search_events` omitting stack traces) that instead matches `E042`,
   whose `last_confirmed_at` (a genuine reconfirmation — `E042` is one of the 5 in the measured
   note below) is the same day — so `O263` is not primary evidence for the tension above, only
   a later echo of it noted for completeness. None of this crosses into `wrong_or_stale`: it
   still reads as flaky/time-varying Sentry-side tagging behavior rather than a fact contradicted
   by the repository, and neither `E041` nor `O235` is *wrong* in any verifiable sense — but it's
   the closest near-miss in the sample and worth a second look if a stronger `wrong_or_stale`
   example is ever needed.

   **Measured, for Task 11's author:** `last_confirmed_at` is the sort key `_eviction_order`
   (`daiv/memory/render.py:52-53`) uses to pick which entry a full render budget evicts first —
   `prune_to_budget` takes the *smallest* `(last_confirmed_at, created_at, pk)` in the largest
   category, i.e. the least-recently-confirmed entry — and it only moves when
   `MemoryEntry.confirm()` runs (`daiv/memory/models.py:136`), which only `run_consolidation_round`
   calls (`daiv/memory/consolidation.py:230`). In this sample, only **5 of the 57** active entries
   (8.8%) have ever had `last_confirmed_at` move past `created_at` — `E013`, `E019`, `E024`,
   `E035`, and `E042`. The other **52 (91.2%)** are frozen at their creation timestamp, `E041`
   among them. That means the eviction key is, for the large majority of this sample, already
   behaving exactly like FIFO-by-creation-date today, independent of any extraction-prompt
   change. This is a measured baseline only — what (if anything) to do about it is Task 11's
   call, not this record's.

Reuse for Task 8/9 case authoring: the sample already contains two real examples of **unmerged
duplicate `MemoryEntry` rows** — `E013`/`E014`/`E015` (three active `srtab/daiv` entries that
overlap on which Sentry MCP tools are available) and `E023`/`E033` (two active `srtab/daiv`
entries both describing the `make lint` command composition) — these are directly usable for a
"merge overlapping fragments" case without paraphrasing beyond scrubbing. The `duplicates and
fragments` observation clusters (the largest: nine restatements that a set of Sentry issues are
by-design audit logs, not defects) supply raw material for a "restate/confirm duplicate" case.
The `ephemeral` rows (specific test-run counts, resolved-incident summaries, point-in-time Sentry
query results) supply material for an ephemeral-filtering case.

## Scrubbing

This repository is public. The sample is drawn from the personal `daivagent` deployment rather
than the corporate one, so no client repository data is involved. The export had the following
scrubbed in place, in every `content` value. The raw export is not distributed with this
repository — it is not committed, and it exists only as a working file on the maintainer's own
machine for authoring the corpus in `consolidation/cases.jsonl`; nobody reading this repository on
GitHub can reach it.

- **Sentry org dashboard URLs and numeric project IDs (10 instances):** seven occurrences of
  Sentry org dashboard/issue links (Sentry requires an authenticated, org-member session to view
  these — they are not public) and three occurrences of numeric Sentry project IDs were removed.
  No case needs a dashboard link or a numeric resource id; every fact those rows state stands on
  its own once the link/id is dropped.
- **Sentry issue short-ids (51 instances, 19 distinct ids across 32 rows):** bare Sentry short-ids
  (e.g. the `DAIV-` prefix followed by a number or letter code) were replaced with a
  non-identifying description of the same issue (e.g. "a recurring Sentry issue", "a Sentry issue
  about X"), or with a placeholder like `<short-id>` where the id was itself the example of a
  query syntax. Every row states its underlying fact independently of the specific id, so the
  substitution is lossless. `DAIVRedisSerializer` and the bare project name `DAIV` (e.g. in a
  Sentry culprit string like `invoke_agent DAIV Agent`) are not ids and were left untouched.
- **Internal company domain (1 instance):** one observation quoted a company-internal domain as a
  fallback URL pattern suggested by a skill; replaced with `example.internal`.
- **Ticket/PR references (4 instances):** four observations cited GitHub PR numbers
  (this repository's own, each verified to resolve to a real squash-merge commit in its public
  history) as the source of a fix; the numeric references were dropped and the surrounding fact
  kept (e.g. "removed in a later fix" instead of "removed in the fix for PR #1344"). Git commit
  SHAs mentioned elsewhere (e.g. `d801a44b`, `aca9850a`) were left as-is — they are not ticket
  references and, unlike a ticket ID, carry no separate attribution.
- **Person names, usernames (0 instances):** none were found in `content`. The sample never
  mentions an individual by name; it is a single operator's own technical notes to themselves.
- **Client repository paths (0 instances):** the only repositories represented are the
  deployment operator's own public repos, `srtab/daiv` and `srtab/daiv-sandbox` — not client
  work. The GitHub org/Sentry org slug `srtab` appears inline in several rows (it is the
  project's own namespace, not a person); it was left as-is for the same reason the repo IDs were
  left as-is — both are already the project's own public identity, not private attribution.
- **`reviewer_preference` rows:** all five (three active entries plus their two source
  observations) describe a procedural rule tied to a specific file or convention (an allauth
  login-form CSS pattern, a CHANGELOG consolidation rule) with no person or team named — none
  read as attributable, so none needed paraphrasing beyond the above.

One additional data-quality artifact found during scrubbing: `O021`'s `content` is genuinely
truncated mid-sentence in production (a bare `pyproject.toml` field name followed by `= `,
nothing further) — not an export bug, verified against the raw JSON. It was left as-is (there is
nothing to scrub) and classified as a fragment.
