# Code review — example output

Two delivery-mode shapes: **inline comment bodies** and the **summary discussion body** (one per review); interactive mode's protocol is in `review-workflow.md`. Every body starts with a `<!-- daiv-cr ... -->` marker (`references/marker-format.md`; `scripts/marker.py` canonical). More examples: `references/few-shot-examples.md`.

---

## Inline body A: `remove_dead_lines` (Python)

````markdown
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"remove_dead_lines","file":"services/api.py","line":42,"anchor":"a1b2c3d4","sha":"abc1234"} -->

`timeout=30` is the default for this client; the explicit argument adds no information.

```suggestion:-0+0
    response = client.get(url)
```
````

---

## Inline body B: `question` (env file)

A question anchored on one line — no `suggestion`, just a yes/no hypothesis.

````markdown
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"question","file":"env_files/all/grafana.env","line":9,"anchor":"b2c3d4e5","sha":"abc1234"} -->

The migration notes call for `fake|alloy` during cutover, but this default is `fake` only. Is `alloy` intentionally omitted here (environment-specific override), or should the default match the documented cutover list?
````

---

## Inline body C: custom-rule (Python)

A `custom-rules` finding cites the enforced rule; same archetype, dedup, adjudication as built-in findings.

````markdown
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"question","file":"payments/client.py","line":34,"anchor":"c4d5e6f7","sha":"abc1234"} -->

Per your `.agents/review-rules.md` ("every external call in `payments/` must set an explicit timeout"): this `httpx.get` has no `timeout`. Intentional (inheriting a client default), or should it set one?
````

---

## Summary discussion body

The summary collects **discussion-only** findings plus a one-line inline-findings index, updated in place per MR — delta-only re-reviews keep, not drop, unrechecked prior findings (`references/gitlab-delivery.md` Step 6).

````markdown
<!-- daiv-cr {"v":1,"kind":"summary","sha":"abc1234"} -->

**Code review** — as of abc1234

## Findings

### Medium

**1. Validation belongs in the model layer, not the controller** — `controllers/seller.ts:55`

<details>
<summary>Details</summary>

`createSeller` validates email uniqueness before delegating to the model. The model already exposes a `validate()` method which is the conventional place for this check, and putting it here means callers from the background-job path won't enforce it.

Move the uniqueness check into the model's `validate()`.

</details>

**2. Same retry policy declared in three ingestion modules** — `ingest/wavecom.py:21`

<details>
<summary>Details</summary>

`RetryPolicy(maxRetries=3, backoff=2)` appears verbatim in three sibling modules. Extract to a shared constant so any future tweak is one edit, not three.

</details>

**3. N+1: seller lookup runs once per order row** — `api/orders.py:90`

<details>
<summary>Details</summary>

`serialize_order` calls `Seller.objects.get(...)` inside the per-order loop, so a page of N orders issues N+1 queries. The fix spans the serializer and the queryset — add `select_related("seller")` on the orders queryset in the viewset — so it can't anchor on a single diff line.

</details>

## Questions

**4. Does the new ingestion pipeline preserve ordering across the three modules, or is reordering OK?** — multiple files

<details>
<summary>Details</summary>

The refactor moves dedup into `ingest/wavecom.py` and removes the explicit sort step from two callers. The change spans more than one file, so it can't anchor on a single diff line. Is the looser ordering intentional, or did the dedup move accidentally drop the sort?

</details>

## Inline findings posted (3)

- `services/api.py:42` — drop explicit default `timeout=30` *(remove_dead_lines)*
- `internal/cache/key.go:18` — use `strings.Join` instead of manual loop *(use_framework_idiom)*
- `env_files/all/grafana.env:9` — is `alloy` intentionally omitted from the default tenant list? *(question)*

_5/5 detectors · 9 candidates · 2 merged → 3 inline, 4 in summary (rest refuted)._
````

---

## What goes inline vs in the summary

Bucketing (inline vs summary, and the demote-on-unreliable-position guard) is authoritative in `references/gitlab-delivery.md` Step 3.
