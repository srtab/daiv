---
name: code-review
description: This skill should be used when a user asks for a code review, feedback on a PR or MR, diff assessment, or says things like 'can you review my changes', 'look at this diff', 'is this ready to merge', 'check my code', 'review this branch', 'what do you think of these changes', or 'LGTM check'. Covers correctness, performance, security, structural concerns, repo-specific review rules, and questions of intent on pull/merge requests or raw diffs from any platform (GitHub, GitLab).
metadata:
  version: 3.6.0
---

# Code Review

This skill runs in two phases. The **review workflow** finds and verifies high-signal issues; **delivery** optionally publishes them to GitLab. Each phase has its own reference — read it as you enter the phase, so its detail is fresh when you act on it. This file is the router: it picks the mode, lists the stages, and states the rules that hold in every mode. The detailed procedure lives in the references, not here.

## Pick a mode

- **Delivery mode** — the runtime has merge-request context (`Scope.MERGE_REQUEST` with a `merge_request_id`) **and** the platform is GitLab. The review is delivered as inline discussions on specific lines plus one top-level summary discussion, posted via the `gitlab` tool. Delivery requires the `gitlab` tool: if it's not loaded, `tool_search` for it; if it can't be loaded (or returns 403 on the discussion endpoint), demote to interactive mode. The pick is provisional: `gitlab-delivery.md` Step 1 re-confirms that `merge_request_id`, the project, and the SHA triplet are all present and demotes to interactive if any is missing.
- **Interactive mode** — anything else: a local diff, a referenced MR/PR with no runtime context, a GitHub PR, or ambiguous scope. The review is returned as a markdown final message; the harness handles delivery.

## Run the review

1. **Always read `references/review-workflow.md` first; do not pre-judge the diff.** It walks scope → Stage 0 (per-repo review rules) → Stage 1 (detector fan-out, or an inline triage pass for trivially small changes) → Stage 2 (merge + adversarial verification) → severity, and hands off the **verified findings**.
2. **Interactive mode:** render the survivors using the interactive output protocol at the end of `review-workflow.md`, and return it as the final message. Done.
3. **Delivery mode:** once the workflow hands off verified findings, **read `references/gitlab-delivery.md` and follow it** to post them. Read it *before* posting anything — the marker, anchor, and dedup machinery is not reconstructable from memory.

## Non-negotiables (every mode)

- **Every run enters `references/review-workflow.md` — no verdict before it.** "This diff is trivial/obvious" is not an exit: judging triviality is the workflow's triage gate, and it is not yours to run from the router.
- **Delivery mode always completes `references/gitlab-delivery.md`, even at zero findings.** "No findings" is not "nothing to deliver" — prior notes, pending replies, and the existing summary still get reconciled there; what (if anything) gets posted is that reference's decision, not yours.
- **Precision over recall.** Adversarially refute every finding; over-pruning is acceptable. Present only confirmed survivors — no strikethrough, no "on closer reading this is fine."
- **Never post style, formatting, whitespace, or import-ordering findings.** That's a linter's or formatter's job.
- **Detectors run as `cr-*` subagents, never `general-purpose`.** A `general-purpose` dispatch returns prose with no `findings` array and breaks the merge. If a `cr-*` type didn't load, skip it and report the gap in the status line — never substitute.
- **Never compute markers or anchors by hand.** `scripts/marker.py` is the only source of markers, anchors, and note parsing; hand-rolling them silently breaks dedup across reruns.
- **In delivery mode the posted comments are the deliverable.** Do not also return the review markdown after a successful post.
- **Never re-invoke the `skill` tool to restart the review.** On a tool failure, switch to an alternative and continue — each phase reference lists its fallbacks.
- **Delta re-reviews are the default in delivery mode.** A re-review scopes detection to commits since the last review (`review-workflow.md`, scope stage). When the user asks for a full re-scan — e.g. `@daiv /code-review --full`, "review the whole MR again", or after a rebase/force-push — review the full `<target>...<head>` range instead.

## References

- `references/review-workflow.md` — the review itself (scope, detect, verify, severity, interactive output).
- `references/gitlab-delivery.md` — posting to GitLab (markers, dedup, inline + summary, pending replies, status line).
- `references/principles.md`, `references/few-shot-examples.md` — the *why* behind a finding and how short a useful comment can be; open a section when a finding's framing is unclear.
- `references/marker-format.md`, `examples/example-review-output.md` — marker field semantics and a complete delivery example; open during delivery if a field's purpose is unclear or you need the output shape.
