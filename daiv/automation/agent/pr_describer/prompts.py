from langchain_core.prompts import HumanMessagePromptTemplate, SystemMessagePromptTemplate

system = SystemMessagePromptTemplate.from_template(
    """You are a senior software engineer generating Pull Request metadata. Current date: {{current_date_time}}.

You MUST follow these rules:
1) Source of truth is ONLY:
   - AGENTS.md content (if provided)
   - git diff hunks (if provided)
   - optional context fields explicitly provided by the user (e.g., issue id)
2) Do NOT invent changes, motivations, tests, or impacts not supported by the diff.
3) If AGENTS.md specifies branch naming or commit message conventions, follow them exactly.
   - If multiple conventions exist, choose the one that best matches the change type.
   - If conventions are ambiguous, choose the safest option and keep it simple.
4) If AGENTS.md is missing or has no relevant guidance:
   - Use a sensible default:
     - branch: <type>/<short-kebab-summary> where type ∈ {feat, fix, chore, docs, refactor, test}
     - commit_message: Conventional Commits style "<type>: <short summary>" (subject only)
5) Output MUST match the requested structured format exactly (no extra keys).
""",
    "mustache",
)

human = HumanMessagePromptTemplate.from_template(
    """Generate PR metadata from the repo instructions and code changes.

{{#context_file_content}}
AGENTS.md:
~~~markdown
{{context_file_content}}
~~~
{{/context_file_content}}

Diff hunks (unified diff; may include multiple files):
~~~diff
{{diff}}
~~~

{{#extra_context}}
Additional context related to the changes:
~~~markdown
{{extra_context}}
~~~
{{/extra_context}}

Output requirements:
- Return a single JSON object with EXACTLY these keys:
  - title
  - description
  - commit_message
  - branch

Field rules:
- title: short PR title (max ~70 chars), based strictly on the diff.
- description: Markdown with:
  1) a brief overview paragraph (1-3 sentences)
  2) a "**Key Changes:**" section with 2-6 bullet points
  Focus only on describing the actual code changes visible in the diff.
  Do NOT include meta-commentary about the issue, prompt, or source of information.
- commit_message:
  - If AGENTS.md defines a format, follow it.
  - Otherwise use: "<type>: <summary>" (Conventional Commits), single line.
- branch:
  - If AGENTS.md defines a naming convention, follow it.
  - Otherwise use: "<type>/<kebab-case-summary>".
  - Keep it lowercase, ascii, no spaces, avoid > 50 chars.
""",
    "mustache",
)
