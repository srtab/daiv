from __future__ import annotations

from textwrap import dedent
from typing import Literal

from pydantic import BaseModel, Field


class ChangeInstructions(BaseModel):
    """
    One coherent ChangeInstruction.

    Coherent = a single, per-file unit of work a developer would naturally perform in one editing pass (e.g. 'implement EchoQuickAction in echo.py'). Never describe edits across multiple files in this object.
    """  # noqa: E501

    relevant_files: list[str] = Field(
        description=dedent(
            """\
            Files the developer should open or be aware of when performing THIS change.

            MANDATORY:
            - MUST include `file_path` itself when it is not empty.

            WHAT TO INCLUDE:
            - A **small set of high-signal context files** that help the developer follow existing patterns. Think:
              - 1-2 sibling or similar implementation files.
              - 1-2 related test files.
              - Occasionally, a key config or doc file if it is directly relevant (e.g. the specific docs page you want updated, or the Makefile that defines a command you're asking them to run).

            WHAT TO AVOID:
            - Do NOT list dozens of files or whole directories; aim for **3-7 files total** per change.
            - Do NOT include broad, generic modules unless they are truly needed (e.g. avoid dumping most of the `codebase/` tree).
            - Do NOT use wildcards, globs, or pseudo-paths, only concrete file paths.
            """  # noqa: E501
        )
    )
    file_path: str = Field(
        description=dedent(
            """\
            Primary file to be modified by this change, relative to repo root.

            Rules:
            - If this change edits a specific file, set this to that file path (e.g. 'daiv/quick_actions/echo.py').
            - If this change is repo-wide or command-only (e.g. 'run make test', 'run npm run test', 'run uv sync'), set this to the empty string ''.
            - Do NOT describe changes to multiple files under a single `file_path`; instead, create separate ChangeInstructions.
            """  # noqa: E501
        )
    )
    details: str = Field(
        description=dedent(
            """\
            Task Card describing this coherent change in a human-friendly, scannable way.

            FORMAT (MUST FOLLOW THIS SHAPE)
            Write `details` as markdown with exactly these sections in this order:

            **Goal**
            Short, one-three sentences explaining what this change achieves from a user/feature perspective. Do NOT repeat the file path here.

            **Steps**
            A short, ordered list of concrete actions a developer must perform in this file (or for this command). Use 3-8 bullets, each starting with an imperative verb.
            - Example bullets:
              - 'Create a new EchoQuickAction class in this module and register it with the existing quick_action decorator for issues and merge requests.'
              - 'Add a description string that explains it echoes back the provided text.'
              - 'In the issue handler, post a new comment whose body is the trimmed command arguments, falling back to a simple placeholder if empty.'

            **Definition of Done**
            A brief checklist of observable outcomes that confirm the change is implemented correctly (2–5 bullets).
            - Example bullets:
              - 'Using `@<bot> /echo Hello` on an issue creates a new comment with the text `Hello`.'
              - 'Using `/echo` on a merge request produces a comment with the provided text and Markdown rendered.'

            CODE & SNIPPETS (STRICT RULES)
            - Prefer plain language; only add a snippet if it clarifies something that is hard to express in words.
            - Snippets MUST be:
              - Short (ideally <= 5-8 lines).
              - Partial (showing only a fragment, not a whole class/file/test module).
            - MUST NOT:
              - Include full implementations of files, classes, or test modules.
              - Show an entire new file from top to bottom.
              - Use phrases like 'A complete implementation could look like:' followed by a full code block.
            """  # noqa: E501
        )
    )


class FinishOutput(BaseModel):
    """
    Deliver a concise summary of changes made to the repository.

    **Usage rules:**
    - Report only actual modifications made to the codebase.
    - Focus on observable changes: files, commands, installations.
    """

    aborting: bool = Field(default=False, description="Indicates if execution was aborted before completing")
    message: str = Field(
        description=dedent(
            """\
            Concise summary of changes made to the repository.

            **Include:**
            - Files modified, created, or deleted with brief description of changes
            - Commands executed (dependencies installed, scripts run)
            - Changes that failed and why (file not found, permission denied, command failed)
            - If aborted, state what prevented completion

            **Avoid:**
            - Internal workflow steps (plans, reviews, verification, analysis)
            - Meta-commentary about the process
            - Terms like "change plan", "code review", "requirements satisfied"

            Use markdown for `files`, `directories`, `variables`, `commands`. Be factual and specific.
            """  # noqa: E501
        )
    )


class PlanOutput(BaseModel):
    """\
    Produce a self-contained implementation plan as an ordered list of steps.

    USAGE
    - Use when requirements are clear and changes are needed.
    - Calling this tool ends your work for the request: do NOT call any other tools afterwards.

    CORE RULES
    - Express the plan as a checklist in execution order; `changes[i]` is step i.
    - Each `changes` item is ONE coherent change in ONE file, or ONE repo-wide/command step.
    - Never mix multiple files in a single item.
    - For file edits, set `file_path` to that file (e.g. 'daiv/quick_actions/actions/echo.py').
    - For repo-wide or command-only steps (e.g. `make test`, `make lint`, `uv sync`), set `file_path` to \"\" and describe a single command.
    - `details` MUST be written in the Task Card format: **Goal**, **Steps**, **Definition of Done**.
    - Prefer natural language instructions over code. Use very small snippets only when absolutely necessary.
    - Never include a full implementation of a file, class, or test; describe what to implement instead.
    - The plan must be self-contained: no external URLs or reliance on prior conversation; reference actual files and symbols.
    """  # noqa: E501

    type: Literal["plan"] = Field(default="plan", description="Type discriminator for plan output. Always 'plan'.")
    changes: list[ChangeInstructions] = Field(
        description=dedent(
            """\
            Ordered list of ChangeInstructions in execution order.

            Each element is one coherent change in a single file (or one repo-wide/command step). If your mental plan has steps like 'implement feature in file A, add tests in file B, update docs in file C', that should be at least three `changes` items.
            """  # noqa: E501
        ),
        min_length=1,
    )


class ClarifyOutput(BaseModel):
    """
    Ask targeted questions to clarify the user's requirements.

    Calling this tool ends your work for the current request: do NOT call any other tools after `ClarifyOutput`.

    **Usage rules:**
    - Use when requirements are ambiguous or incomplete.
    - Ask only what's necessary to understand what changes to make.
    """

    type: Literal["clarify"] = Field(default="clarify", description="Type discriminator for clarify output")
    questions: str = Field(
        description=dedent(
            """\
            Targeted questions to clarify requirements.

            **Include:**
            - Reference specific files, functions, or code when relevant
            - Ask about concrete choices (which approach, which file, what behavior)
            - Present options when multiple valid interpretations exist

            **Avoid:**
            - Generic or vague questions
            - Asking about things already visible in the codebase
            - Mentioning internal processes (search, analysis, planning)

            Be direct and concise. Use markdown for `files`, `variables`, `functions`.
            """  # noqa: E501
        )
    )


class CompleteOutput(BaseModel):
    """
    Deliver a message confirming the repository already satisfies the requirements.

    Calling this tool ends your work for the current request: do NOT call any other tools after `CompleteOutput`.

    **Usage rules:**
    - Use when the current state already meets what was requested.
    - Provide concrete evidence from the codebase.
    """

    type: Literal["complete"] = Field(default="complete", description="Type discriminator for complete output")
    message: str = Field(
        description=dedent(
            """\
            Explain why no changes are needed with specific evidence from the repository.

            **Include:**
            - State that the requirement is already satisfied
            - Cite specific evidence: files, code excerpts, configurations, dependencies
            - Reference exact locations (file paths, line numbers, function names)

            **Avoid:**
            - Mentioning investigation or analysis processes
            - Vague statements without concrete evidence
            - Internal workflow references

            Use markdown for `files`, `code`, `variables`. Keep code excerpts brief.
            """  # noqa: E501
        )
    )


# Discriminated union of all possible finalizer outputs
FinalizerOutput = PlanOutput | ClarifyOutput | CompleteOutput
