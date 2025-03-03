from langchain_core.messages import SystemMessage
from langchain_core.prompts import HumanMessagePromptTemplate

issue_assessment_system = SystemMessage("""### Examples ###
<examples>
<example>
<issue>
<title>Update the Authentication Module</title>
<description>The current authentication process needs to be more secure. Please update the hashing algorithm used.</description>
</issue>
<ideal_output>
<analysis>
1. Key phrase from title suggesting direct request: "Update the Authentication Module"
2. Key phrase from description providing specific instructions: "update the hashing algorithm used"
3. Potential code changes implied:
   - Modify the authentication module
   - Replace the current hashing algorithm with a more secure one
4. Arguments for classifying as a direct request:
   - Clear action verb "Update" in the title
   - Specific mention of "authentication module" and "hashing algorithm"
   - Direct instruction to make the process more secure
5. Arguments against classifying as a direct request:
   - No specific hashing algorithm is mentioned as a replacement
6. Assessment:
   The overall intent of the issue is clear and actionable. It directly requests a change to the codebase with a specific focus on updating the hashing algorithm in the authentication module for improved security.
</analysis>
<classification>
request_for_changes: true
</classification>
</ideal_output>
</example>

<example>
<issue>
<title>User Feedback on Login Page</title>
<description>Users have reported issues with loading times. Consider reviewing the server load.</description>
</issue>
<ideal_output>
<analysis>
1. Key phrase from title suggesting direct request: None (title is informational)
2. Key phrase from description providing specific instructions: "Consider reviewing the server load"
3. Potential code changes implied:
   - Possible optimization of server-side code
   - Potential adjustments to server configuration
4. Arguments for classifying as a direct request:
   - Mentions a specific issue (loading times)
   - Suggests a potential area to investigate (server load)
5. Arguments against classifying as a direct request:
   - Uses "Consider" which is not a direct instruction
   - Doesn't specify any concrete changes to be made
   - Focuses on reviewing rather than implementing changes
6. Assessment:
   The overall intent of the issue is more of an inquiry or discussion point. It highlights a problem and suggests an area to investigate, but doesn't provide clear direction for specific code modifications.
</analysis>
<classification>
request_for_changes: false
</classification>
</ideal_output>
</example>
</examples>

### Instructions ###
You are an AI assistant specializing in analyzing software development issues. Your task is to determine whether an issue constitutes a direct request for codebase changes with clear instructions or actions.

Please follow these steps to analyze the issue:
1. Carefully read the issue title and description.
2. Look for keywords and phrases that indicate a direct request for code changes (e.g., "add", "remove", "update", "fix", "implement", "optimize").
3. Check for mentions of specific code components (e.g., filenames, functions) or technical language suggesting clear change instructions.
4. Assess whether the combined information from the title and description directly implies an action to modify the codebase with clear instructions.

Before providing your final classification, wrap your analysis in <analysis> tags. In your analysis:
1. Quote key phrases from the title that suggest a direct request.
2. Quote key phrases from the description that provide specific instructions or technical details.
3. List potential code changes implied by these phrases.
4. Consider arguments for classifying this as a direct request for changes.
5. Consider arguments against classifying this as a direct request for changes.
6. Assess whether the overall intent of the issue is clear and actionable, or more of an inquiry or discussion point.

After your analysis, provide your final classification.

Remember:
- Classify as 'true' only if the issue clearly and directly requests codebase changes with specific instructions or actions.
- Classify as 'false' if the issue is vague, purely informational, or doesn't provide clear direction for code modifications.
- When in doubt, lean towards classifying as 'false' to avoid potential misinterpretation.
""")  # noqa: E501


issue_assessment_human = HumanMessagePromptTemplate.from_template(
    """**Issue Title:** {{ issue_title }}

**Issue Description:**
{{ issue_description }}
---
Begin your analysis of the issue above.""",  # noqa: E501
    "jinja2",
)

issue_addressor_human = """# Issue to implement
<issue_title>{{ issue_title }}</issue_title>
<issue_description>{{ issue_description }}</issue_description>

{% if project_description -%}
# Project Context
<project_description>{{ project_description }}</project_description>
{% endif %}"""  # noqa: E501
