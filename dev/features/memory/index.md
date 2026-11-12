# Repository memory

Repository memory is what DAIV remembers about a repository from its past runs: how the project builds and tests, facts about the code, mistakes to avoid and what reviewers ask for. Later runs on the same repository start with that knowledge, so you do not repeat it in each prompt. Memory is per repository, and DAIV never shows what it learned on one repository to a run on another.

## How memory forms

Memory forms in three steps, none of which needs you to act.

1. **Extraction.** When a run finishes, DAIV reads the session's transcript and asks the extraction model for candidate observations. It stores each one as pending, under one of five categories:

   | Category                | What it captures                                |
   | ----------------------- | ----------------------------------------------- |
   | **Build & test**        | How the project builds, tests and lints         |
   | **Codebase fact**       | Durable facts about the code and its layout     |
   | **Pitfall**             | Mistakes and traps a run should avoid repeating |
   | **Reviewer preference** | What the repository's reviewers ask for         |
   | **Workflow**            | Conventions for working on the repository       |

1. **Consolidation**, which DAIV also calls *dreaming*. An hourly sweep finds the repositories that are due, and the consolidation model folds their pending observations into the repository's entries. [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/#memory) lists the settings that choose the extraction and consolidation models. For each observation it adds a fact, corrects or merges existing entries, confirms a duplicate, or discards noise. DAIV rewrites only the entries an operation names.

1. **Injection.** DAIV renders the surviving entries into one document, grouped by category in a fixed order with one line per entry. No model is involved in rendering, so the same entries always give the same document. A later run on the repository gets the document appended to its system prompt, under the header `# Learned repository memory (auto-generated from past runs; may be stale — verify before relying on it)`. A [multi-repository session](https://docs.daivagent.com/dev/features/jobs-api/#multi-repository-sessions) gets the main repository's memory only.

### What a run contributes

DAIV extracts observations from every finished run, failed ones included. It extracts nothing from:

- A run started from the dashboard chat.
- A run in a [multi-repository session](https://docs.daivagent.com/dev/features/jobs-api/#multi-repository-sessions), whose transcript holds the other repositories' code.
- A run whose session's working state expired before DAIV read it. [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/#redis) gives how long that state lives.
- A run that failed or was cancelled before it started.
- A run in a session where the agent has not replied yet.

### When a repository is consolidated

A repository is due when either of these holds:

- It has at least a set number of pending observations.
- Its oldest pending observation is older than a set number of days, so a quiet repository still forms memory.

In both cases DAIV consolidates only if the last attempt was longer ago than the minimum interval. A failed attempt counts, so a repository whose rounds keep failing is retried at the same pace as a healthy one. DAIV applies at most 50 operations from a round. Observations named only by the rest stay pending for the next round.

[Environment variables](https://docs.daivagent.com/dev/reference/env-variables/#memory) gives the thresholds and their defaults.

## Read a repository's memory

**Memory**, in the sidebar's **Settings** menu under **Agent setup**, lists the repositories you can read that have memory activity. Each row shows whether the repository has a document (**Document** or **No document**), when it was last consolidated, how many observations are pending and how many exist in total. Search by repository name, or filter by **All**, **Document**, **No document** or **Pending**.

Select a repository to open its page:

- The header shows when the repository was last consolidated and the document's size in lines and bytes. Before the first consolidation it reads **Never consolidated**.
- **Consolidated document** shows the text DAIV adds to later runs.
- **Observations** lists every observation with its status (**Pending**, **Consolidated** or **Discarded**) and its category. Filter by either, and select **View source run** to open the run it came from.

### Who can read a repository's memory

You can read a repository's memory when you have read access to the repository on your Git platform, and admins can read every repository's. A repository you cannot read does not appear in the list, and its page returns a 404. A pending observation can quote the repository's code, which is why read access is the rule. [Accounts and roles](https://docs.daivagent.com/dev/getting-started/accounts/#repository-access) explains how DAIV learns your access.

## Consolidate a repository now

An admin can run a round without waiting for the hourly sweep. The round ignores the thresholds and the minimum interval. You need memory on site-wide, because the **Consolidate now** button is absent while it is off.

1. In the sidebar, select **Settings** → **Memory**.
1. Select the repository.
1. Select **Consolidate now**.

DAIV queues the round and shows `Consolidation queued for <repository> (<n> pending observation(s)).`. It queues nothing in two cases and says why:

- There are no pending observations: `Nothing to consolidate for <repository> — no pending observations.`
- The repository has a stored document but no entries: `` <repository> has a memory document but no entries, so consolidating would discard it. An administrator must run `backfill_memory_entries --repo-id <repository>` on the server first. ``

If the repository's `.daiv.yml` sets `memory.enabled` to `false`, the button still shows and DAIV still reports the round as queued, but the round skips the repository.

From a shell, [`consolidate_memory`](https://docs.daivagent.com/dev/reference/management-commands/#consolidate_memory) consolidates one repository in the foreground, and refuses to run while fewer than 10 observations are pending unless you pass `--force`. [`backfill_memory_entries`](https://docs.daivagent.com/dev/reference/management-commands/#backfill_memory_entries) rebuilds a repository's entries after an upgrade or a repair.

## Turn memory off

Memory has two switches, and a repository's memory works only while both are on:

- The site-wide switch, in the **Memory** group of the site configuration. [Site configuration](https://docs.daivagent.com/dev/reference/site-configuration/#setting-groups) lists the group, and [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/#memory) lists the `DAIV_MEMORY_*` variables that set the same values.
- A repository's `memory.enabled` in `.daiv.yml`. [Repository configuration](https://docs.daivagent.com/dev/customization/repository-config/#feature-toggles) describes it. A repository can opt out, but it cannot switch memory on where the site has it off.

While memory is off, DAIV extracts and consolidates nothing, and later runs get no document. The document and observations stay, and the dashboard shows them.

## Size limit

The document has a limit in lines and in bytes, 200 lines and 10240 bytes by default, so memory stays a short briefing and not an archive. [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/#memory) gives the settings that change them.

While the document fits, DAIV evicts nothing. When a round pushes it over a limit, DAIV evicts the least recently confirmed entry of the category with the most text, and repeats until the document fits. A small category is never drained to make room for a large one. DAIV never evicts the last remaining entry, even if it alone exceeds the limit. An evicted entry no longer appears in the document, and the dashboard does not show it.
