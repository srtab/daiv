---
description: "DAIV learns from its finished runs: candidate observations are extracted from each run, consolidated into a small per-repository document, and fed back into later runs on that repository."
---

# Repository Memory

DAIV remembers what it learns on a repository. When a run finishes, DAIV reads its transcript and extracts candidate learnings — how the project builds, facts about the codebase, mistakes to avoid, what its reviewers ask for. A periodic consolidation pass folds those candidates into a small, curated document, and later runs on the same repository start with that document in their context, headed *"Learned repository memory (auto-generated from past runs; may be stale — verify before relying on it)"*.

Memory is per repository: nothing learned on one repository is ever shown to a run on another.

---

## How it works

1. **Extraction** — when a run finishes, a background task reads the run's transcript and asks the extraction model for candidate observations. Each observation is stored as *pending*, under one of five categories:

    | Category | What it captures |
    |---|---|
    | Build & test | How the project builds, tests and lints |
    | Codebase fact | Durable facts about the code and its layout |
    | Pitfall | Mistakes and traps a run should avoid repeating |
    | Reviewer preference | What the repository's reviewers ask for |
    | Workflow | Conventions for working on the repository |

    Transcripts live behind a time-to-live, so extraction runs promptly; a run whose transcript has already expired is skipped silently. A [multi-repository session](jobs-api.md) adds no observations — its transcript holds the other repositories' code.

2. **Consolidation ("dreaming")** — an hourly sweep looks for repositories that are due: at least `memory_consolidation_min_pending` observations pending, **or** the oldest pending observation older than `memory_consolidation_max_pending_age_days` (so a quiet repository still forms memory eventually) — and in both cases only if the last attempt is older than `memory_consolidation_min_interval_hours`. For a due repository, the consolidation model decides per observation: add a fact, correct or merge existing entries, confirm a duplicate, or discard noise. Only the entries an operation names are rewritten.

3. **The document** — after each round the document is re-rendered from the surviving entries: one line per entry, grouped under its category heading in a fixed order. Rendering is deterministic — no model is involved — so the same entries always produce the same document.

If memory is off — site-wide or for the repository — extraction and consolidation both skip, and the repository's document stops growing.

---

## The dashboard

**Memory** in the dashboard (`/dashboard/memory/`) lists every repository you can read that has memory activity: its observation totals, how many are still pending, whether a document exists, and when it was last consolidated. Repositories can be filtered by name and by status.

A repository's page shows the consolidated document with its current size against the caps, and every observation with its status (*pending*, *consolidated*, *discarded*) and category, filterable by both.

An admin can run a round on demand with **Consolidate now** on the repository's page instead of waiting for the hourly sweep. The same round is available from the shell as the `consolidate_memory` management command, and `backfill_memory_entries` rebuilds a repository's entries from its consolidated observations after an upgrade or a repair.

---

## Settings

### Site-wide

Configure under **Configuration → Memory** in the dashboard (admin only). Every setting can also be set via environment variable or Docker secret, which takes precedence over the UI.

| Setting | Default | Description |
|---------|---------|-------------|
| `memory_enabled` | `true` | Master switch for repository memory: when on, finished runs yield observations and due repositories are consolidated into memory documents. Env var: `DAIV_MEMORY_ENABLED`. |
| `memory_extraction_model_name` | `openrouter:openai/gpt-6-luna` | Model that reads each finished run's transcript and proposes memory observations. Env var: `DAIV_MEMORY_EXTRACTION_MODEL_NAME`. |
| `memory_extraction_fallback_model_name` | `openrouter:anthropic/claude-haiku-5.5` | Used when the extraction model is unavailable. Env var: `DAIV_MEMORY_EXTRACTION_FALLBACK_MODEL_NAME`. |
| `memory_consolidation_model_name` | — | Model that folds pending observations into a repository's memory entries. Leave empty to use the extraction model. Env var: `DAIV_MEMORY_CONSOLIDATION_MODEL_NAME`. |
| `memory_consolidation_min_pending` | `10` | A repository becomes due for consolidation once this many observations are waiting. Env var: `DAIV_MEMORY_CONSOLIDATION_MIN_PENDING`. |
| `memory_consolidation_max_pending_age_days` | `7` | However few are waiting, a repository is due once its oldest pending observation is this old. Env var: `DAIV_MEMORY_CONSOLIDATION_MAX_PENDING_AGE_DAYS`. |
| `memory_consolidation_min_interval_hours` | `24` | Minimum hours between consolidation attempts for the same repository — the real throttle on the hourly sweep. Env var: `DAIV_MEMORY_CONSOLIDATION_MIN_INTERVAL_HOURS`. |
| `memory_max_lines` | `200` | Hard cap on the rendered document's line count. Env var: `DAIV_MEMORY_MAX_LINES`. |
| `memory_max_bytes` | `10240` | Hard cap on the rendered document's size in bytes. Env var: `DAIV_MEMORY_MAX_BYTES`. |

### Per repository

The only per-repository switch is `memory.enabled` in `.daiv.yml`; models, thresholds and caps are site-wide only.

```yaml
memory:
  enabled: false
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `memory.enabled` | `bool` | site-wide | Set to `false` and this repository grows no memory: its runs yield no observations and it is never consolidated. It cannot switch memory back on where the operator turned it off site-wide. |

See [Repository Config](../customization/repository-config.md) for the full `.daiv.yml` reference.

---

## Size caps

The rendered document is capped at `memory_max_lines` lines and `memory_max_bytes` bytes so memory stays a short briefing, not an archive. While the document fits, nothing is evicted. When a consolidation round pushes it over budget, the largest category gives up its least-recently-confirmed entry, repeatedly, until it fits again — so a small category is never drained to make room for a large one. Evicted entries are superseded, not deleted: they stay in the repository's history but no longer render. The last remaining entry is never evicted.

---

## Related pages

- [Accounts & Roles](../getting-started/accounts.md) — read access to a repository is what gates its memory pages
- [Jobs API](jobs-api.md) — multi-repository sessions read the main repository's memory but add no observations
- [Site Configuration](../reference/site-configuration.md) — the full settings reference
