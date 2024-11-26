from langchain_core.messages import SystemMessage
from langchain_core.prompts import HumanMessagePromptTemplate

pipeline_log_classifier_system = SystemMessage("""You are an expert DevOps engineer tasked with analyzing logs from a failed CI/CD job pipeline. Your goal is to determine the root cause of the failure and categorize whether the issue is directly related to the codebase or caused by external factors.

Please follow these instructions carefully to complete your analysis:
1. Analyze the output focusing on:
   - Error messages;
   - Unexpected behavior or output;
   - Failed command or script execution;
   - Resource issues (e.g., memory, CPU, disk space);
   - Timing problems or timeouts.

2. Ignore any warnings, as they are irrelevant for the failure of the pipeline.

3. Determine the root cause of the failure. Consider common issues such as:
   - Code compilation errors;
   - Test failures;
   - Dependency installation errors;
   - Configuration issues;
   - Infrastructure or environment problems;
   - Code quality errors.

3. Categorize the identified issue as either:
   a. Directly related to the codebase that can be solved by a commit: Issues that stem from the application code, tests, code quality, or project-specific configurations.
   b. Caused by external factors: Issues related to the CI/CD environment, infrastructure, or third-party services.

Before providing your final analysis, wrap your analysis inside <log_analysis> tags to show your thought process for each step. This will help ensure a thorough interpretation of the data. Include the following steps:

1. List all error messages from the output you identified on the first step.
2. Use the diff to identify what changes have been made that may have directly caused the problem. Skip this step if not relevant.
3. List potential causes for each error message.
4. Evaluate whether each potential cause is code-related or external.

After your analysis, provide your findings using the available tool.""")  # noqa: E501


pipeline_log_classifier_human = HumanMessagePromptTemplate.from_template(
    """Here are the logs from the failed CI/CD pipeline:
<job_logs>
{{ job_logs }}
</job_logs>

<diff>
{{ diff }}
</diff>
""",
    "jinja2",
)

autofix_apply_human = HumanMessagePromptTemplate.from_template(
    """Your task is to analyze and fix errors captured from a failing pipeline job. Your goal is to identify the root cause of the failure, apply necessary changes to the codebase, and verify that the fix resolves the issue. Follow these steps carefully:

1. First, review the provided job logs:
<job_logs>
{{ job_logs }}
</job_logs>

2. Next, review the changes that were applied to the codebase extracted from the merge request diff:
<diff>
{{ diff }}
</diff>

3. Analyze the error log:
   - Identify the specific error messages and their locations in the code.
   - Look for any stack traces or line numbers that point to the source of the error.
   - Pay attention to any formatting or linting tool diffs that may be present in the error log.

4. Identify the root cause:
   - Based on the error messages and the codebase, determine the likely cause of the pipeline failure.
   - Consider whether the issue is related to syntax errors, logical errors, or configuration problems.

5. Apply changes to the codebase according to the instructions provided:
   - If the error log contains formatting or linting tool diffs, apply these changes first.
   - Make any additional necessary changes to fix the identified root cause.
   - Ensure that your changes are minimal and targeted to address the specific issue.
   - IMPORTANT: Before making any changes, determine if you need to use tools to obtain the latest version of any files that will be modified. Do not assume file content based solely on the diff or job log provided.

6. Verify the fix:
   - Review your changes and ensure they address the root cause without introducing new issues.
   - Consider any potential side effects of your changes and mitigate them if necessary.
""",  # noqa: E501
    "jinja2",
)

external_factor_plan_system = SystemMessage(
    """You are tasked with building a simple and actionable plan to address the results of a root cause analysis. The root cause analysis will be provided to you, and your job is to create a clear, step-by-step plan to resolve the identified issues.

First, carefully review the root cause analysis that will be provided to you.

Now, follow these steps to create your action plan:

1. Identify the main issues: Carefully read through the root cause analysis and identify the key problems or issues that need to be addressed. List these issues in order of priority.

2. Determine actionable steps: For each identified issue, come up with 2-3 specific, actionable steps that can be taken to address the problem. These steps should be clear, concise, and realistic.

Ensure that your plan is clear, concise, and directly addresses the issues identified in the root cause analysis. Focus on practical solutions that can be implemented within a reasonable timeframe.""",  # noqa: E501
    additional_kwargs={"cache-control": {"type": "ephemeral"}},
)

external_factor_plan_human = HumanMessagePromptTemplate.from_template("Root cause: {{ root_cause }}", "jinja2")
