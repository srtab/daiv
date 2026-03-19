from langchain_core.prompts import HumanMessagePromptTemplate, SystemMessagePromptTemplate

system = SystemMessagePromptTemplate.from_template(
    """You are a senior software engineer generating Pull Request metadata. Current date: {{current_date}}.

You MUST follow these rules:
1) Source of truth is ONLY:
   - memory content (if provided)
   - git diff hunks (if provided)
   - optional context fields explicitly provided by the user (e.g., issue id)
2) Do NOT invent changes, motivations, tests, or impacts not supported by the diff.
   - Compare the before and after lines carefully.
   - Only mention items that actually differ between the two.
3) Be specific: name the actual entities, values, or operations that changed.
   - Never use vague verbs like "improve", "update", or "enhance"
     when you can state what concretely changed.
4) If memory specifies branch naming or commit message conventions,
   you MUST follow them — they override ALL defaults below.
   - Pay close attention to required prefixes, delimiters, and casing rules.
   - If multiple conventions exist, choose the one that best matches the change type.
   - If conventions are ambiguous, choose the safest option and keep it simple.
5) If the additional context or diff references an issue/ticket identifier
   (e.g., ABC-123, CAL-204), incorporate it into branch and commit_message
   following the memory conventions.
   - If no convention exists, prefix: "<TICKET-ID> <type>: <summary>".
6) Only if memory is missing or has no relevant guidance, fall back to these defaults:
   - branch: <type>/<short-kebab-summary> where type ∈ {feat, fix, chore, docs, refactor, test}
   - commit_message: Conventional Commits style "<type>: <short summary>" (subject only)
""",
    "mustache",
)

human_pr_metadata = HumanMessagePromptTemplate.from_template(
    """Generate PR metadata from the memory and code changes.

Diff hunks (unified diff; may include multiple files):
~~~diff
{{pr_metadata_diff}}
~~~

{{#extra_context}}
Additional context related to the changes:
~~~markdown
{{extra_context}}
~~~
{{/extra_context}}

Field rules:
- title: short PR title (max ~70 chars), based strictly on the diff.
- description: Markdown with:
  1) a brief overview paragraph (1-3 sentences)
  2) a "**Key Changes:**" section with up to 6 bullet points
  Focus only on describing the actual code changes visible in the diff.
  Do NOT include meta-commentary about the issue, prompt, or source of information.
- commit_message:
  - MUST follow the memory convention if one exists (including any ticket/issue prefix or wrapper).
  - Otherwise use: "<type>: <summary>" (Conventional Commits), single line.
- branch:
  - MUST follow the memory convention if one exists (including any required issue-id segments).
  - Otherwise use: "<type>/<kebab-case-summary>".
  - Keep it lowercase, ascii, no spaces, avoid > 50 chars.""",
    "mustache",
)


human_commit_message = HumanMessagePromptTemplate.from_template(
    """Generate a commit message from the memory and code changes.

Diff hunks (unified diff; may include multiple files):
~~~diff
{{commit_message_diff}}
~~~

{{#extra_context}}
Additional context related to the changes:
~~~markdown
{{extra_context}}
~~~
{{/extra_context}}

Field rules:
- commit_message:
  - MUST follow the memory convention if one exists (including any ticket/issue prefix or wrapper).
  - Otherwise use: "<type>: <summary>" (Conventional Commits), single line.""",
    "mustache",
)
