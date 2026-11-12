# Security Policy

## Reporting a vulnerability

Please do not report security vulnerabilities through public GitHub issues, discussions or pull requests.

Report them privately through GitHub's [private vulnerability reporting](https://github.com/srtab/daiv/security/advisories/new) instead. Include:

- what an attacker can do, and what they need first (a GitLab/GitHub account, a DAIV login, write access to a repository, a crafted issue or comment)
- the steps to reproduce it, or a proof of concept
- the DAIV version or image tag, and the sandbox version if the sandbox is involved

The report stays private between you and the maintainers until a fix is released. Once it is, the advisory is published and credits you, unless you ask not to be named.

## Supported versions

Security fixes go into `main` and the next release. Older releases do not get backports.

## Scope

These are in scope:

- running commands or reading files outside the sandbox container
- reaching hosts a sandbox environment's egress policy does not allow, or reading the credentials the egress proxy adds for allowed hosts
- leaking secrets (LLM keys, git platform tokens, sandbox environment secrets) to a repository, an issue, a merge request or the model
- triggering agent runs through the webhooks without a valid webhook secret, or as a user DAIV should not act for
- seeing or acting on repositories, sessions or runs that a DAIV user has no access to on the git platform

Vulnerabilities in the sandbox itself can be reported here or in [srtab/daiv-sandbox](https://github.com/srtab/daiv-sandbox/security/advisories/new).

Prompt injection through issue text, comments or repository files is a known risk for any coding agent. Report it when it gets past one of the controls above; a prompt that only makes the agent write bad code into a merge request a person then reviews is not a vulnerability on its own.

How the sandbox and its network controls work is described in [Sandbox](https://docs.daivagent.com/dev/features/sandbox/) and [Sandbox Environments](https://docs.daivagent.com/dev/features/sandbox-environments/).
