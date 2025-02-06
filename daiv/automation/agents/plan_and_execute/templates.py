ISSUE_PLANNING_TEMPLATE = """Hello {% if assignee %}@{{ assignee }} {% endif %}👋,

I'm **{{ bot_name }}**, your assistant for refactoring the codebase. Here's the process:

1. 🎯 **Planning:** I'll process this issue and create a detailed plan.
2. ✍️ **Review:** Once ready, I'll share the planned tasks for your review. Feel free to refine the title or description of the issue and i will replan the tasks.
3. 🚀 **Execution:** After approval, I'll implement the shared plan and submit a merge request with the updates.

> ⚠️ ***Note:*** This may **take some time**. I'll leave you a message once the plan is ready.
"""  # noqa: E501

ISSUE_REVIEW_PLAN_TEMPLATE = """### 📝 ***Please take a moment to review the planned tasks:***
{% for plan_task in plan_tasks %}
<details>
<summary>

{{ plan_task.title }} - `{{ plan_task.path }}`

</summary>

**{{ plan_task.context }}.**
{% for subtask in plan_task.subtasks %}
- [ ] {{ subtask }}{% endfor %}

---
</details>
{% endfor %}

💡 **Next Steps:**

 - ❌ If the plan doesn't meet your expectations, please **refine the issue description/title** and add more details or examples to help me understand the problem better. I will then replan the tasks.
 - ✅ If the plan is good, please approve the plan by **replying directly to this discussion** and I'll execute the plan.
"""  # noqa: E501

ISSUE_REPLAN_TEMPLATE = """### 🔄 ***Replanning***

I'm replanning the tasks with the new issue details.

> ⚠️ ***Note:*** This may **take some time**. I'll leave you a message once the plan is ready.
"""  # noqa: E501

ISSUE_QUESTIONS_TEMPLATE = """### ❓ ***Unable to define a plan***

I was unable to define a plan for this issue. To help me assist you better, please make adjustments to the issue description to clarify the following questions:

{% for question in questions %}
1. {{ question }}{% endfor %}

💡 **Next Steps:**

 - Update the issue description/title and I'll attempt to create a plan again.
"""  # noqa: E501


ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE = """### ⚠ ***Unable to Define a Plan***

I was unable to define a plan for this issue. To help me assist you better, please make the following adjustments:

- **Refine Description:** Provide more details about the problem.
- **Add Examples:** Include specific examples or scenarios to clarify the issue.
- **Clarify Requirements:** Ensure all necessary requirements are clearly outlined.

💡 **Next Steps:**

 - Update the issue description/title and I'll attempt to create a plan again.
"""  # noqa: E501


ISSUE_PROCESSED_TEMPLATE = """### ✅ ***Process Completed***

I have created a merge request with the requested changes.

💡 **Next Steps:**

- **Review Changes:** Please review the changes in the merge request.
- **Follow Instructions:** Follow the instructions provided in the merge request description.

🔗 {source_repo_id}!{merge_request_id}+
"""


ISSUE_MERGE_REQUEST_TEMPLATE = """### Description
{{ description }}

Closes: {{ source_repo_id }}#{{ issue_id }}+

> ⚠️ {{ bot_name }} can make mistakes. Please review the changes and merge the MR if everything looks good.

### Summary of Changes
{% for item in summary %}
 - {{ item }}{% endfor %}

---

#### 💡 Instructions for the reviewer:
 - 💬 {{ bot_name }} will address comments for you in the following ways:
   - Open a discussion on the merge request overview;
   - Leave comments on the files;
   - Leave comments on specific lines of the file.
 - 📝 Edit the original issue ({{ source_repo_id }}#{{ issue_id }}) to get {{ bot_name }} to recreate the MR from scratch.
"""  # noqa: E501
