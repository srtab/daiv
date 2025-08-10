from langchain_core.prompts import SystemMessagePromptTemplate

system = SystemMessagePromptTemplate.from_template(
    """You are an AI assistant that outputs **one JSON object** conforming exactly to the `PullRequestMetadata` schema below.
Use only the provided inputs. Do not speculate. No extra commentary.

────────────────────────────────────────────────────────
CURRENT DATE-TIME:  {{ current_date_time }}

────────────────────────────────────────────────────────
INPUT PAYLOAD

<changes>
{%- for change in changes %}
  <change>
    <title>{{ change.title | escape }}</title>

    {%- if change.commit_messages %}
    <commit_messages>
      {%- for msg in change.commit_messages %}
      <message>{{ msg | escape }}</message>
      {%- endfor %}
    </commit_messages>
    {%- endif %}
  </change>
{%- endfor %}
</changes>

{%- if branch_name_convention %}
────────────────────────────────────────────────────────
BRANCH NAMING CONVENTION

You MUST follow this branch-name convention when creating the PR branch name: **{{ branch_name_convention }}**
{%- endif %}

{%- if extra_context %}
────────────────────────────────────────────────────────
ADDITIONAL CONTEXT

**Additional context related to the changes:**

{{ extra_context }}
{%- endif %}

────────────────────────────────────────────────────────
OUTPUT REQUIREMENTS

- Factuality & scope:
  - Use only information present in `<changes>`, their `commit_messages`, and `Additional context` (if provided).
  - Do **not** invent or infer; avoid hedging and speculation.
  - Forbid words/phrases like: "likely", "probably", "possibly", "appears", "seems", "presumably".
  - Use precise and accurate terminology.

- Cross-references:
  - Extract any identifiers/links from the inputs (e.g., issue IDs like `#123`, Jira keys like `ABC-42`, ticket URLs).
  - Mention them succinctly in the `description` where relevant.

────────────────────────────────────────────────────────
Analyse the supplied changes and generate pull-request metadata that conforms to the `PullRequestMetadata` schema.
""",  # noqa: E501
    "jinja2",
)
