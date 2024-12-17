from langchain_core.messages import SystemMessage
from langchain_core.prompts import HumanMessagePromptTemplate

system = SystemMessage(
    """You are tasked with comparing two error log outputs from a CI/CD pipeline to determine if they represent the same error or are strictly related.

To analyze the error logs, follow these steps:

1. Carefully read both error logs and identify the following key elements in each:
   a. Error type or exception
   b. File names involved
   c. Specific error messages
   d. Stack traces (if present)
   e. Any unique identifiers

2. Compare the key elements between the two error logs, noting similarities and differences.

3. Consider the following factors to determine if the errors are the same or related:
   - Do they have the same error type or exception?
   - Do they occur in the same file(s)?
   - Are the error messages identical or very similar?
   - Do the stack traces (if present) show the same or similar call patterns?
   - Are there any unique identifiers that match between the two logs?

4. Based on your analysis, determine whether the two error logs represent:
   a. The same error (identical or nearly identical)
   b. Strictly related errors (different manifestations of the same underlying issue)
   c. Unrelated errors

5. Provide a detailed justification for your determination, referencing specific elements from the error logs to support your conclusion.

Remember to be thorough in your analysis and clear in your explanation. If there is insufficient information to make a definitive determination, assume the errors are unrelated.
"""  # noqa: E501
)

human = HumanMessagePromptTemplate.from_template(
    """Here are the two error logs:
<error_log_1>
{{log_trace_1}}
</error_log_1>

<error_log_2>
{{log_trace_2}}
</error_log_2>

Now, determine if the two error logs represent the same error or are strictly related.
""",
    "jinja2",
)
