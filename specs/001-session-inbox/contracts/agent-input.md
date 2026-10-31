# Contract: what the model receives

This is an agent-visible change (Constitution V). The `inbox` eval marker measures it (R18).

## Message order for an inbox run

1. **Context** (`synthetic_message(kind="inbox")`). Hidden from the transcript, and omitted when there is nothing to report.
2. **Request** (`HumanMessage`):
   - people's words, when any were taken: chat messages, the follow-up job prompt, mentions with their authors;
   - otherwise a DAIV-written prompt, tagged so the transcript shows its notice, e.g. "DAIV · CI failed".

The executor's existing `input_messages` path carries both (`RunSpec.input_messages`).

## Context message format

```text
Since your last run on this session:

- CI on !9 (branch chore/install-gettext): pipeline 830724 FAILED on e96f1f77, jobs: image:build [7.4 …]. https://…/pipelines/830724
  Superseded by pipeline 830731 on the same commit: PASSED (retried by sfr).
- The branch chore/install-gettext moved: sfr pushed 2 commits (e96f1f77 → 0a1b2c3d). Your working copy was fast-forwarded.
- Comment by alice on !9 (not addressed to you): "Looks good once CI is green."
- !9 was merged into main by sfr.
- Delegated session 3f2a… (!12 in group/other): CI passing.
```

**Rules**:
- **Order**: one line per surviving event, oldest first. A superseded event appears only as an indented "Superseded by …" line under its replacement's subject.
- **Branch line**: the fast-forward result (R17) is part of the branch line: "was fast-forwarded", or "could not be fast-forwarded because you have unpublished commits".
- **Quoting**: mention and comment bodies are quoted, and the quote is cut at 1,000 characters.
- **Size**: the message is capped at 8,000 characters. Over the cap, the oldest remembered lines collapse into "N more <kind> events". Waking events are never collapsed (FR-014).
- **Owner identity**: the message never states the owner's identity or credentials.

## Request message

| Waking events taken | Request content |
|---|---|
| Chat messages | The messages' text, joined in order, each prefixed by its time when there are several |
| Mention(s) | `<author> mentioned you on !<iid> (<web_url>):` and the quoted comment, per mention. Then: "Reply as you would on the merge request; your final message is posted there." |
| Follow-up job | The job prompt, unchanged |
| Delegated batch finished | The existing `render_batch_summary` text |
| CI failure only | The existing fix prompt (`FIX_RUN_PROMPT`): failed jobs, pipeline URL, and "If the failure is not something a code change can fix, explain why instead of guessing" |

When several kinds are taken together, the sections appear in the precedence order of [data-model.md](../data-model.md#kinds), separated by a blank line.
