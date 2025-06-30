ISSUE_PLANNING_TEMPLATE = """Hello {% if assignee %}@{{ assignee }} {% endif %}👋,

I'm **{{ bot_name }}**, your assistant for refactoring the codebase. Here's the process:

1. 🎯 **Planning:** I'll process this issue and create a detailed plan.
2. ✍️ **Review:** Once ready, I'll share the planned tasks for your review. Feel free to refine the title or description of the issue and i will revise the plan.
3. 🚀 **Execution:** After approval, I'll implement the shared plan and submit a merge request with the updates.

> ⚠️ ***Note:*** This may **take some time**. I'll leave you a message once the plan is ready.
"""  # noqa: E501

ISSUE_REVIEW_PLAN_TEMPLATE = """### 📝 ***Please take a moment to review the planned tasks:***
{% for plan_task in plan_tasks %}
<details>
<summary>

**Changes to apply {% if plan_task.file_path %}to `{{ plan_task.file_path }}`{% else %}to the repository{% endif %}**

</summary>

{{ plan_task.details }}
{% if plan_task.relevant_files %}
#### Relevant files:{% for file in plan_task.relevant_files %}
- `{{ file }}`{% endfor %}
{% endif %}

</details>

---

{% endfor %}

💡 **Next Steps:**

 - ✅ If the plan is good, leave a comment with `@{{ bot_username }} plan execute` to execute the plan.
 - ❌ If the plan doesn't meet your expectations, please **refine the issue description/title** and add more details or examples to help me understand the problem better. I will then refine the plan.
"""  # noqa: E501

ISSUE_REVISE_TEMPLATE = """{% if not discussion_id %}### 🔄 ***Revising the Plan***

I'm creating a brand-new plan based on the updated details.{% else %}
I'm creating a brand-new plan based on the details.{% endif %}

> ⚠️ ***Note:*** This may **take a moment**. I'll notify you as soon as the new plan is ready.
"""  # noqa: E501

ISSUE_EXECUTE_PLAN_TEMPLATE = """I'll apply the plan straight away.

> ⚠️ ***Note:*** This may **take a moment**. I'll notify you as soon as the plan is executed.
"""  # noqa: E501

ISSUE_QUESTIONS_TEMPLATE = """{% if not discussion_id %}### ❓ ***Clarification needed***

{% endif %}I couldn't define a plan clearly based on the current details. To help me create a better plan, please clarify the following points:

{{ questions }}

---

💡 **Next Steps:**

- Update the issue's title or description with the requested clarifications.
- I'll automatically attempt to generate a new plan once the details are updated.
"""  # noqa: E501


ISSUE_UNABLE_DEFINE_PLAN_TEMPLATE = """{% if not discussion_id %}### ⚠ ***Unable to Define a Plan***

{% endif %}I was unable to define a plan for this issue. To help me assist you better, please make the following adjustments:

- **Refine Description:** Provide more details about the problem.
- **Add Examples:** Include specific examples or scenarios to clarify the issue.
- **Clarify Requirements:** Ensure all necessary requirements are clearly outlined.

----

💡 **Next Steps:**

- Update the issue's title or description.
- I'll automatically attempt to generate a new plan once the details are updated.
"""  # noqa: E501


ISSUE_UNABLE_EXECUTE_PLAN_TEMPLATE = """{% if not discussion_id %}### ⚠ ***Unable to Execute the Plan***

{% endif %}I was unable to execute the plan for this issue. To help me assist you better, please make the following adjustments:

- **Refine Description:** Provide more details about the problem.
- **Add Examples:** Include specific examples or scenarios to clarify the issue.
- **Clarify Requirements:** Ensure all necessary requirements are clearly outlined.

---

💡 **Next Steps:**

- Update the issue's title or description.
- I'll automatically attempt to generate a new plan once the details are updated.
"""  # noqa: E501


ISSUE_UNABLE_PROCESS_ISSUE_TEMPLATE = """{% if not discussion_id %}### ⚠ ***Unable to Process the Issue***

{% endif %}⚠️ An unexpected error occurred while processing the issue.

Please check the logs for more details.
"""  # noqa: E501


ISSUE_PROCESSED_TEMPLATE = """### ✅ ***Process Completed***

I have created a merge request with the requested changes.

💡 **Next Steps:**

- **Review Changes:** Please review the changes in the merge request.
- **Follow Instructions:** Follow the instructions provided in the merge request description.

🔗 {{ source_repo_id }}!{{ merge_request_id }}+
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
   - Open a discussion on the merge request overview and mention @{{ bot_username }};
   - Leave comments on the files and mention @{{ bot_username }};
   - Leave comments on specific lines of the file and mention @{{ bot_username }}.
 - 📝 Edit the original issue ({{ source_repo_id }}#{{ issue_id }}) to get {{ bot_name }} to recreate the MR from scratch.
"""  # noqa: E501
