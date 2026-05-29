# Code review — example output

Three shapes exist: **interactive-mode markdown** (returned as the final assistant message when there's no MR to post to), **inline comment bodies** (one per inline finding, posted to a diff line in delivery mode), and the **summary discussion body** (one per review, posted as a top-level MR discussion).

Every posted body in delivery mode **must start** with a `<!-- daiv-cr { ... } -->` marker carrying a JSON payload. It's invisible in the rendered view but parsed on the next review to dedup. The inline-marker `line` and `sha` fields are diagnostic; the dedup fingerprint is `(kind, archetype, file, anchor)` where `anchor` is the first 8 hex chars of `sha256` over the stripped target line, with a next-line disambiguator appended when the target is short or all-separators. See the SKILL.md *Marker format* section for the full schema, and `scripts/marker.py` for the canonical implementation.

For a wider range of per-archetype examples across multiple languages, see `references/few-shot-examples.md`.

---

## Interactive mode (returned as the final assistant message)

```markdown
## Findings

### High

**1. Recursive call drops the `is_provider` flag** — [billing/routing.go:64](https://example.org/repo/-/blob/feature-branch/billing/routing.go#L64)

<details>
<summary>Details</summary>

`checkRouting(routing)` recursively calls itself without forwarding `isProvider`. Provider routing on chained transfers will always take the non-provider branch.

```go
c.checkRouting(routing, isProvider)
```

</details>

### Medium

**2. Loop uses `=` where it should accumulate** — [reports/aggregate.py:72](https://example.org/repo/-/blob/feature-branch/reports/aggregate.py#L72)

<details>
<summary>Details</summary>

`count = len(batch)` resets the counter each iteration. The outer loop currently runs once so the bug is latent, but it will silently miscount as soon as a second iteration is added.

```python
count += len(batch)
```

</details>

## Questions

**3. Notification fires on every save, not only on creation?** — [orders/hooks.ts:118](https://example.org/repo/-/blob/feature-branch/orders/hooks.ts#L118)

<details>
<summary>Details</summary>

The handler invokes `sendOrderConfirmation(order)` whenever `Order` is persisted. A subsequent admin edit will re-send the email. Is this intended, or should the trigger guard on a creation event?

</details>

```

---

## Delivery mode — inline body (example A: `remove_dead_lines`, Python)

````markdown
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"remove_dead_lines","file":"services/api.py","line":42,"anchor":"a1b2c3d4","sha":"abc1234"} -->

`timeout=30` is the default for this client; the explicit argument adds no information.

```suggestion:-0+0
    response = client.get(url)
```
````

---

## Delivery mode — inline body (example B: `use_framework_idiom`, Go)

````markdown
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"use_framework_idiom","file":"internal/cache/key.go","line":18,"anchor":"7f3c9d12","sha":"abc1234"} -->

The standard library has `strings.Join` for this; the manual `+` loop is harder to read.

```suggestion:-0+0
return strings.Join(parts, ":")
```
````

---

## Delivery mode — inline body (example C: `question`, env file)

A targeted question — anchored on one new-side line, no `suggestion` block, just a concrete hypothesis the author can answer yes/no.

````markdown
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"question","file":"env_files/all/grafana.env","line":9,"anchor":"b2c3d4e5","sha":"abc1234"} -->

The migration notes call for `fake|alloy` during cutover, but this default is `fake` only. Is `alloy` intentionally omitted here (environment-specific override), or should the default match the documented cutover list?
````

---

## Delivery mode — inline body (example D: custom-rule, Python)

A `custom-rules` finding cites the rule it enforces in the prose, so the author sees *why* it was flagged. It still uses one of the standard archetypes (here `question`) and goes through the same dedup + adjudication as built-in findings.

````markdown
<!-- daiv-cr {"v":1,"kind":"inline","archetype":"question","file":"payments/client.py","line":34,"anchor":"c4d5e6f7","sha":"abc1234"} -->

Per your `.agents/review-rules.md` ("every external call in `payments/` must set an explicit timeout"): this `httpx.get` has no `timeout`. Intentional (inheriting a client default), or should it set one?
````

---

## Delivery mode — summary discussion body

The summary collects every **discussion-only** finding plus a one-line index of the inline findings posted this run. On re-review, this exact body is updated in place — never appended.

````markdown
<!-- daiv-cr {"v":1,"kind":"summary","sha":"abc1234"} -->

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

## Questions

**3. Does the new ingestion pipeline preserve ordering across the three modules, or is reordering OK?** — multiple files

<details>
<summary>Details</summary>

The refactor moves dedup into `ingest/wavecom.py` and removes the explicit sort step from two callers. The change spans more than one file, so it can't anchor on a single diff line. Is the looser ordering intentional, or did the dedup move accidentally drop the sort?

</details>

## Inline findings posted (3)

- `services/api.py:42` — drop explicit default `timeout=30` *(remove_dead_lines)*
- `internal/cache/key.go:18` — use `strings.Join` instead of manual loop *(use_framework_idiom)*
- `env_files/all/grafana.env:9` — is `alloy` intentionally omitted from the default tenant list? *(question)*

_Detectors: 5/5 completed · 3 of 11 candidates posted (2 merged across detectors)._
````

---

## What goes inline vs in the summary

Two inline shapes:

- **Fix archetypes** — one- or two-line fixes that map cleanly to a `suggestion` block: `remove_dead_lines`, `use_framework_idiom`, `replace_with_constant`, `swap_library_call`. The body is the suggestion; the prose is one or two sentences of justification, no more.
- **Question archetype** — targeted questions anchored on a single new-side line or a contiguous new-side range within one hunk. No `suggestion` block — just marker + one or two sentences ending in `?`. The reader sees the question on the exact line(s) in the diff view. When the question is about a multi-line block, scope the position to the full range and compute the anchor on the first new-side line of that range.

Everything else goes in the summary: findings spanning multiple lines or files, architectural / placement concerns, **renames** (a `suggestion` block can only patch the declaration, not the call sites), questions without a single-line anchor (cross-cutting concerns), anything that needs prose to land. If a finding's diff position can't be reliably constructed (renamed file, line moved, hunk anchor unclear), demote it to the summary — never post a misaligned inline suggestion or misanchored question.
