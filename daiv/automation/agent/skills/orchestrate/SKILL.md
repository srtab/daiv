---
name: orchestrate
description: Use when you are a coordinator on a coordination repo turning one request — a ticket, issue, security advisory, or direct instruction — into tailored per-repo sub-jobs via delegate_jobs, then reporting the consolidated outcome once every leg finishes.
---

# Orchestrate cross-repo work

You run on a **coordination repository**. Your job is to turn one request — a ticket, issue,
security advisory, or direct instruction — into tailored work across other repositories, then
report the combined outcome.

> **Precondition.** This skill depends on the `delegate_jobs` tool, which is bound by default but can
> be disabled per-repository via `orchestration.enabled: false` in `.daiv.yml`. If `delegate_jobs` is
> not in your available tools, orchestration has been disabled here — say so plainly and stop; do not
> try to emulate delegation by other means.

## Workflow

1. **Triage.** Read the request using your attached MCP tools. Consult `AGENTS.md` in this repo for
   the repository directory and routing rules. Decide which repositories are affected and what each
   one must do.
2. **Delegate.** Call `delegate_jobs(goal, targets)` once, with a tailored `prompt` per target.
   - Each leg runs in an isolated session and sees only the prompt you give it — not this
     conversation, the originating request, or your tool outputs. Put everything a leg needs
     directly in its prompt.
   - Include the no-change convention in each prompt: *"if this repository is unaffected, reply
     saying so and make no changes."*
3. **End your turn.** State your delegation plan and stop. Do **not** poll or wait — you will be
   resumed automatically once every leg finishes.
4. **Report.** On resume you receive a summary of all legs (status, MR links, replies). Verify the
   outcome, compose the consolidated result, and report it back to wherever the request originated
   (e.g. comment on the ticket or issue) using your MCP tools.
5. **Follow up (optional).** If a sequenced change is needed (e.g. adapt repo B against repo A's
   MR), delegate another batch — you will be resumed again.

## Limits

- Up to 10 targets per `delegate_jobs` call.
- Delegation depth is capped; a leg cannot itself delegate beyond the configured chain depth.
- You can only delegate as an authenticated coordinator; targets you lack write access to are
  reported back to you as failures rather than run.

Routing rules specific to your setup belong in this repo's `.agents/AGENTS.md`, not this skill.
