ISSUE_PLANNING_TEMPLATE = """Hello @{assignee} 👋,

I'm **{bot_name}**, your assistant for refactoring the codebase. Here's the process:

1. 📝 **Planning:** I'll process this issue and create a detailed plan.
2. 🔍 **Review:** Once ready, I'll share the plan for your review. Feel free to ask questions or suggest changes.
3. 🚀 **Execution:** After approval, I'll implement the plan and submit a merge request with the updates.

⚠️ *Note:* This may take some time. I'll notify you once the plan is ready.

Thank you for your patience! 😊
"""  # noqa: E501

ISSUE_REVIEW_PLAN_TEMPLATE = """🔍 ***Please take a moment to examine the plan***

- **Modify Tasks:** You can add, delete, or adjust tasks as needed. Customized tasks will be considered when executing the plan.
- **Plan Adjustments:** If the plan doesn't meet your expectations, please refine the issue description and add more details or examples to help me understand the problem better. I will then replan the tasks and delete the existing ones.

✅ ***Approval is Required***

If everything looks good, please **reply directly to this comment** with your approval, and I'll proceed.

---

Thank you! 😊
"""  # noqa: E501


ISSUE_UNABLE_DEIFNE_PLAN_TEMPLATE = """⚠️ **Unable to Define a Plan**

I encountered an issue while creating a plan for this task. To help me assist you better, please make the following adjustments:

- **Refine Description:** Provide more details about the problem.
- **Add Examples:** Include specific examples or scenarios to clarify the issue.
- **Clarify Requirements:** Ensure all necessary requirements are clearly outlined.

🔄 **Next Steps:**

Once you've updated the issue, I'll attempt to create a plan again. If you need assistance, feel free to reach out!

---

Thank you for your cooperation! 😊
"""  # noqa: E501


ISSUE_PROCESSED_TEMPLATE = """✅ **Process Completed**

This issue has been successfully processed.

I have created a merge request with the requested changes: {source_repo_id}!{merge_request_id}.

🔍 **Next Steps:**

- **Review Changes:** Please review the changes in the merge request.
- **Follow Instructions:** Follow the instructions provided in the merge request description.

---

Thank you! 😊
"""


ISSUE_MERGE_REQUEST_TEMPLATE = """### Description
{{ description }}

Closes: {{ source_repo_id }}#{{ issue_id }}

### Summary of Changes
{% for item in summary %}
 * {{ item }}{% endfor %}

 ---

#### 💡 Instructions for the reviewer:
 - Comment on the files or specific lines of the file, and {{ bot_name }} will address it for you.
 - Edit the original issue ({{ source_repo_id }}#{{ issue_id }}) to get {{ bot_name }} to recreate the MR from scratch.
"""  # noqa: E501
