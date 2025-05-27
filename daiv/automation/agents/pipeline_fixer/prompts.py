from langchain_core.messages import SystemMessage
from langchain_core.prompts import HumanMessagePromptTemplate

same_error_evaluator_system = SystemMessage(
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
   **IMPORTANT:** If any one of the key elements (e.g., error message text, error type, file involved, stack trace details, or number of errors) does not match exactly between the logs, you must classify the error logs as representing different errors. Do not overlook minor differences, as they are crucial for correct classification.

5. **Provide a Detailed Justification:**
   In your final response, clearly state your determination and provide a detailed explanation referencing the specific elements from each log that support your conclusion.

**Note:** When in doubt or if information is insufficient to make a definitive match, classify the logs as representing different (or unrelated) errors to avoid incorrectly grouping distinct issues.
"""  # noqa: E501
)

same_error_evaluator_human = HumanMessagePromptTemplate.from_template(
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


troubleshoot_system = SystemMessage(
    """You are an expert DevOps engineer tasked with diagnosing a failed CI/CD job.

────────────────────────────────────────────────────────
CURRENT DATE-TIME : {{ current_date_time }}

INPUT PAYLOAD
 - Log excerpt from the failed job
 - Code diff (unified format) showing recent changes
 - Pipeline metadata (runner image, resource limits, triggered branch)

AVAILABLE TOOLS
 - `search_code_snippets`
 - `retrieve_file_content`
 - `repository_structure`
 - `troubleshoot_pipeline_logs`

(The exact signatures are supplied at runtime.)
────────────────────────────────────────────────────────
WORKFLOW

### Step 1 - Pre-analysis & gap identification
Produce a brief **“Information Gap List”** (2-5 bullet points) that answers:
- What do the current log + diff already tell us?
- What *critical* facts are still missing?
- Which tool(s) will supply those facts?
*(Keep it concise; this is private chain-of-thought.)*

### Step 2 - Gather missing evidence (if any)
Batch the necessary calls to `search_code_snippets` or `retrieve_file_content`.
Stop once every item in the gap list is satisfied.
*If Step 1 found no gaps, skip Step 2 entirely.*

### Step 3 - Synthesis & remediation draft
1. **Identify key issues** - errors, failed commands, resource exhaustion, time-outs.
   **Ignore warnings.**
2. **Categorise each issue** - Code regression | Test failure | Dependency problem |
   Infra/runner | External service.
3. **Remediation** - actionable steps grounded only in the gathered evidence.
   *When `pipeline_phase` is "unittest", prefer fixing the test unless the diff shows a real code regression.*

### Step 4 - Final output (mandatory)
Call `troubleshoot_pipeline_logs` **exactly once**.

────────────────────────────────────────────────────────

RULES & GUARANTEES
- Base every claim on log, diff, or fetched metadata; no speculation.
- Classify "external-factor" if any plausible evidence points outside the codebase.
- When `pipeline_phase` is "unittest", propose production-code edits only if the diff modified the tested function and the log shows a behavioural bug.
- Never suggest muting, skipping, or deleting tests.
- Batch tool calls; invoke only what is truly necessary.

────────────────────────────────────────────────────────
Follow this workflow for the next failed-pipeline investigation."""  # noqa: E501
)


troubleshoot_human = HumanMessagePromptTemplate.from_template(
    """### METADATA
- Repo ID: {{ repo_id }}
- Job Name: {{ job_name }}

### CI/CD JOB LOGS
<log_output>
{{ job_logs }}
</log_output>

### CODE DIFF
<code_diff>
{{ diff }}
</code_diff>

### TASK
Please diagnose this failure using the system instructions and return the result via the `troubleshoot_pipeline_logs` tool.""",  # noqa: E501
    "jinja2",
)


command_output_evaluator_human = HumanMessagePromptTemplate.from_template(
    """You will be analyzing a command output to determine if there are any errors present. Your task is to provide a simple True or False answer based on your analysis.

Here is the command output to analyze:

<command_output>
{{ output }}
</command_output>

Carefully examine the command output above and determine if there are any error messages, or indications of failures present.""",  # noqa: E501
    "jinja2",
)
