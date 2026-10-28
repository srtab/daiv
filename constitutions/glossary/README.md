# Ubiquitous language

One word per concept, in code, docs and conversation. A synonym in a name is a second concept
as far as the next reader is concerned.

<!-- prettier-ignore -->
| Term | Meaning | Not |
| --- | --- | --- |
| Session | One conversation with the agent; its primary key `thread_id` is also the LangGraph checkpoint key | thread, chat, activity |
| Run | One agent execution inside a Session. The Jobs API and MCP tools call it a job, and their `job_id` is the Run's id | activity, execution |
| Origin | What started a Session (`SessionOrigin`, `Session.origin`) | source |
| Trigger type | What started one Run (`Run.trigger_type`); a Session started by a webhook can hold chat Runs | origin |
| Workspace | What a run's agent works in: its files, its git and, on a sandbox run, its shell (`DiskWorkspace`, `SandboxWorkspace`) | backend |
| Sandbox environment | A reusable runtime: base image, CPU and memory, egress policy and encrypted secrets, scoped to repositories (`SandboxEnvironment`) | sandbox config |
| Sandbox session | One running sandbox a run acquires and releases | container |
| Egress policy | A sandbox environment's allow-list of outbound network access; without one the sandbox is network-isolated | network flag |
| Provider | A configured LLM provider (`Provider` row) that model specs resolve against | vendor, backend |
| Model spec | A `provider:model` string naming one model on one provider | — |
| Skill | A packaged set of instructions the agent loads (`SKILL.md`): built-in, or from a repository's `.agents/skills/` | — |
| Subagent | A specialised agent the main agent delegates to, built-in or from `.agents/subagents/` | worker |
| Detector | A `/code-review` subagent (`cr-*`) that checks one concern | reviewer |
| Middleware | A layer of the agent stack that provides tools or shapes the model request (`automation/agent/middlewares/`) | plugin |
| Deferred tool | A tool the model loads on demand through `tool_search`; tools in `ALWAYS_LOADED_TOOLS` are not deferred | lazy tool |
| Repository memory | What DAIV remembers about a repository (`MemoryEntry`, `RepositoryMemory`) | notes |
| Bot label | An issue or merge/pull request label that hands work to DAIV: `daiv`, `daiv-max`, `daiv-auto` | tag |
| Schedule | A saved job that submits runs on a cron schedule, or once | cron job |
