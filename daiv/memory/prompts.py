from langchain_core.prompts import HumanMessagePromptTemplate, SystemMessagePromptTemplate

from memory.schemas import CONTENT_GUIDELINE_CHARS, MAX_OBSERVATIONS, MAX_OPERATIONS

# Each reject is paired with its nearest good neighbour: a list of bad examples alone teaches a
# category, not a boundary. Deliberately about a generic project — the prompt runs against every
# repository DAIV serves, so the model must learn the boundary, not one repository's vocabulary.
EXTRACTION_FEW_SHOTS = """Worked examples. Each pair is one boundary; the two sides are close on purpose.

REJECT: "The API returned rate-limit errors on two requests during this run."
KEEP:   "The API enforces a 60-requests-per-minute limit per token; a burst of parallel calls
         trips it, and every request in the burst gets a 429 until the window resets."
  — the first is the state of one run, the second is a limit that will trip again the same way.

REJECT: "Prefer descriptive variable names over abbreviations."
KEEP:   "A new non-null database column needs two migrations, one nullable and one backfill,
         because the deploy applies migrations before the new code is running."
  — the first is advice for any project, the second is this project's deploy order.

REJECT: "The last deploy touched 340 lines across nine files."
KEEP:   "The asset build is not reproducible across Node major versions; pin the version from the
         tooling config or the bundle hashes change between machines."
  — the first is a count describing one change, the second is why a build varies across machines.

REJECT: "Check the documentation before changing this module."
KEEP:   "Generated client code is overwritten by the codegen step, so a change has to go into the
         template rather than the generated file."
  — the first is a gesture at where to look, the second is what would have been rediscovered the hard way."""

CONSOLIDATION_FEW_SHOTS = """Worked examples.

MERGE — two entries are fragments of one fact:
  entries:      a1 | workflow | The release tag must be pushed after the changelog commit.
                a2 | workflow | Release tags are what triggers the publish pipeline.
  observation:  b1 | workflow | Tagging before the changelog commit publishes a release whose
                notes are empty.
  MERGE(entry_ids=[a1, a2], observation_ids=[b1], content="The release tag triggers the publish
  pipeline, so push it only after the changelog commit — tagging first publishes a release with
  empty notes.")

DISCARD — a decision, not a fallback:
  observation:  b2 | build_test | The nightly job timed out twice this week, then passed.
  DISCARD(observation_ids=[b2], reason="the state of a few runs; nothing here holds for a future
  session")"""

# The two system templates take no mustache variables, so they can be f-strings; the ``*_human``
# ones below must not be — an f-string would collapse their ``{{var}}`` placeholders to ``{var}``.
extraction_system = SystemMessagePromptTemplate.from_template(
    f"""You analyze the transcript of a finished coding-agent run and extract observations worth remembering
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
- Maximum {MAX_OBSERVATIONS}; prefer 0-3 high-value observations over many weak ones.

{EXTRACTION_FEW_SHOTS}""",
    "mustache",
)

extraction_human = HumanMessagePromptTemplate.from_template(
    """Repository: {{repo_id}}
Run finished with status: {{status}}
{{#memory}}

What this repository's memory already records:
~~~
{{{memory}}}
~~~

A fact above is NOT automatically off-limits. Decide per fact based on what the run itself did
with it:
- the run CONTRADICTS it → emit the NEW fact; this is how a stale entry gets corrected.
- the run RE-VERIFIED it — by running a command, hitting a pitfall again, or reading the code,
  schema, or config that *defines* the fact (not a document that only asserts it) and finding it
  still holds → emit it; this is how a fact stays confirmed. Seeing the fact in this block does
  not make it "trivially rediscoverable" — the run's own act of exercising it is what earns the
  confirmation.
- the run merely read the fact above and did neither of the above → emit nothing about it.
When you cannot tell which applies, emit it: a redundant confirmation costs little, a missed one
freezes the fact's confirmation date. None of this licenses lazy restatement — a fact above with
nothing in the run that tested it is still not an observation.
{{/memory}}

Run transcript (roles, text, tool calls; long outputs truncated):
~~~
{{{transcript}}}
~~~

Extract the observations worth remembering for future runs on this repository.
Return an empty list if there are none.""",
    "mustache",
)

consolidation_system = SystemMessagePromptTemplate.from_template(
    f"""You maintain the long-term memory of a code repository. Memory is a set of individual
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
- Keep content specific, verifiable and self-contained: at most {CONTENT_GUIDELINE_CHARS} characters,
  plain text, no markdown headings or bullets.
- Return at most {MAX_OPERATIONS} operations. If the batch needs more, cover the OLDEST observations
  first and prefer MERGE and UPDATE over ADD; anything you leave out is re-queued for the next round.
- Several observations in this batch may state the same fact. Cover them with ONE operation
  naming all of their ids, rather than one operation each.

{CONSOLIDATION_FEW_SHOTS}""",
    "mustache",
)

consolidation_human = HumanMessagePromptTemplate.from_template(
    """Repository: {{repo_id}}

{{#entries}}
Current memory entries (id | category | last confirmed | content):
{{{entries}}}
{{/entries}}
{{^entries}}
This repository has no memory entries yet; everything worth keeping is an ADD.
{{/entries}}

New observations, oldest first (id | category | date | content):
{{{observations}}}

Return the operations to apply.""",
    "mustache",
)
