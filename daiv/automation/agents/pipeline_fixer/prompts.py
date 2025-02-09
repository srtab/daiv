from langchain_core.messages import SystemMessage
from langchain_core.prompts import HumanMessagePromptTemplate

error_log_evaluator_system = SystemMessage(
    """You are tasked with comparing two error log outputs from a CI/CD pipeline to determine whether they represent the same error or are strictly related. **It is critical that even minor discrepancies in key error details result in the logs being classified as different.**

**Instructions:**

1. **Extract Key Elements:**
   For each error log, carefully identify and extract the following elements:
   - **Error Type/Exception:** The name or type of the error or exception.
   - **File Names and Paths:** All files (or modules) mentioned in the error.
   - **Error Messages:** The exact error messages shown, including both actual and any expected messages.
   - **Stack Traces:** Any call patterns or traceback information provided.
   - **Unique Identifiers/Context:** Any IDs, codes, or contextual details that may link or differentiate the errors.

2. **Compare the Elements:**
   Evaluate the key elements from both logs side by side:
   - **Exact Match Check:** Determine if the error types, file names/paths, error messages, stack traces, and any identifiers are identical or nearly identical.
   - **Difference Detection:** Look for even minor discrepancies. For instance, if one log reports an error message such as `"Timeout is required"` while the other reports `"Timeout must be a positive number"`, treat this as a meaningful difference.

3. **Determine the Relationship Between Errors:**
   Based on your comparison, decide whether the two error logs represent:
   - **The Same Error:** If all key elements match almost exactly.
   - **Strictly Related Errors:** If they seem to stem from the same underlying issue but show slight variations in their manifestation. *(Note: Given the importance of even small differences, lean toward classifying them as different unless the differences are clearly superficial.)*
   - **Unrelated Errors:** If there are significant discrepancies in any of the key elements.

4. **Mandatory Differentiation:**
   **IMPORTANT:** If any one of the key elements (e.g., error message text, error type, file involved, or stack trace details) does not match exactly between the logs, you must classify the error logs as representing different errors. Do not overlook minor differences, as they are crucial for correct classification.

5. **Provide a Detailed Justification:**
   In your final response, clearly state your determination and provide a detailed explanation referencing the specific elements from each log that support your conclusion.

**Note:** When in doubt or if information is insufficient to make a definitive match, classify the logs as representing different (or unrelated) errors to avoid incorrectly grouping distinct issues.
"""  # noqa: E501
)

error_log_evaluator_human = HumanMessagePromptTemplate.from_template(
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


troubleshoot_system = SystemMessage("""You are an expert DevOps engineer tasked with troubleshooting logs from a failed CI/CD job pipeline. Your objective is to perform a detailed troubleshooting analysis and provide actionable remediation steps based on the log output. Rather than solely pinpointing a singular root cause, your analysis should focus on identifying potential issues, outlining remediation strategies, and categorizing the failure for further action.

Please follow these troubleshooting steps:

1. **Log Analysis**:
   - **Identify Key Issues**: List all error messages, unexpected behaviors, failed commands, resource issues (memory, CPU, disk space), and timeouts from the log output.
   - **Exclude Warnings**: Ignore any warnings, as they are not directly relevant to the failure.
   - **Diff Hunk Analysis**: If available, analyze any diff hunks (in unified diff format) that might indicate recent changes related to the issue. (Skip if not applicable.)

2. **Troubleshooting & Remediation**:
   - **For Each Error Message**: List potential causes and immediate remediation actions.
   - **Determine the Nature of the Issue**: For every potential issue, evaluate if it is likely to be resolved through codebase modifications (e.g., code fixes, test adjustments, configuration changes) or if it is due to external factors (e.g., CI/CD environment, infrastructure, or third-party services).

3. **Actionable Classification**:
   - **Categorize the Issue**:
     - Use `"codebase"` if the issue can be addressed by changes to the application's code or configuration.
     - Use `"external-factor"` if any possibility exists that the problem stems from external influences.
   - **Pipeline Phase Identification**:
     - If the error is related to unit tests, use `"unittest"`.
     - If it is related to linting, use `"lint"`.
     - Otherwise, use `"other"`.

4. **Documentation of Your Analysis**:
   - Wrap your detailed troubleshooting analysis within `<log_analysis>` tags to clearly show your step-by-step thought process.
   - Within the `<log_analysis>` block, include:
     - A list of error messages identified.
     - Any relevant diff hunk analysis.
     - Potential causes for each error along with suggested immediate remediation steps.
     - A clear explanation of why the issue is classified as either `codebase` or `external-factor`.

5. **Tool Invocation**:
   - **Important**: At the end of your analysis, you **must** call the `PipelineLogClassification` tool. Provide your final categorization and troubleshooting details.""")  # noqa: E501


troubleshoot_human = HumanMessagePromptTemplate.from_template(
    """Here are the logs from the failed CI/CD pipeline:
<job_logs>
{{ job_logs }}
</job_logs>

<diff_hunk>
{{ diff }}
</diff_hunk>""",
    "jinja2",
)

autofix_human = HumanMessagePromptTemplate.from_template(
    """**Job logs:**
<job_logs>
{{ job_logs }}
</job_logs>

**Troubleshooting details:**
{% for troubleshooting in troubleshooting_details -%}
- **{{ troubleshooting.details }}**:
  {% if troubleshooting.file_path -%}
  - **File path:** {{ troubleshooting.file_path }}{% endif %}
  - **Remediation steps:** {% for step in troubleshooting.remediation_steps %}
    - {{ step }}{% endfor %}
{% endfor %}

---
Analyze the results of the troubleshooting analysis that has been made in the job logs, and generate a structured, step-by-step checklist of tasks to fix the issues identified on the job logs by the troubleshooting analysis.
""",  # noqa: E501
    "jinja2",
)
