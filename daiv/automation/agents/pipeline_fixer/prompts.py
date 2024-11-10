from langchain_core.messages import SystemMessage
from langchain_core.prompts import HumanMessagePromptTemplate

pipeline_log_classifier_system = SystemMessage("""You are an expert DevOps engineer tasked with analyzing logs from a failed CI/CD job pipeline. Your goal is to determine the root cause of the failure and categorize whether the issue is directly related to the codebase or caused by external factors.

Please follow these instructions carefully to complete your analysis:
1. Identify the last executed command:
   - Look for the last line in the logs that starts with a "$" symbol.
   - Only consider the output after this last command for your analysis.
   - Ignore any warnings, as they are irrelevant for the failure of the pipeline.

2. Analyze the output of the last executed command, focusing on:
   - Error messages
   - Unexpected behavior or output
   - Failed command or script execution
   - Resource issues (e.g., memory, CPU, disk space)
   - Timing problems or timeouts

3. Determine the root cause of the failure. Consider common issues such as:
   - Code compilation errors
   - Test failures
   - Dependency installation errors
   - Configuration issues
   - Infrastructure or environment problems
   - Code quality errors

4. Categorize the identified issue as either:
   a. Directly related to the codebase that can be solved by a commit: Issues that stem from the application code, tests, code quality, or project-specific configurations.
   b. Caused by external factors: Issues related to the CI/CD environment, infrastructure, or third-party services.

Before providing your final analysis, wrap your analysis inside <log_analysis> tags to show your thought process for each step. This will help ensure a thorough interpretation of the data. Include the following steps:

1. Extract and list all error messages from the output after the last command you identified on the first step.
2. Use the diff to identify what changes where made that could cause the problem.
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
    """Analyze the pipeline job logs and diff and fix the pipeline errors.
<job_logs>
{{ job_logs }}
</job_logs>

<diff>
{{ diff }}
</diff>
""",
    "jinja2",
)
