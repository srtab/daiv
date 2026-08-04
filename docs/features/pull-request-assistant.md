# Pull Request Assistant

The Pull Request Assistant helps you address code review feedback directly within merge requests (GitLab) or pull requests (GitHub). Mention DAIV in a comment and it will apply changes, answer questions, or fix failing pipelines.

!!! note
    On GitLab this works on **Merge Requests**, on GitHub on **Pull Requests**. This page uses "pull request" to refer to both.

## How to use

Mention DAIV in any comment on a pull request:

```
@daiv <your request>
```

Replace `daiv` with your DAIV bot's username if it differs.

DAIV reacts with an eyes emoji to confirm it received your request, then processes it and replies or pushes changes to the branch.

### Where to comment

You can mention DAIV in two places:

- **Inline on the diff** — comment directly on specific lines of code
- **General discussion** — comment in the pull request conversation

DAIV has context of the diff and the comment's position, so inline comments give it precise context about what you're referring to.

## What it can do

### Apply code changes

Ask DAIV to modify code based on your review feedback:

```
@daiv use Redis instead of in-memory storage
```

```
@daiv add error handling for the case when the API returns null
```

```
@daiv move this logic to a separate helper function
```

### Answer questions

Ask DAIV about the code to help you make review decisions:

```
@daiv why is this import inside the method instead of at the top?
```

```
@daiv is this approach thread-safe?
```

### Fix failing pipelines

When CI/CD fails, ask DAIV to investigate and fix it:

```
@daiv the pipeline is failing, can you fix it?
```

DAIV will inspect the pipeline logs, identify the root cause, and push a fix to the branch.

## Conversation continuity

DAIV maintains context across multiple interactions on the same pull request. You can have a back-and-forth conversation — each new mention builds on previous context, so DAIV understands the full history of changes and discussions.

!!! tip
    If DAIV starts drifting or gets stuck, you can use `@daiv /clear` to reset the conversation and start fresh.

## Configuration

The pull request assistant is enabled by default. To disable it, add the following to your `.daiv.yml`:

```yaml
pull_request_assistant:
  enabled: false
```

## Review reports

Each review is posted as a single comment on the pull request — findings grouped by
severity (Critical / Important / Suggestions), open questions for the author, and a short
list of recommended actions. On GitLab, reviews **stack**: a re-review posts a new report
covering only the commits since the previous one (after a force-push, the next report covers
the full change again and says so). If a review could not cover one of its dimensions — say
the security detector failed — the report says so, and the next review re-covers that span
rather than treating it as done. A review requested when nothing has changed since the last
one answers with a one-line "already reviewed" note instead of a fresh report.

On GitHub the report carries no tracking marker: re-review scope comes from the conversation
history on the pull request rather than from the reports themselves, so it is lost if that
conversation is cleared — after which the next report covers the whole pull request again.

You can reply on the merge/pull request and mention DAIV to ask about a finding or have it
apply a fix.

## Custom review rules

Define team-specific review rules in `.agents/review-rules.md` at the repository root. Write them in plain language, one rule per bullet, with any path scope expressed in prose:

- *Every external API call in `src/payments/**` must set an explicit timeout.*
- *Never log request or response bodies.*
- *New Celery tasks must be idempotent.*

The code review agent checks for rule sources at the start of every review and, when any exist, runs a dedicated `cr-custom-rules` detector against the diff; each violation it posts cites the rule it enforces.

You don't need a dedicated `.agents/review-rules.md`: rules already written in your repository's `AGENTS.md` and `.agents/AGENTS.md` are picked up as a **secondary** source — the agent mines them for concrete, diff-checkable conventions. When sources disagree, `.agents/review-rules.md` wins. The detector is skipped only when none of these files exist.

Every custom-rule finding passes the same confidence gate and skeptical aggregation as built-in findings, so a noisy `AGENTS.md` will not flood the review.

## Related pages

- [Slash Commands & Skills](slash-commands.md) — invoke `/code-review` from a merge request, and write custom skills to extend it
- [Sandbox](sandbox.md) — the isolated container where DAIV reproduces and repairs failing pipelines
- [Sessions](sessions.md) — follow a running review and revisit what the agent changed
- [Repository Config](../customization/repository-config.md) — tune review behaviour per repository with `.daiv.yml`
