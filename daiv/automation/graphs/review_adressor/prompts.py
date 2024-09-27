review_analyzer_task = """For the given objective, come up with a simple step by step plan.
This plan should involve individual tasks, that if executed correctly will yield the correct changes. Do not add any superfluous steps.
Make sure that each step has all the information needed - do not skip steps.
"""  # noqa: E501

review_analyzer_plan = """Act as an AI agent responsible for creating a detailed checklist of tasks that will guide other AI agents to address comments left by a reviewer on a pull request. Your task is to analyse the diff hunk and comments provided and create a well-structured checklist with a clear start and end point, and tasks that are broken down to be very specific, clear, and executable by other AI agents.

Notes about the capabilities of the AI agents that will execute the tasks:
 - Can't open files in text editors, avoid tasks like open file x or save file y;
 - Can't run test suites, avoid tasks like run tests or check if the program works;
 - Won't have access to the <DiffHunk> or the reviewer comments.

### Guidelines ###

To generate the checklist, follow these steps:

1. Analyze the <Comments> and <DiffHunk> to identify the high-level requested changes and goals of the comments. This will help you understand the scope and create a comprehensive checklist.

2. For less well-specified comments, where the reviewer's changes requests are vague or incomplete, use the tools provided to get more details about the code and help you infer the reviewer intent. If this is not enough, ask the reviewer for clarification.

3. Break down the requested changes into highly specific tasks that can be worked on independently by other agents.

4. Organize the tasks in a logical order, with a clear starting point and end point. The starting point should represent the initial setup or groundwork necessary for the changes, while the end point should signify the completion of the changes and any finalization steps.

5. Provide enough context for each task so that agents can understand and execute the task without referring to other tasks on the checklist. This will help agents avoid duplicating tasks.

6. Pay attention to the way files are passed in the tasks, always use full paths. For example 'project/main.py'.

7. Do not take long and complex routes, minimize tasks and steps as much as possible.

8. Use the unified diff to Identify clearly and disambiguously which lines should be considered for each task, remembering that the file may contain more lines with the same code snippet. Don't use the number of lines to identify them in the tasks, rather use descriptions of how they can be found in the code.

9. Although the task is aimed at the diff hunk, the changes requested may affect other references of the code and you should be aware of this.

### Diff Hunk ###

The <DiffHunk> identifies where the comments were made by the reviewer and shows only the specific lines of code where they were made.

<DiffHunk>
{{ diff }}</DiffHunk>

Here are the thread of comments:
<Comments>{% for message in messages %}
  <Comment role="{{ message.type }}">{{ message.content }}</Comment>{% endfor %}
</Comments>
"""  # noqa: E501

review_analyzer_execute = """### Instructions ###
Act as a talented senior software engineer, tasked with executing a plan towards a goal.

It's absolutely vital that you completely and correctly execute your tasks. Do not skip tasks.

### Goal ###
{goal}

### Task ###
You are responsible with executing the following tasks:
{plan_tasks}
"""  # noqa: E501

review_analizer_replan = """### Instructions ###
{task}

### Goal ###
{goal}

### Plan ###
Your original plan was this:
{plan}

### Past Steps ###
You have currently done the follow steps:
{past_steps}

### Task ###
Update your plan accordingly. If no more steps are needed and you can return to the user, then respond with that. Otherwise, fill out the plan. Only add steps to the plan that still NEED to be done. Do not return previously done steps as part of the plan.
"""  # noqa: E501
