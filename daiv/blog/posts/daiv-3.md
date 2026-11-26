---
title: "DAIV 3.0: an open-source coding agent that works inside GitLab and GitHub"
date: 2026-11-25
summary: "DAIV turns issues into merge requests on your own infrastructure, with the model you choose and a sandbox whose network you define. Version 3.0 adds sessions you can watch, measured code review and egress control."
author: Sandro Rodrigues
image: social-preview.png
---

DAIV (pronounced "dive") is an open-source coding agent that you run on your own infrastructure. It connects to GitLab, self-managed included, and to GitHub, and it works in the issues and merge requests your team already uses. DAIV 3.0.0 is out now.

This is the first post on this blog, so it starts with what DAIV does, then covers what is new in 3.0.

## How it works

Add the `daiv` label to an issue and DAIV reads the repository and posts a plan as a comment. Reply `@daiv proceed`, or anything else that says go ahead, and it opens a merge request with the change.

![The plan DAIV posted on a GitLab issue that asks for pagination on the item list](plan.webp)

When the pipeline fails, DAIV tries to fix it, and explains why when it can't. When a reviewer mentions `@daiv` on the merge request, it answers or pushes a change, with the context of the work it already did.

![The merge request DAIV opened for that issue, with its pipeline passed and ready to merge](merge-request.webp)

DAIV is Apache-2.0, with no per-seat pricing. You bring the model: Anthropic, OpenAI, Google, OpenRouter, or your own OpenAI-compatible server such as vLLM, llama.cpp or LiteLLM.

## What's new in 3.0

Version 3.0 is eight months of work since 2.0.0. These are the four changes that matter most.

### Every run is a session you can watch

Whatever starts a run, whether an issue label, a mention on a merge request, the dashboard chat, the Jobs API, MCP or a schedule, it is now a session. You can watch it live, continue the conversation, retry it, stop it, and see what it cost. **Home** lists what needs you, **Team insights** shows run and code-velocity trends, and **Spend** shows what DAIV spent on models by person, repository, trigger and model.

![A session in the DAIV dashboard: the agent's summary of the change, the merge request it opened, and the run's context use and cost](session.webp)

### Code review you can measure

`/code-review` posts one report on the merge request, written by detector subagents chosen by what the change touches and by your own rules in `.agents/review-rules.md`. On ReviewBench, 25 real pull requests with 352 known issues, it found 150 of them at 92.1% precision, for about $0.31 a review on Claude Haiku 5.5. The [benchmarks page](https://docs.daivagent.com/latest/reference/benchmarks/) has the method, and the [experiment log](https://github.com/srtab/daiv/tree/v3.0.0/evals/results/reviewbench) has every experiment, including the ones that failed.

![A code review report on a merge request, with two important issues, a question and recommended actions](code-review.webp)

### A sandbox on the network you choose

Every run works inside a sandbox, where the agent runs your tests and builds. The sandbox reaches only your git platform, unless its environment allows more hosts through an egress proxy. The proxy adds the credentials for those hosts, so they never enter the container, and GitLab clones use short-lived tokens scoped to one project. When the environment turns it on, the agent can also drive a headless browser to check the page it just built.

![The sandbox environment form: unlisted hosts denied, four allowed hosts, and a credential the proxy adds for one of them](egress.webp)

### Your models and tools, from anywhere

Pick providers and models in the dashboard, then a model and thinking level per run, skill and subagent. Connect Claude Code, Cursor or Codex CLI to DAIV's MCP endpoint and hand it longer work. Call the Jobs API from GitLab CI or GitHub Actions with the pipeline's own ID token instead of an API key, or schedule recurring jobs. The agent can also hand work to other repositories, stop to ask you a question, and remember what it learned about each repository.

## Try it

- [Quickstart](https://docs.daivagent.com/latest/getting-started/quickstart/): deploy DAIV with Docker Compose and connect a repository.
- [Release notes](https://github.com/srtab/daiv/releases/tag/v3.0.0), and [UPGRADING.md](https://github.com/srtab/daiv/blob/v3.0.0/UPGRADING.md) if you run 2.0.0. Version 3.0 has breaking changes, so follow it in order.
- [GitHub Discussions](https://github.com/srtab/daiv/discussions) for questions and feedback.

If your team runs self-managed GitLab and wants to try DAIV, I'm looking for a few teams to set it up with over the next weeks. Start a discussion and say hello.

DAIV now has its own accounts on [LinkedIn](https://www.linkedin.com/company/daivagent), [X](https://x.com/daivagent), [Bluesky](https://bsky.app/profile/daivagent.com), [Mastodon](https://mastodon.social/@daivagent) and [YouTube](https://www.youtube.com/@daivagent). Follow along there, or through this blog's RSS feed.
