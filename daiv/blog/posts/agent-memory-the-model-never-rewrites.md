---
title: "Agent memory the model never gets to rewrite"
date: 2027-02-23
summary: "In DAIV's repository memory the model proposes named changes, code applies them to append-only rows, and code renders what later runs read."
author: Sandro Rodrigues
image: memory-flow.png
---

If a model maintains its own memory file, each rewrite can drop a fact it judged unimportant or change one quietly, and nothing records what changed. The memory described here works the other way round: the model writes the text of new and replacement entries, code checks each change and keeps every replaced version on record, and code renders the document the next run reads, the same way every time. What follows shows each part in the code, and the mistakes it still lets through.

DAIV is an open-source coding agent for GitLab and GitHub that you host yourself, with [the model you choose](https://docs.daivagent.com/latest/getting-started/llm-providers/), including one served by your own vLLM or llama.cpp server. Its repository memory is what it learns about each repository from its past runs.

Here is one entry from the rendered document for DAIV's own repository, on a development instance, as a later run receives it under the heading `## Build & test`:

> tests/unit_tests/test_docs_links.py::test_all_docs_pages_reachable does a BFS from docs/index.md over relative .md links only; mkdocs nav membership does not count. A new docs page must be linked from a reachable page (e.g. docs/community.md) or the test fails with 'pages not reachable'.

A model wrote that text from what earlier runs observed, and it matches the test. Code checked the change the model proposed, applied it, and rendered the document, one line per entry.

## Four steps, two of them without a model

Memory forms without anyone acting ([memory docs](https://docs.daivagent.com/latest/features/memory/#how-memory-forms)):

| Step | Who does it | What it produces |
|---|---|---|
| Extraction, when a run finishes | A model reads the session's transcript | Pending observations, each in one of five categories |
| Proposal, in an hourly sweep | A model reads the pending observations and the current entries | Operations on named entries |
| Checking and applying | Code | New entries, and old ones marked superseded |
| Rendering | Code | The document later runs on the repository receive |

![A diagram of four steps. A model extracts observations from a finished run's transcript; they stay pending until consolidated. A model proposes operations (ADD, UPDATE, MERGE, CONFIRM, DISCARD); each names observations by id, and UPDATE, MERGE and CONFIRM also name entries. Code checks each operation, rejects what doesn't fit the round, and applies the rest to entries whose text never changes: a change adds a new entry and supersedes the old one. Code renders the document later runs read: the same entries give the same document, and over 200 lines or 10,240 bytes the largest category drops its least recently confirmed entry.](memory-flow.svg)

The five categories are build and test, codebase facts, pitfalls, reviewer preferences and workflow. The extraction model is asked for at most 10 observations per run, and an empty list is the expected answer when a run taught nothing new.

## When a round runs

A repository is due for consolidation when it has at least 10 pending observations, or when its oldest pending observation is 7 days old, so a quiet repository still forms memory. Either way, a round runs only if the last attempt on that repository was at least 24 hours ago ([environment reference](https://docs.daivagent.com/latest/reference/env-variables/#memory)). Those are the defaults, and the `DAIV_MEMORY_*` variables change them.

A failed attempt counts as an attempt. A repository whose rounds keep failing is therefore retried at the same pace as a healthy one, not on every hourly sweep ([models.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/models.py#L159-L161)). An admin can also run a round at once from the repository's memory page, which skips the thresholds and the interval. From a shell, the `consolidate_memory` command runs one in the foreground, and refuses while fewer than 10 observations are pending unless it is given `--force`.

One case is refused outright. A repository whose document predates entries has a stored document but no entries, and a round there would render the document from nothing and so discard it ([consolidation.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/consolidation.py#L374-L380)). The memory page queues nothing and says that an administrator must run `backfill_memory_entries` first to rebuild the entries. The hourly sweep can still queue a round, but the worker skips it and logs the same instruction.

## The model proposes, code checks

The consolidation model can propose five operations. ADD, UPDATE and MERGE also carry the full text of the entry they create:

```python
op: MemoryOperationLiteral = Field(
    description=(
        "ADD: create a new entry from the observations. "
        "UPDATE: replace exactly one existing entry with corrected/expanded content. "
        "MERGE: replace two or more existing entries of the SAME category with one combined entry. "
        "CONFIRM: the observations restate an existing entry, nothing to change. "
        "DISCARD: the observations are not worth keeping."
    )
)
```

([schemas.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/schemas.py#L82-L90))

Each operation names, by id, the observations that motivated it, and for UPDATE, MERGE and CONFIRM the entries it targets. The ids come from the lists the model was given for this round, so an operation cannot reach an entry or an observation the model was never shown. Every operation must name at least one observation ([schemas.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/schemas.py#L137-L160)).

Before anything is written, code checks each operation against the round ([consolidation.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/consolidation.py#L80-L102)). Among other checks, an operation is rejected when it:

- names an observation outside this round, or one an earlier operation in the round already used;
- names an entry that does not exist or is no longer active;
- targets an entry an earlier operation in the round already changed;
- merges entries from different categories;
- has the wrong number of entries for its kind, an ADD without a category, or text missing where its kind needs it;
- discards observations without a reason.

A rejected operation is logged with its reason. Its observations stay pending for a later round, unless an accepted operation in the same round also names them. When half or more of a round's operations are rejected, DAIV logs a warning that the model's output is degraded, and when all of them are, it applies nothing and logs an error ([consolidation.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/consolidation.py#L131-L160)). A round applies at most 50 operations; observations named only by the rest wait for the next one ([consolidation.py, the cap](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/consolidation.py#L359-L366)).

## An entry's text never changes

An entry is one fact, and its text is never edited after it is written:

```python
class MemoryEntry(models.Model):
    """A single durable fact in a repository's memory — the source of truth for the document.

    Entries are append-only in content: a change creates a successor (or none, for eviction) and
    marks the replaced row ``SUPERSEDED`` rather than rewriting it, so the history of every fact
    stays queryable. :meth:`confirm` is the one in-place write; ...
    """
```

([models.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/models.py#L79-L86))

An UPDATE writes a new entry and marks the old one superseded, pointing at its successor; a MERGE does the same for two or more entries at once. A CONFIRM changes no text: it records when the fact was last confirmed, which later decides what gets evicted, and links the new observations to the entry. An entry a round creates links to the observations behind it and to the run behind the newest of them; entries that DAIV's upgrade built by parsing an older document have neither link.

So the model does write memory text: the full text of every new entry, and with UPDATE or MERGE it chooses which entries that text replaces. What it cannot do is change an entry without the old version staying on record, or write the document. Its own instructions say so: "There is no operation that rewrites memory as a whole" ([prompts.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/prompts.py#L150-L157)). Its operations touch only the entries they name, but a new or longer entry can push the document over budget, and code then evicts an entry no operation named.

## The document is rendered, never written

The text a later run receives is neither stored by the model nor summarised by it. Code renders it from the active entries:

```python
def render_memory_document(entries: Iterable[MemoryEntry]) -> str:
    """Render active entries into the document injected into agent runs.

    Deterministic by construction — fixed section order, entries by creation time then id (a
    round stamps one timestamp on all of its entries), whitespace collapsed so one entry is
    always exactly one line. No model involvement.
    """
```

([render.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/render.py#L28-L34))

The same entries always give the same document, so a document that changed between two runs means the entries changed, and the entries say how. The stored document is a cache of that render, kept because it is where runs read it from. A later run gets it appended to its system prompt, under a header that says it "may be stale — verify before relying on it" ([memory middleware](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/automation/agent/middlewares/memory.py#L33)).

The new entries, the observations' new statuses, any evictions and the new document commit in one database transaction. A round that fails leaves the observations as they were and the stored document untouched ([consolidation.py, apply](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/consolidation.py#L131-L205)).

## A budget that evicts the stale, not the small

The document is limited to 200 lines and 10,240 bytes by default, so it stays a short briefing ([constants.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/constants.py#L8-L9)). While it fits, nothing is evicted. When a round pushes it over, code removes entries until it fits:

```python
while len(kept) > 1 and not _fits_budget(render_memory_document(kept), max_lines=max_lines, max_bytes=max_bytes):
    category = _largest_category(kept)
    victim = min((entry for entry in kept if entry.category == category), key=_eviction_order)
    kept.remove(victim)
    evicted.append(victim)
```

([render.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/memory/render.py#L76-L96))

Each pass takes the category with the most text and evicts its least recently confirmed entry. A small category, say two reviewer preferences, is never emptied to make room for a long list of codebase facts. The last remaining entry is never evicted: the code's own reasoning is that overshooting the budget by one line beats erasing the repository's memory.

## What gets remembered

- **Per repository.** What DAIV learns on one repository never reaches a run on another. A run in a session that spans several repositories is not used at all, because its transcript holds the other repositories' code.
- **Not from every run.** A dashboard chat turn starts no extraction, nor does a run that failed or was cancelled before it started, and a session whose working state expired, or where the agent never replied, is skipped. A run that started and then failed is used, failure and all. Extraction reads the whole session, not only the run that finished, so a later job, webhook or CI-fix run in the same session can learn from its earlier chat turns.
- **Readable with repository access.** A repository's memory page shows its document and every observation, with a link to the run it came from while that run exists, to anyone with read access to the repository. A pending observation can quote the code, which is why read access is the rule.
- **Off in two places.** A site-wide switch and a repository's `memory.enabled` in `.daiv.yml`. A repository can opt out, but not opt in where the site is off.

## What this does not protect against

- **A correct fact replaced by a wrong one.** An UPDATE or MERGE that passes the checks takes its old entries out of the document. The old text stays in the database, but nothing in DAIV shows it, so a good fact can drop out of what runs read.
- **A wrong fact, worded well.** The checks catch operations that do not fit the round, not facts that are false. Nothing compares an entry with the code; the header's "may be stale — verify" asks the agent to.
- **Injected text.** As I read it, the extraction model reads transcripts that include text other people wrote: issues, review comments, files. An instruction planted there could become an observation, and then an entry that later runs receive.
- **Lost entries.** An evicted or superseded entry stays in the database, and an eviction is logged with each entry's id. Getting one back means a query.
- **Slow forgetting.** An entry that stops being true stays until a later observation corrects it or the budget evicts it.

## Trying it

Memory is on by default. After DAIV's first runs on a repository, open **Settings** → **Memory** and select the repository. **Observations** lists what each run contributed, with a link to the run. Once a round has run, **Consolidated document** shows the text later runs receive, and an admin can select **Consolidate now** rather than wait for the hourly sweep to find the repository due.

The [memory page](https://docs.daivagent.com/latest/features/memory/) of the docs covers the size limit and switching memory off, and the `DAIV_MEMORY_*` variables in the [environment reference](https://docs.daivagent.com/latest/reference/env-variables/#memory) set the thresholds and the models.
