---
name: orchestrate
description: Use when you are a coordinator on a coordination repo and must triage a ticket, delegate tailored per-repo sub-jobs with delegate_jobs, and report the consolidated outcome back once they finish.
---

# Orchestrate cross-repo work

You run on a **coordination repository**. Your job is to turn one request (often a ticket) into
tailored work across other repositories, then report the combined outcome.

> **Precondition.** This skill depends on the `delegate_jobs` tool, which is bound only when this
> repository sets `orchestration.enabled: true` in `.daiv.yml`. If `delegate_jobs` is not in your
> available tools, orchestration is not enabled here — say so plainly and stop; do not try to
> emulate delegation by other means.

## Workflow

1. **Triage.** Read the request/ticket using your attached MCP tools. Consult `AGENTS.md` in this
   repo for the client's repository directory and routing rules. Decide which repositories are
   affected and what each one must do.
2. **Delegate.** Call `delegate_jobs(goal, targets)` once, with a tailored `prompt` per target.
   - Include the ticket context each leg needs — legs run in isolation and cannot see the ticket
     unless they also have the ticketing MCP tools.
   - Include the no-change convention in each prompt: *"if this repository turns out to be
     unaffected, reply saying so and make no changes."*
3. **End your turn.** State your delegation plan and stop. Do **not** poll or wait — you will be
   resumed automatically once every leg finishes.
4. **Report.** On resume you receive a summary of all legs (status, MR links, replies). Verify the
   outcome, compose the consolidated result, and post it back to the ticket via your MCP tools.
5. **Follow up (optional).** If a sequenced change is needed (e.g. adapt repo B against repo A's
   MR), delegate another batch — you will be resumed again.

## Limits

- Up to 10 targets per `delegate_jobs` call.
- Delegation depth is capped; a leg cannot itself delegate beyond the configured chain depth.
- You can only delegate as an authenticated coordinator; targets you lack write access to are
  reported back to you as failures rather than run.

Client-specific routing knowledge belongs in this repo's `.agents/AGENTS.md`, not this skill.
