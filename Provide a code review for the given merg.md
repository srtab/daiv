Provide a code review for the given merge request.

**Agent assumptions (applies to all agents and subagents):**
- All tools are functional and will work without error. Do not test tools or make exploratory calls. Make sure this is clear to every subagent that is launched.
- Only call a tool if it is required to complete the task. Every tool call should have a clear purpose.

To do this, follow these steps precisely:

1. Launch a general purpose agent to check if any of the following are true:
   - The merge request is closed
   - The merge request is a draft
   - The merge request does not need code review (e.g. automated PR, trivial change that is obviously correct)

   If any condition is true, stop and do not proceed.

Note: Still review DAIV generated MR's.

2. Launch a general purpose agent to view the merge request and return a summary of the changes

3. Launch 3 agents in parallel to independently review the changes. Each agent should return the list of issues, where each issue includes a description and the reason it was flagged (e.g. "AGENTS.md adherence", "bug"). The agents should do the following:

   Agent 1: AGENTS.md compliance agent
   Audit changes for AGENTS.md compliance.

   Agent 2: General purpose bug agent (parallel subagent with agent 3)
   Scan for obvious bugs. Focus only on the diff itself without reading extra context. Flag only significant bugs; ignore nitpicks and likely false positives. Do not flag issues that you cannot validate without looking at context outside of the git diff.

   Agent 3: General purpose bug agent (parallel subagent with agent 2)
   Look for problems that exist in the introduced code. This could be security issues, incorrect logic, etc. Only look for issues that fall within the changed code.

   **CRITICAL: We only want HIGH SIGNAL issues.** Flag issues where:
   - The code will fail to compile or parse (syntax errors, type errors, missing imports, unresolved references)
   - The code will definitely produce wrong results regardless of inputs (clear logic errors)
   - Clear, unambiguous AGENTS.md violations where you can quote the exact rule being broken

   Do NOT flag:
   - Code style or quality concerns
   - Potential issues that depend on specific inputs or state
   - Subjective suggestions or improvements

   If you are not certain an issue is real, do not flag it. False positives erode trust and waste reviewer time.

   In addition to the above, each subagent should be told the PR ID, title and description. This will help provide context regarding the author's intent.

4. For each issue found in the previous step by agents 2 and 3, launch parallel subagents to validate the issue. These subagents should get the PR ID, title and description along with a description of the issue. The agent's job is to review the issue to validate that the stated issue is truly an issue with high confidence. For example, if an issue such as "variable is not defined" was flagged, the subagent's job would be to validate that is actually true in the code. Another example would be AGENTS.md issues. The agent should validate that the AGENTS.md rule that was violated is scoped for this file and is actually violated. Use subagents for bugs, logic issues, and AGENTS.md violations.

5. Filter out any issues that were not validated in step 4. This step will give us our list of high signal issues for our review.

6. If issues were found, skip to step 7 to reply.

   If NO issues were found, reply with the following message:
   "No issues found. Checked for bugs and AGENTS.md compliance."

7. Create a list of all comments that you plan on leaving. This is only for you to make sure you are comfortable with the comments. Do not post this list anywhere.

8. Post inline comments for each issue using `gitlab` tool with `project-merge-request-draft-note` providing a `position` argument. For each comment:
   - Provide a brief description of the issue
   - For small, self-contained fixes, include a committable suggestion block
   - For larger fixes (6+ lines, structural changes, or changes spanning multiple locations), describe the issue and suggested fix without a suggestion block
   - Never post a committable suggestion UNLESS committing the suggestion fixes the issue entirely. If follow up steps are required, do not leave a committable suggestion.

   **IMPORTANT: Only post ONE comment per unique issue. Do not post duplicate comments.**

Use this list when evaluating issues in Steps 4 and 5 (these are false positives, do NOT flag):

- Pre-existing issues
- Something that appears to be a bug but is actually correct
- Pedantic nitpicks that a senior engineer would not flag
- Issues that a linter will catch (do not run the linter to verify)
- General code quality concerns (e.g., lack of test coverage, general security issues) unless explicitly required in AGENTS.md
- Issues mentioned in AGENTS.md but explicitly silenced in the code (e.g., via a lint ignore comment)

Notes:

- Use `gitlab` tool to interact with GitLab (e.g., fetch merge requests, create inline comments). Do not use web fetch.
- Create a todo list before starting.
- You must cite and link each issue in inline comments (e.g., if referring to a AGENTS.md, include a link to it).
- When linking to code in inline comments, follow the following format precisely, otherwise the Markdown preview won't render correctly: http://gitlab:8929/anthropics/claude-code/-/blob/c21d3c10bc8e898b7ac1a2d745bdc9bc4e423afe/package.json#L10-L15
  - Requires full git sha
  - You must provide the full sha. Commands like `http://gitlab:8929/owner/repo/-/blob/$(git rev-parse HEAD)/foo/bar` will not work, since your comment will be directly rendered in Markdown.
  - Repo name must match the repo you're code reviewing
  - # sign after the file name
  - Line range format is L[start]-L[end]
  - Provide at least 1 line of context before and after, centered on the line you are commenting about (eg. if you are commenting about lines 5-6, you should link to `L4-7`)
