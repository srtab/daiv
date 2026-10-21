from __future__ import annotations

from langchain_core.prompts import SystemMessagePromptTemplate

from automation.agent.middlewares.git_platform.github import GITHUB_TOOL_NAME
from automation.agent.middlewares.git_platform.gitlab import GITLAB_TOOL_NAME

GITLAB_TOOL_DESCRIPTION = """\
Use this tool to inspect the configured GitLab project through the python-gitlab CLI.

This tool is best for retrieving the current state of:
- issues
- merge requests
- pipelines
- jobs
- job traces
- other project-scoped GitLab resources

**Inputs:**
- `subcommand`: a single CLI-like subcommand string in the form `<object> <action> <arguments...>`
- `output_mode`: either `simplified` or `detailed`
- `output_to_file`: set true to write the FULL result to a file (as JSON; `output_mode` is ignored) and get back a compact confirmation instead of the content. Project-job trace is written as raw log text, not JSON.

**Hard rules:**
- Do NOT include the `gitlab` prefix in `subcommand`
- Do NOT pass `--project-id` (the project is injected automatically)
- Do NOT pass `--output` (set automatically from `output_mode`, or forced to json when output_to_file is true)
- Prefer IID-based lookup when available (for example `--iid` for issues and merge requests)

**Default behavior:**
- The configured project is targeted automatically
- Results are ordered from most recent to oldest by default
- List subcommands return the first 5 items by default unless you paginate with `--page`
- A result too large for the context window is automatically saved to a file (see below) instead of being returned inline

**Getting results into a file (for jq/scripts), and automatic saving:**
- Set `output_to_file=true` to write the FULL, untruncated result as JSON (using `--output json`; project-job trace is written as raw log text) instead of returning it inline. The file path is chosen automatically and returned in the confirmation. Then process it with `bash` (jq, scripts, grep) or read it with `read_file`. Use this when you intend to post-process the result.
- This avoids re-typing tool output into a bash command. Pagination stays under your control: pair with `--get-all` or `--per-page` for complete list dumps.
- Even without `output_to_file`, any result too large for the context window is automatically saved to the same large-tool-results directory and replaced inline with a short preview plus the file path — read it with `read_file`, or `grep` within that directory if you don't know the exact path.
- Example: `project-merge-request list --state opened --get-all` with `output_to_file=true`, then read the returned path with `bash`: `jq '[.[].labels[]] | group_by(.) | map({label: .[0], count: length})' <returned-path>`.

**How to use it well:**
- Use `output_mode="simplified"` ONLY for broad `list` discovery where you just need IDs (e.g. `project-merge-request list --state opened`).
- Use `output_mode="detailed"` for every `get --iid`/`--id` call and for any `list` subcommand where you need status/name/stage fields. `simplified` strips almost every field on `get` and is pure overhead when the target is already known.
- Do NOT call a resource in `simplified` mode and then re-call the same resource in `detailed` mode — the second call makes the first redundant. Pick the right mode up front.
- Prefer one targeted query over broad listings.
- If debugging CI, move in this order: pipeline -> failing jobs -> failing job trace.

**Useful subcommand patterns:**
- Issue by IID: `project-issue get --iid <issue_iid>`
- Issue note (comment): `project-issue-note create --issue-iid <issue_iid> --body "<text>"`
- Merge request by IID: `project-merge-request get --iid <merge_request_iid>`
- MR note (comment): `project-merge-request-note create --mr-iid <merge_request_iid> --body "<text>"`
- MR pipelines (only when you need the *history* of pipelines for an MR): `project-merge-request-pipeline list --mr-iid <merge_request_iid>`
  - For the *latest* pipeline status of an MR, do NOT call this — the detailed response of `project-merge-request get --iid <merge_request_iid>` already includes `head_pipeline.status` and `head_pipeline.id`.
- Pipeline jobs (failures only — preferred for CI triage): `project-pipeline-job list --pipeline-id <pipeline_id> --scope failed`
- Pipeline jobs (all): `project-pipeline-job list --pipeline-id <pipeline_id>` (use only when you need to see passing/skipped jobs too, e.g. to diagnose a *skipped* job)
- Job trace: `project-job trace --id <job_id>`
- Filtered issue list: `project-issue list --state opened --labels bug --page <page_number>`
- MR diff versions: `project-merge-request-diff list --mr-iid <merge_request_iid>`
- MR diff detail: `project-merge-request-diff get --mr-iid <merge_request_iid> --id <diff_id>`

**Invalid examples:**
- ✗ `gitlab project-issue get --iid 42`
- ✗ `project-issue get --iid 42 --project-id 123`
- ✗ `project-issue get --id 42`
- ✗ `project-issue list --output json`
- ✗ `project list --topic test` (unsupported: this tool is project-scoped)

**Special case: inline merge request comments**
- For a new inline comment on an MR diff, use `project-merge-request-discussion create`, not `project-merge-request-note create`.
- Inline comments require `--position`. Without `--position`, the discussion is created on the MR overview, not on a diff line.
- Before creating an inline comment, first inspect the latest MR diff/version to get the correct SHA triplet (`base_sha`, `start_sha`, `head_sha`).
- For text diff comments, `--position` must be a **JSON object string** containing: `position_type` ("text"), `base_sha`, `start_sha`, `head_sha`, `old_path`, `new_path`, and the correct line anchor:
  - use `new_line` (int) for an added line
  - use `old_line` (int) for a removed line
  - use both `old_line` and `new_line` for an unchanged line
  - Example: `--position '{"position_type": "text", "base_sha": "abc", "start_sha": "def", "head_sha": "ghi", "old_path": "src/foo.py", "new_path": "src/foo.py", "new_line": 42}'`
- To reply to an existing inline thread, use `project-merge-request-discussion-note create --discussion-id <discussion_id>`.
- Never guess an inline position. If the exact diff anchor cannot be determined reliably, prefer a regular MR note instead of posting a misplaced inline comment.

**Special case: review suggestions**
- When leaving an inline MR diff comment that proposes a precise, localized code change, prefer a suggestion block in the comment body instead of plain prose.
- A suggestion is created by putting GitLab suggestion Markdown inside the discussion body (for example, a single-line suggestion uses a `suggestion:-0+0` block).
- Suggestions only work in merge request diff threads. A plain merge request note is not the right place for an applyable suggestion.
- Prefer single-line suggestions by default. Use multi-line suggestions only when the replacement range is clear and tightly scoped.
- Use suggestions only for concrete code replacements. If the feedback is ambiguous, high-level, or not safely expressible as code, use a normal comment instead.

**Operational guidance:**
- Do NOT try to fetch URLs returned by this tool; they require authentication
- If a subcommand fails because you need flags, use targeted help: `<object> <action> --help`
- Always wrap arguments containing spaces or multiline text in double quotes. Do not escape internal quotes with backslashes — use single quotes inside double-quoted strings or rephrase. If the body is very long (>2000 chars), consider splitting into multiple notes.
- When the output is long, extract only the decisive facts needed for the next step"""  # noqa: E501

GITHUB_TOOL_DESCRIPTION = """\
Use this tool to inspect the configured GitHub repository through the GitHub CLI.

This tool is best for retrieving the current state of:
- issues
- pull requests
- checks
- workflow runs
- job logs
- other repository-scoped GitHub resources

**Inputs:**
- `subcommand`: a single CLI-like subcommand string in the form `<object> <action> [arguments...]>`
- `output_to_file`: set true to write the FULL stdout to a file (verbatim) and get back a compact confirmation instead of the content.

**Hard rules:**
- Do NOT include the `gh` prefix in `subcommand`
- Do NOT pass `--repo` or `-R` (the repository is injected automatically)
- Keep listings small and targeted

**Default behavior:**
- The configured repository is targeted automatically
- List subcommands are limited to the first 30 items by default
- Results are ordered from most recent to oldest when supported by the underlying subcommand
- A result too large for the context window is automatically saved to a file (see below) instead of being returned inline

**Getting results into a file (for jq/scripts), and automatic saving:**
- Set `output_to_file=true` to write the FULL, untruncated stdout to a file instead of returning it inline. The file path is chosen automatically and returned in the confirmation. Then process it with `bash` (jq, scripts, grep) or read it with `read_file`. Use this when you intend to post-process the result.
- gh has no global JSON flag — for jq-able output, include gh's own `--json <fields> [--jq ...]` in the subcommand; otherwise the file holds gh's text output.
- Pagination stays under your control: use `--limit` for complete list dumps.
- Even without `output_to_file`, any result too large for the context window is automatically saved to the same large-tool-results directory and replaced inline with a short preview plus the file path — read it with `read_file`, or `grep` within that directory if you don't know the exact path.
- Example: `pr list --state open --json number,title,labels --limit 200` with `output_to_file=true`, then read the returned path with `bash`: `jq '.[] | select(.labels | length == 0) | .number' <returned-path>`.

**How to use it well:**
- Start with the smallest subcommand that identifies the exact target
- Prefer direct detail/log commands once you know the relevant issue, PR, run, or job
- If debugging CI, move in this order: PR checks -> failing run/job -> logs
- Prefer one targeted query over broad listings

**Useful subcommand patterns:**
- Issue by number: `issue view <issue_number>`
- Pull request by number: `pr view <pr_number>`
- PR checks: `pr checks <pr_number>`
- Workflow runs: `run list --workflow <workflow_name>`
- Job logs: `run view <run_id> --job <job_id> --log`
- Filtered issue list: `issue list --state open --label bug --limit <n>`
- Create a release: `release create <tag> --title "<title>" --notes "<notes>"` (or `--generate-notes`)

**Invalid examples:**
- ✗ `gh issue view 42`
- ✗ `issue list --repo owner/repo`
- ✗ `issue list --limit 200`

**Operational guidance:**
- Do NOT try to fetch URLs returned by this tool; they require authentication
- Prefer direct log/detail subcommands over relying on summary output
- Always wrap arguments containing spaces or multiline text in double quotes
- When the output is long, extract only the decisive facts needed for the next step"""  # noqa: E501


GIT_PLATFORM_SYSTEM_PROMPT = SystemMessagePromptTemplate.from_template(
    f"""\
## Git Platform Tools

Use the available Git platform tool early whenever platform state can change what you should do.

Scope: All operations are scoped to the CURRENT project only. You cannot access files, pipelines, or metadata from other projects. If you need cross-project information, ask the user to provide it.

**Core policy:**
- If the user references an issue, PR/MR, pipeline, workflow, job, check, CI failure, review comment, or platform artifact, inspect it before editing code. An issue already in the latest `<issue>` block of the conversation counts as inspected; fetch it only for its comments or when its description was cut.
- Prefer platform facts over assumptions.
- Do not propose a fix for failing CI until you have inspected the most relevant failing logs/traces available.
- Use the smallest query that identifies the exact resource, then inspect that resource in detail.
- Do not dump raw platform output back to the user; extract only the facts that affect the next action.
- After inspecting platform state, continue with normal coding-agent behavior: inspect the codebase, make the smallest plausible fix, and verify.

**Default debug loop:**
1. Identify the exact target resource
2. Read the most relevant details
3. If CI is failing, inspect the latest failing logs/traces
4. Form a concrete hypothesis
5. Make the smallest likely fix
6. Re-check the relevant platform signal if needed

{{{{#gitlab_platform}}}}
### `{GITLAB_TOOL_NAME}` (GitLab)

Use this tool for GitLab issues, merge requests, pipelines, jobs, and traces.

**GitLab-specific guidance:**
- For issue work, use the issue in the latest `<issue>` block of the conversation as the task definition. Fetch it only when it is not there, for its comments, or when its description was cut.
- For merge request work, fetch the MR first; if CI is relevant, inspect its latest pipeline before changing code.
- For pipeline failures, do not edit code or CI config until you have read the failing job trace(s).

**Inline MR comment policy:**
- When the user asks for an inline MR comment, do not create a plain merge request note.
- First identify the exact target line in the current MR diff.
- Then inspect the latest MR diff/version to obtain the SHA triplet needed for anchoring (`base_sha`, `start_sha`, `head_sha`).
- Only then create the comment with `project-merge-request-discussion create --mr-iid <mr_iid> --body "<comment>" --position "<structured position payload>"`.
- If the user is replying to an existing inline thread, use `project-merge-request-discussion-note create --discussion-id <discussion_id>`.
- If the exact inline anchor cannot be resolved safely, fall back to a regular MR note and say the inline position could not be determined reliably.
- Prefer single-line inline comments. Multi-line diff comments are more brittle and should only be attempted when the required range metadata is clearly available.

**Suggestion policy:**
- When leaving an inline review comment and you can express the fix as a small, concrete code replacement, prefer an inline comment with a suggestion block so the author can apply it directly.
- Prefer suggestions for localized edits such as renames, condition fixes, missing guards, small refactors, formatting, or replacing one expression with another.
- Do not use a suggestion when the change is speculative, architectural, spans too much code, depends on broader context, or cannot be anchored precisely to the current diff.
- Prefer a single concise suggestion over a long explanatory comment when the code change itself communicates the fix clearly.
- If using an inline comment, first anchor the discussion correctly to the diff, then place the suggestion block in the comment body.
- Default to single-line suggestions; only use multi-line suggestions when the replacement range is obvious and tightly scoped.

<example>
user: Fix issue #42.
assistant:
  [If the conversation has no `<issue>` block for #42, call `{GITLAB_TOOL_NAME}("project-issue get --iid 42", output_mode="detailed")`]
assistant:
  [Extract the real problem from the issue]
  [Inspect the relevant code]
  [Make the smallest fix that addresses the issue]
</example>

<example>
user: Fix the failing pipeline for merge request #123.
assistant:
  [Call `{GITLAB_TOOL_NAME}("project-merge-request get --iid 123", output_mode="detailed")`]
  [Read `head_pipeline.status` and `head_pipeline.id` directly from the MR detail — no separate pipeline fetch needed for the latest pipeline]
  [Call `{GITLAB_TOOL_NAME}("project-pipeline-job list --pipeline-id <pipeline_id> --scope failed", output_mode="detailed")`]
  [For each failing job: Call `{GITLAB_TOOL_NAME}("project-job trace --id <job_id>", output_mode="detailed")`]
  [If no jobs come back with --scope failed (pipeline failed because a job was *skipped* or config-rejected), re-list without --scope to inspect job statuses/stages]
assistant:
  [Use the traces to form a root-cause hypothesis]
  [Then change code or CI config]
</example>

<example>
user: Leave an inline review comment on merge request #123 for src/foo.py line 87.
assistant:
  [Call `gitlab("project-merge-request-diff list --mr-iid 123", output_mode="detailed")`]
  [Select the latest diff/version and extract the SHA fields needed for anchoring]
  [If needed, inspect the diff details to confirm the file path and whether the target line is added, removed, or unchanged]
  [Construct the position as a JSON object with `position_type`, `base_sha`, `start_sha`, `head_sha`, `old_path`, `new_path`, and the correct line field(s)]
  [Call `gitlab("project-merge-request-discussion create --mr-iid 123 --body \\"<comment>\\" --position \\"<json position object>\\"", output_mode="detailed")`]
assistant:
  [Confirm the comment was created at the intended diff location]
</example>

<example>
user: Leave an inline review comment on merge request #123 suggesting a safer nil check.
assistant:
  [Identify the exact diff line and create a properly anchored inline MR discussion]
  [Write the comment body as a short explanation plus a GitLab suggestion block containing the replacement code]
assistant:
  [Prefer a single-line suggestion if the fix is localized and directly applicable]
</example>
{{{{/gitlab_platform}}}}

{{{{#github_platform}}}}
### `{GITHUB_TOOL_NAME}` (GitHub)

Use this tool for GitHub issues, pull requests, checks, workflow runs, and logs.

**GitHub-specific guidance:**
- For issue work, use the issue in the latest `<issue>` block of the conversation as the task definition. Fetch it only when it is not there, for its comments, or when its description was cut.
- For pull request work, fetch the PR first; if CI is relevant, inspect checks and the failing run/job before changing code.
- For workflow failures, do not edit code or workflow config until you have read the most relevant failing logs.
- Prefer direct log/detail subcommands over summaries when possible.

<example>
user: Fix issue #42.
assistant:
  [If the conversation has no `<issue>` block for #42, call `{GITHUB_TOOL_NAME}("issue view 42")`]
assistant:
  [Extract the real problem from the issue]
  [Inspect the relevant code]
  [Make the smallest fix that addresses the issue]
</example>

<example>
user: Investigate failing workflow/job/check for pull request #123.
assistant:
  [Call `{GITHUB_TOOL_NAME}("pr checks 123")`]
  [Identify the failing check and the relevant run/job identifiers from the returned metadata]
  [Call `{GITHUB_TOOL_NAME}("run view <run_id> --job <job_id> --log")`]
assistant:
  [Use the logs to form a root-cause hypothesis]
  [Then change code or workflow config]
</example>
{{{{/github_platform}}}}""",  # noqa: E501, S608
    "mustache",
)
