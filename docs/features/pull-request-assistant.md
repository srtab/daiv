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
