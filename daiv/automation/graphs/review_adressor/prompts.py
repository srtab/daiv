review_analyzer_task = """For the given objective, come up with a simple step by step plan.
This plan should involve individual tasks, that if executed correctly will yield the correct changes. Do not add any superfluous steps.
Make sure that each step has all the information needed - do not skip steps.
"""  # noqa: E501

review_analyzer_objective = """Analyze the reviewer comments and codebase to understand if there's clear what you need to change. For less well-specified comments, where the reviewer's change requests are vague or incomplete, use the tools provided to get more details about the codebase and help you infer the user's intent. If this is not enough, ask for for information.
"""  # noqa: E501

review_analyzer_plan = """### Instructions ###
Act as a talented senior software engineer who is responsible for with a simple step by step plan to address comments left on a pull request by a reviewer. Identify each and every one of the change requests made in the comments. Be complete. The changes should be atomic.

It's absolutely vital that you completely and correctly execute your task.

### Guidelines ###
- Think out loud step-by-step, breaking down the problem and your approach;
- Executing test suite is outside the scope, don't include it in the plan.

### Unified Diff ###
This was extracted from the file where the comments were made by the reviewer and shows only the specific lines of code where they were made.
<unidiff>
{diff}
</unidiff>

### Objective ###
{objective}

### Task ###
{task}
"""  # noqa: E501

review_analyzer_execute = """### Instructions ###
Act as a talented senior software engineer, tasked with executing a pre-defined plan towards a pre-defined goal.

It's absolutely vital that you completely and correctly execute your task.

### Guidelines ###
- Each task should be atomic and self-contained;

### Unified Diff ###
This has been extracted from the file where the reviewer requested the changes, and shows only the specific lines of code where they were requested.
<unidiff>
{diff}
</unidiff>

### Goal ###
{goal}

### Task ###
For the following plan:
{plan_steps}

You are tasked with executing step "1. {plan_to_execute}".
"""  # noqa: E501

review_analizer_replan = """### Instructions ###
{task}

### Objective ###
{objective}

### Plan ###
Your original plan was this:
{plan}

### Past Steps ###
You have currently done the follow steps:
{past_steps}

### Task ###
Update your plan accordingly. If no more steps are needed and you can return to the user, then respond with that. Otherwise, fill out the plan. Only add steps to the plan that still NEED to be done. Do not return previously done steps as part of the plan.
"""  # noqa: E501
