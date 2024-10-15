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
