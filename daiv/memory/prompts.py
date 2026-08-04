from langchain_core.prompts import HumanMessagePromptTemplate, SystemMessagePromptTemplate

extraction_system = SystemMessagePromptTemplate.from_template(
    """You analyze the transcript of a finished coding-agent run and extract observations worth remembering
for FUTURE runs on the same repository.

An observation is worth keeping ONLY if it is ALL of:
- specific and verifiable: names a real command, file, flag, convention, or behavior;
- durable: likely to still be true in a future session on this repository;
- hard-won: the agent could NOT trivially rediscover it by reading the repository's docs or file tree.

Categories:
- build_test: exact commands that worked or failed, and why (e.g. required env vars, flags, working directory)
- codebase_fact: non-obvious facts about structure or behavior discovered through investigation
- pitfall: dead ends, wrong assumptions, approaches that wasted effort or broke things
- reviewer_preference: corrections, preferences, or rejections expressed by users or reviewers
- workflow: process conventions discovered (branch naming, MR etiquette, CI quirks)

Hard rules:
- Most runs teach nothing new: returning ZERO observations is the normal, expected outcome.
- NEVER invent generic advice ("write tests", "follow code style", "check the docs").
- NEVER restate the task itself, its diff, or its outcome summary.
- NEVER include secrets, tokens, or credentials.
- Each observation must stand alone: a future agent reads it without this transcript.
- Maximum 10; prefer 0-3 high-value observations over many weak ones.""",
    "mustache",
)

extraction_human = HumanMessagePromptTemplate.from_template(
    """Repository: {{repo_id}}
Run finished with status: {{status}}

Run transcript (roles, text, tool calls; long outputs truncated):
~~~
{{transcript}}
~~~

Extract the observations worth remembering for future runs on this repository.
Return an empty list if there are none.""",
    "mustache",
)

consolidation_system = SystemMessagePromptTemplate.from_template(
    """You maintain the long-term memory of a code repository. Memory is a set of individual
entries; each is injected into a coding agent's system prompt before every future run, so every
entry must earn its place.

You are given the repository's current entries and a batch of new observations extracted from
recent runs. Decide what each observation means for the entries and return the operations that
express it. You do NOT write the memory document — it is rendered from the entries by code.

Operations:
- ADD(observation_ids, category, content) — the observation states a fact no entry covers yet.
- UPDATE(entry_ids=[one], observation_ids, content) — the observation corrects, contradicts or
  sharpens exactly one entry. The new content replaces that entry entirely, so it must stand
  alone. Prefer UPDATE over ADD whenever an entry already covers the same ground.
- MERGE(entry_ids=[two or more], observation_ids, content) — several entries of the SAME
  category are fragments of one fact; combine them into one. The merged entry keeps that
  category, so do not supply one. Never merge across categories.
- CONFIRM(entry_ids=[one], observation_ids) — the observation restates a fact an entry already
  captures correctly. Nothing changes; this is the normal outcome for a duplicate.
- DISCARD(observation_ids, reason) — the observation is not worth keeping.

Rules:
- Every operation MUST name at least one observation from the batch, and every observation
  SHOULD be covered by exactly one operation.
- Copy entry and observation IDs verbatim. An operation naming an ID that is not in the lists
  below is rejected and its observations are re-queued, so never invent or reformat one.
- Entries you do not name are left exactly as they are. There is no operation that rewrites
  memory as a whole, and you must not attempt one.
- DISCARD is a decision, not a fallback: reject an observation when it is ephemeral (a one-off
  error count, a resolved incident, the state of one run), generic advice, a restatement of a
  task, or specific to a deployment rather than the repository. Always give a reason — a
  DISCARD without one is rejected.
- Keep content specific, verifiable and self-contained: at most 500 characters, plain text, no
  markdown headings or bullets.""",
    "mustache",
)

consolidation_human = HumanMessagePromptTemplate.from_template(
    """Repository: {{repo_id}}

{{#entries}}
Current memory entries (id | category | last confirmed | content):
{{entries}}
{{/entries}}
{{^entries}}
This repository has no memory entries yet; everything worth keeping is an ADD.
{{/entries}}

New observations, oldest first (id | category | date | content):
{{observations}}

Return the operations to apply.""",
    "mustache",
)
