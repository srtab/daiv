ISSUE_PLANNING_TEMPLATE = """Hello {% if assignee %}@{{ assignee }} {% endif %}👋,

I'm **{{ bot_name }}**, your assistant for refactoring the codebase. Here's the process:

1. 🎯 **Planning:** I'll process this issue and create a detailed plan.
2. ✍️ **Review:** Once ready, I'll share the planned tasks for your review. Feel free to refine the title or description of the issue and i will revise the plan.
3. 🚀 **Execution:** After approval, I'll implement the shared plan and submit a merge request with the updates.

> ⚠️ ***Note:*** This may **take some time**. I'll leave you a message once the plan is ready.
"""  # noqa: E501

ISSUE_REVIEW_PLAN_TEMPLATE = """### 📋 ***Please take a moment to review the planned tasks:***

---

{% for plan_task in plan_tasks %}
<details><summary><b>Changes to apply {% if plan_task.file_path %}to <code>{{ plan_task.file_path }}</code>{% else %}to the repository{% endif %}</b></summary>

{{ plan_task.details }}
{% if plan_task.relevant_files %}
#### Relevant files:{% for file in plan_task.relevant_files %}
- `{{ file }}`{% endfor %}
{% endif %}

</details>

---

{% endfor %}

💡 **Next steps**

 - ✅ If the plan is good, leave a comment with `{{ approve_plan_command }}` to execute the plan.
 - ❌ If the plan doesn't meet your expectations, please **refine the issue description/title** and add more details or examples to help me understand the problem better. I will then revise the plan.
"""  # noqa: E501

ISSUE_QUESTIONS_TEMPLATE = """### 🔍 Additional Details Required

I need more information before I can create a clear implementation plan. Please help by answering the following questions directly within the issue's **title or description**:

{{ questions }}

---

💡 **Next steps**

1. **Edit the issue's title or description** and include your answers there.
2. Once you've updated the details, I'll automatically attempt to draft a new implementation plan.
"""  # noqa: E501


ISSUE_NO_CHANGES_NEEDED_TEMPLATE = """### ✅ ***No Changes Needed***

{{ no_changes_needed }}
"""  # noqa: E501


ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE = """### ⚠️ Error Executing the Plan

An error occurred while applying the implementation plan.

---

💡 **Next Steps:**

- 🔄 Comment **`{{ approve_plan_command }}`** to retry the plan execution.
- 📜 **Check the app logs** - open the {{ bot_name }} logs to see the full stack trace and [open an issue](https://github.com/srtab/daiv/issues/new) if the problem persists.
"""  # noqa: E501


ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE = """### ⚠️ Unexpected Error

Something went wrong while processing this issue.

---

💡 **Next Steps:**

- 🔄 Comment **`{{ revise_plan_command }}`** to trigger a fresh planning run.
- 📜 **Check the app logs** - open the {{ bot_name }} logs to see the full stack trace and [open an issue](https://github.com/srtab/daiv/issues/new) if the problem persists.
"""  # noqa: E501


ISSUE_PROCESSED_TEMPLATE = """### ✅ ***Process Completed***

{{ message }}

---

💡 **Next Steps:**

- **Review Changes:** Please review the changes in the merge request.
- **Follow Instructions:** Follow the instructions provided in the merge request description.
{% if show_merge_request_link %}
🔗 {{ source_repo_id }}!{{ merge_request_id }}+
{% endif %}
"""


ISSUE_MERGE_REQUEST_TEMPLATE = """{{ description }}

Closes: {{ source_repo_id }}#{{ issue_id }}{% if is_gitlab %}+{% endif %}

> ⚠️ {{ bot_name }} can make mistakes. Please review the changes and merge the MR if everything looks good.

### Summary of Changes
{% for item in summary %}
 - {{ item }}{% endfor %}

---

#### 💡 Instructions for the reviewer:
 - 💬 {{ bot_name }} will address comments for you in the following ways:
   - Open a discussion on the merge request overview and mention @{{ bot_username }};
   - Leave comments on the files and mention @{{ bot_username }};
   - Leave comments on specific lines of the file and mention @{{ bot_username }}.
 - 📝 Edit the original issue ({{ source_repo_id }}#{{ issue_id }}) to get {{ bot_name }} to recreate the MR from scratch.
"""  # noqa: E501
