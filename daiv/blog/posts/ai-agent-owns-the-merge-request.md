---
title: "When an AI agent owns the merge request: inbox, CI watch and mentions"
date: 2027-02-09
summary: "After DAIV opens a merge request, its session owns it: CI results and mentions go to one inbox that decides what wakes the agent, with hard caps."
author: Sandro Rodrigues
image: session.png
---

An agent that opens a merge request (a pull request, on GitHub) and stops there leaves the follow-up to you: the pipeline fails, a reviewer asks for a change, someone pushes to the branch. Here is what happened instead on a demo repository, from its GitLab history, in UTC:

```text
23:23:32  the agent opens merge request !2
23:23:50  pipeline fails: job test
23:25:55  the agent pushes 5b35c335 "chore(store): add __future__.annotations import"
23:26:10  pipeline passes
```

No person commented or pushed in between. What follows is how that works: the session that published the merge request owns it, an inbox decides which events wake the agent, caps limit how often it wakes itself, and CI results are judged conservatively, with the cases that still go wrong.

DAIV is an open-source coding agent for GitLab and GitHub that runs on your servers, with [the model you choose](https://docs.daivagent.com/dev/getting-started/llm-providers/), from a hosted provider to your own vLLM server. A session is its record of one piece of work: the runs, the conversation and the merge requests they produced.

The failure woke the session that had opened the merge request. From the CI trace, the agent found that CI runs Python 3.12.12, while the Python it had tested with, 3.14.2, evaluates annotations lazily. It added `from __future__ import annotations` to the file:

![A DAIV session titled "Add a low-stock report", running. The agent quotes the CI trace, TypeError: 'function' object is not subscriptable, at app/store.py:12 in def list_low_stock(self, threshold: int = 10) -> list[Item], and explains that the class's own list method shadows the builtin there. It finds that the sandbox runs Python 3.14.2, where annotations are lazy, while CI runs 3.12.12, adds from __future__ import annotations to store.py, and runs the tests on Python 3.12. The sidebar shows merge request !2 on acme/inventory-api-5 with "CI: Waiting for CI".](session.png)

That is one run on a demo repository, not a success rate. Had the fix failed as well, the agent could have tried up to twice more, since a merge request gets 3 fix attempts by default. A failure after the last attempt notifies the merge request's owner instead.

## One owner per merge request

When a run publishes, its session claims the merge request and records the commit DAIV pushed. These runs claim it: addressing an issue, a job from the API, MCP or a schedule, and a chat turn ([which runs claim a merge request](https://docs.daivagent.com/dev/features/pipeline-watch/#which-runs-claim-a-merge-request)).

- A later publish from another session of the same person moves the merge request to that session.
- A publish from another person's session leaves it where it is and tells the owning session.
- When DAIV answers review comments on a merge request that no session owns, it claims nothing. Watching that pipeline would put commits nobody asked for on someone else's branch. A reviewer who wants CI fixed can ask in a comment.

## Everything goes through one inbox

Everything that asks a session to act is an event in its inbox:

- a CI result on a merge request it published;
- a mention or comment on that merge request;
- a chat message sent while the session was busy;
- a follow-up job;
- a delegated batch finishing;
- a coordinating session asking a session it delegated to, to continue its work.

Each event either wakes the agent or is only remembered ([the session inbox](https://docs.daivagent.com/dev/features/sessions/#the-session-inbox)). A person's request wakes it: a chat message, a follow-up job, or a mention, unless the session is archived, when DAIV replies asking for it to be unarchived. A CI pass, a pipeline waiting for a person, someone else's push, a merge or close, and a comment that does not mention DAIV are remembered. They start no run, and the next run on that branch gets them as context, so the agent knows the branch moved before it pushes.

![A diagram: seven kinds of event reach the session (a CI failure on DAIV's commit, a CI pass or a pipeline needing a person, a mention, a comment, push or merge, a chat message while busy, a follow-up job, a finished delegated batch) and go into the session's inbox. The inbox either wakes the agent, starting a run that is one at a time per session, one per person and branch, and acts as the requester, with 3 attempts by default for CI failures; or remembers the event, so no run starts and the next run on the branch gets it as context.](inbox-flow.svg)

## One run at a time, per person

A session runs one thing at a time. DAIV claims the session's single run slot in the same database transaction that makes the run ready ([inbox design](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/sessions/inbox/__init__.py#L1-L50)). A waking event that arrives while the session is busy joins a queued next run, so it has a run to show while it waits.

How events become runs:

- **One run per person and branch.** A chat message and a CI failure that arrived during the same run are answered together, in the next run.
- **People don't share runs.** Requests from different people wait for runs of their own, so one person's request never runs with another's access.
- **A run acts as the person who woke it.** A teammate's mention runs as the DAIV account linked to their GitLab or GitHub account, with the external tools (MCP servers) they connected. A commenter who never linked an account gets no personal tools. A run that DAIV woke for a CI failure acts as the session's owner.
- **The newest wins.** When several pending events concern the same thing, such as two CI results for one merge request (on GitHub, for the same workflow), the run takes the newest and marks the older ones superseded, so they are never acted on.
- **Built-in slash commands run alone**, because DAIV answers a command only when it is a run's whole request.

When a run fails, its events are tried again, three runs in all, then marked failed. A coordinating session's request to continue is the exception: it runs alone and is never retried. When the owner stops a run, its events are dismissed instead ([complete.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/sessions/inbox/complete.py#L1-L12)).

## Judging CI conservatively

The CI watch reads each settled pipeline and posts the result to the inbox. Only the merge request's head pipeline counts; a pipeline that an older push started is ignored. Of all the statuses CI can report, only two are a verdict:

```python
# The only two statuses that are a verdict. Everything else either waits on a human or has
# not got there yet (created, pending, running, preparing, …), and both read as UNCLEAR.
VERDICT_STATUSES = frozenset({"success", "failed"})
NEEDS_A_HUMAN_STATUSES = frozenset({"blocked", "manual", "canceled", "skipped"})
```

([judgment.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/sessions/pipeline_watch/judgment.py#L12-L15))

The rows below are read top to bottom, and the first match wins:

| Pipeline | Outcome |
|---|---|
| Still running, or not found | Not judged yet, nothing posted |
| `skipped` with no jobs | Not judged: GitLab records that for a push sent with `ci.skip`, the option that asks it not to start a pipeline |
| `blocked`, `manual`, `canceled` or `skipped` | Remembered as needing a person |
| No jobs at all | Remembered as needing a person |
| `failed`, with a failed job the project does not allow to fail | Failed: may wake the session |
| `failed`, with no failed job visible (a configuration error, say) | Failed: may wake the session |
| `failed`, but every failed job is allowed to fail (GitLab) | Passed |
| `success`, with at least one job | Passed |

The zero-job row comes from GitLab's CI includes. When DAIV pushes with a project-scoped token on GitLab, the pipeline runs as that token's bot user. If the project's CI configuration includes a template from another private project, the bot cannot read it, and GitLab records a pipeline with no jobs ([conservative judgment](https://docs.daivagent.com/dev/features/pipeline-watch/#conservative-judgment)).

Treating that as green would let a broken pipeline pass, and treating it as a failure would spend every attempt on a pipeline that was never going to run. So DAIV records the result as needing a person and starts no run. The watch stays on, and the next pipeline on the branch is judged as usual. [The post on DAIV's GitLab tokens](/blog/gitlab-token-out-of-agent-sandbox/) covers how DAIV pushes around it.

## When a failure wakes the agent

A failure wakes the owning session only when all of these hold, and the first one that doesn't becomes the reason the agent sees next time ([how it works](https://docs.daivagent.com/dev/features/pipeline-watch/#how-it-works)):

| Condition | Reason recorded when it fails |
|---|---|
| The watch is on for the repository and this merge request | Watch off |
| The pipeline ran on the commit DAIV last pushed | Not DAIV's commit |
| No CI run has looked at that commit yet | Already examined |
| Attempts are left: 3 per merge request by default | Attempts spent |
| DAIV has not started 3 runs in a row on the session without a person | Too many runs in a row |

The CI run gets the failed jobs' names and the pipeline's link, and reads the job logs itself. A run that only CI woke cannot ask a question, because nobody is there to answer. It either pushes a fix or explains why it pushed nothing; a run that pushes nothing does not give up, but no push means no new pipeline, so the session shows CI as failing with a link to the explanation.

When a failure on a new DAIV commit finds no attempts left, the merge request's owner is notified once for that commit. The attempts come back when a person starts a run that publishes to the merge request, or turns **Fix CI failures** back on. After three runs in a row that DAIV started on its own, CI failures and finished delegated batches stop waking the session, and the owner is told; a person's request picks it up again ([policy.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/sessions/inbox/policy.py#L29)).

An administrator sets the site-wide switch and attempt cap, and a repository's `.daiv.yml` can only tighten them:

```yaml
pipeline_watch:
  enabled: true
  max_attempts: 2
```

## Missed webhooks

CI results arrive as webhooks: `pipeline_events` on GitLab, `workflow_run` on GitHub. Any of those webhooks can be missed. So a task runs every 10 minutes ([tasks.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/sessions/tasks.py#L115-L126)) and re-reads CI for each watched merge request that nothing was heard of for 30 minutes ([reconciler.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/sessions/pipeline_watch/reconciler.py#L1-L31)).

Each run of that task reads at most 200 branches, the longest-quiet first, and a branch it could not read in full is tried again on the next run. A pipeline already judged in the same status is skipped before its jobs are read.

Nothing expires: a watch lasts until its merge request is merged or closed.

## What it does not do

- **Flaky tests look like real failures.** Every failure on DAIV's commit may wake the session. The attempt cap bounds the cost, and a run that finds nothing to fix says so.
- **GitHub reports per workflow.** A push with several workflows sends one event each, judged on its own. The first workflow to pass can show CI as passing while another is still running.
- **GitHub does not say which jobs are required**, so DAIV treats every failed job as required when it lists the failures for the agent, and never reads a failed run as green because its failures were tolerated.
- **GitLab child pipelines** are ignored in favour of their parent. A child's failure counts only when its trigger job uses `strategy: mirror` or `strategy: depend`, so the parent fails with it.
- **GitLab merge-request-only pipelines** are not on the branch ref, so the 10-minute re-read finds nothing for them, and those repositories rely on the webhook alone.
- **Only merge requests DAIV published** are watched, and CI on the default branch is ignored.

## Trying it

The watch is on by default, but it needs CI events. On GitLab, `docker compose exec app django-admin setup_webhooks --update` adds them to the webhooks DAIV registered earlier, and new webhooks have them already. On GitHub, give DAIV's App the **Actions** read-only permission and subscribe it to **Workflow run**. Each installation's owner must then accept the new permission, and the watch stays off on an installation until they do.

After that, DAIV watches every merge request a session owns. The session's repository card shows **Waiting for CI**, then **Passing**, **Failing** or **Needs a person**, and the **Fix CI failures** switch in its **⋯** menu turns the watch off for that merge request.

The [pipeline watch page](https://docs.daivagent.com/dev/features/pipeline-watch/) covers these steps in full, and the site and repository limits. [The session inbox](https://docs.daivagent.com/dev/features/sessions/#the-session-inbox) covers how requests become runs.
