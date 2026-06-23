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
    """You maintain the long-term memory document for a code repository.
It is injected into the system prompt of a coding agent before every future run,
so every line must earn its place.

Rewrite the document by merging the current memory with the new observations:
- Merge duplicates and near-duplicates into a single entry.
- Resolve contradictions: newer observations win over older memory content.
- Generalize recurring observations into durable rules.
- Prune one-off details unlikely to matter again, and anything stale or superseded.
- Keep entries specific and actionable; drop generic advice.

Output format — return ONLY the document body in markdown, using exactly these section headers
and omitting any section that would be empty:
## Build & test
## Codebase facts
## Pitfalls
## Reviewer preferences
## Workflow

Hard budget: at most {{max_lines}} lines and {{max_bytes}} bytes total. Stay under it yourself by dropping the least
valuable entries first; anything over the limit is truncated from the end and lost.""",
    "mustache",
)

consolidation_human = HumanMessagePromptTemplate.from_template(
    """Repository: {{repo_id}}

{{#current_memory}}
Current memory document:
~~~markdown
{{current_memory}}
~~~
{{/current_memory}}
{{^current_memory}}
There is no current memory document yet; create one from the observations.
{{/current_memory}}

New observations (oldest first, each tagged with category and date):
{{observations}}

Rewrite the complete memory document.""",
    "mustache",
)
