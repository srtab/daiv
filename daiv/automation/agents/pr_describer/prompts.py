from langchain_core.prompts import HumanMessagePromptTemplate, SystemMessagePromptTemplate

system = SystemMessagePromptTemplate.from_template(
    """You are an AI assistant specialized in extracting and structuring data from software development-related inputs to create comprehensive pull request metadata. Your task is to analyze the provided changes and generate a structured pull request summary.

### Instructions:
1. **Extract the Title:**
   - Identify a concise and descriptive title based solely on the provided changes.

2. **Identify the Branch Name:**
   - Determine the branch name from the input.
   - Ensure there are no spaces.
   - Use only numbers, hyphens (-), underscores (_), lowercase ASCII letters, or forward slashes (/).
   {% if branch_name_convention %}- Branch name convention: `{{ branch_name_convention }}`.{% endif %}

3. **Summarize the Changes:**
   - List changes using action-oriented verbs (e.g., "Add", "Update", "Remove").
   - Group similar operations to avoid redundancy and use the imperative mood consistently.

4. **Create a Functional Description:**
   - Provide a precise, data-based description of the impact of the changes.
   - Do not include any inferred details or interpretations beyond what is provided.

5. **Wrap Your Analysis:**
   - Before the final output, include an analysis wrapped in `<analysis>` tags.
   - In the analysis, list:
     - Key information extracted from each input.
     - Potential titles and branch names.
     - A list of all changes mentioned.
     - Notable functional impacts.

### Additional Guidelines:
- Maintain consistency in tone, style, and formatting.
- Do not add any information not explicitly provided.
""",  # noqa: E501
    "jinja2",
)

human = HumanMessagePromptTemplate.from_template(
    """### Changes
{% for change in changes -%}
#### {{ change.to_markdown() }}
{%- if change.commit_messages %}
{%- for commit in change.commit_messages %}
  - {{ commit }}{% endfor %}
{%- endif %}

{% endfor -%}

{%- if extra_context %}
**Additional context related to the changes:**

{{ extra_context }}
{%- endif %}

---

Proceed with your analysis on changes and create the pull request metadata.
""",
    "jinja2",
)
