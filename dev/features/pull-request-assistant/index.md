# Pull request assistant

The pull request assistant answers when you mention DAIV in a comment on a GitLab merge request or a GitHub pull request. DAIV applies review feedback, answers questions about the change, and fixes failing pipelines. The `/code-review` skill adds a full review: DAIV posts its findings as one [review report](#review-reports).

On GitLab this works on merge requests, on GitHub on pull requests. This page says "pull request" for both.

## Ask DAIV on a merge request

To ask DAIV for something on a pull request:

1. Open the pull request.

1. Add a comment that mentions DAIV:

   ```
   @daiv <your request>
   ```

   Replace `daiv` with your DAIV bot's username if it differs, and `<your request>` with what you want.

DAIV reacts to your comment with an eyes emoji to confirm it received the request, then replies or pushes changes to the branch.

### Where to comment

You can mention DAIV in two places:

- **Inline on the diff** — comment directly on specific lines of code. This reaches DAIV only on a merge request a DAIV session owns (see [A merge request DAIV published](#a-merge-request-daiv-published))
- **General discussion** — comment in the pull request conversation

DAIV sees the diff and the comment's position, so an inline comment tells it exactly what you mean.

### A merge request DAIV published

When a DAIV session published the merge or pull request, a mention goes to that session instead of starting a new one: the session that owns it (see [The session inbox](https://docs.daivagent.com/dev/features/sessions/#the-session-inbox)). It picks up where the session left off, and acts as you: as the DAIV account you signed in to with that GitLab/GitHub account, with its personal MCP servers, never as the session's owner. If you never signed in to DAIV with that account, the run gets no personal tools, even when a DAIV user shares your username. Its final message, or its question, is posted in the thread of your mention; when you mentioned DAIV in several threads before it answered, the full reply goes in the latest one, and each other thread gets a line linking to it.

A mention on a diff line reaches DAIV only on a merge request a session owns. On GitHub this also needs the App subscribed to **Pull request review comment** (see [Platform setup](https://docs.daivagent.com/dev/getting-started/platform-setup/#github-app-permissions-and-events)). Comments that don't mention DAIV aren't answered, but the owning session remembers them, and its next run sees them. It remembers them from anyone but DAIV itself, even from people left out of [`allowed_usernames`](https://docs.daivagent.com/dev/customization/repository-config/#access-control); only a mention needs its author on that list.

## What DAIV can do

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

DAIV inspects the pipeline logs, identifies the root cause, and pushes a fix to the branch.

### Answer a question from DAIV

When your request is ambiguous in a way that changes the work, or the choice is yours to make, DAIV can post a question instead of guessing. The question mentions you and ends with "reply mentioning `@daiv` with your answer." To answer, leave a new comment that mentions DAIV. On GitLab, DAIV posts its question inside the discussion thread of the comment that triggered it, so replying in that same thread keeps the conversation together; on GitHub the question is a top-level comment, unless you asked on a diff line of a merge request a session owns, where it is posted in that thread.

## Conversation continuity

DAIV keeps its context across the mentions on one pull request. Each new mention builds on the earlier ones, so DAIV knows the history of changes and discussion. If DAIV drifts or gets stuck, comment `@daiv /clear` to reset the conversation; see [Slash commands and skills](https://docs.daivagent.com/dev/features/slash-commands/#clear).

## Review reports

DAIV posts each review as a single comment on the pull request — findings grouped by severity (Critical / Important / Suggestions), open questions for the author, and, when there are Critical or Important findings, a short list of recommended actions. On GitLab, reviews **stack**: a re-review posts a new report covering only the commits since the previous one (after a force-push, the next report covers the full change again and says so). If a review could not cover one of its dimensions — say the security detector failed — the report says so, and the next review re-covers that span rather than treating it as done. A review requested when nothing has changed since the last one answers with a one-line "already reviewed" note instead of a fresh report.

On GitHub the report carries no tracking marker: re-review scope comes from the conversation history on the pull request rather than from the reports themselves, so it is lost if that conversation is cleared — after which the next report covers the whole pull request again.

To ask about a finding or have DAIV apply a fix, reply on the pull request and mention DAIV.

## Custom review rules

Define team-specific review rules in `.agents/review-rules.md` at the repository root. Write them in plain language, one rule per bullet, with any path scope expressed in prose:

- *Every external API call in `src/payments/**` must set an explicit timeout.*
- *Never log request or response bodies.*
- *New Celery tasks must be idempotent.*

The code review agent checks for rule sources at the start of every review and, when any exist, runs a dedicated `cr-custom-rules` detector against the diff; each violation it posts cites the rule it enforces.

`.agents/review-rules.md` is optional: rules already written in your repository's `AGENTS.md` and `.agents/AGENTS.md` are a **secondary** source, which the agent mines for concrete conventions it can check against the diff. When sources disagree, `.agents/review-rules.md` wins. The detector is skipped only when none of these files exist.

Every custom-rule finding passes the same confidence gate and skeptical aggregation as built-in findings, so a noisy `AGENTS.md` does not flood the review.

## Turn off the assistant

The pull request assistant is on by default. To turn it off for a repository, add this to its `.daiv.yml`:

```
pull_request_assistant:
  enabled: false
```

[Repository configuration](https://docs.daivagent.com/dev/customization/repository-config/index.md) lists every feature toggle.

## Related pages

- [Slash commands and skills](https://docs.daivagent.com/dev/features/slash-commands/index.md) — invoke `/code-review` from a merge request, and write custom skills to extend it
- [Sandbox](https://docs.daivagent.com/dev/features/sandbox/index.md) — the isolated container where DAIV reproduces and repairs failing pipelines
- [Sessions](https://docs.daivagent.com/dev/features/sessions/index.md) — follow a running review and revisit what the agent changed
- [Repository configuration](https://docs.daivagent.com/dev/customization/repository-config/index.md) — tune review behaviour per repository with `.daiv.yml`
