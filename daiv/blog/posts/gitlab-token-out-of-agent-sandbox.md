---
title: "Keeping your GitLab token out of the agent's sandbox"
date: 2026-12-08
summary: "When GitLab allows it, DAIV pushes with a token that can only use git, can push to one project, expires within 3 days and never enters the sandbox."
author: Sandro Rodrigues
image: token-flow.png
draft: true
---

A coding agent that opens merge requests on GitLab needs a token that can push. If that token sat in the agent's sandbox, any command the agent was talked into running could read it. A token with the `api` scope could then do whatever its user can, in every project that user belongs to. Below is how DAIV keeps the token out of the sandbox and narrows the one the sandbox can use, with the code, the side effects, and the cases where it falls back to the broader token.

DAIV is an open-source coding agent for GitLab and GitHub that you host yourself, and its agent works in a Docker sandbox from which the branch it produces is also pushed.

This is the token DAIV asks GitLab for, about once a day for each project it works on:

```python
CLONE_TOKEN_NAME = "daiv-clone"
CLONE_TOKEN_SCOPES = ["write_repository"]
CLONE_TOKEN_ACCESS_LEVEL = 30  # Developer: enough to push non-protected branches.
# Expiry is date-granular (token dies at 00:00 UTC on expires_at). Three days guarantees a token
# served at the very end of the CLONE_TOKEN_CACHE_TIMEOUT window still has >=24h of validity left.
CLONE_TOKEN_LIFETIME_DAYS = 3
```

([clone_tokens.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/clone_tokens.py#L17-L22))

![Four steps. 1, mint: a project token with write_repository only, the Developer role, pushing to one project, expiring within 3 days, minted through the API with DAIV's own token. 2, clone on DAIV's worker, with the token in a header set for one command, not in the URL, .git/config or argv. 3, work in the sandbox copy, which holds no git token; DAIV's bash tool rejects direct git push and commit commands, and DAIV pushes when the run ends. 4, push through the egress proxy, a separate container per session that adds the token to requests for the git host.](token-flow.svg)

## Two tokens, two jobs

DAIV needs GitLab for two different things, and they get different credentials.

| Credential | Used for | Scope | Lifetime | Where it lives |
|---|---|---|---|---|
| DAIV's personal access token | Reading issues, posting comments, opening merge requests, minting project tokens | `api`, in every project DAIV's user belongs to | The expiry you set for it | DAIV's configuration |
| Project token, one per project | `git clone` and `git push` | `write_repository`, Developer, pushes to one project | Expires within 3 days | DAIV's cache, the clone command's environment, the egress proxy |

The personal access token is the one the [platform setup guide](https://docs.daivagent.com/dev/getting-started/platform-setup/) asks for: DAIV's user is a Maintainer on each project, with a token that has the `api` scope. When everything works, it stays in DAIV's own process.

Everything the sandbox sends to the GitLab host carries the project token instead, added by the proxy. That includes API requests, since the API and git are on the same host. So the project token must be one that can do nothing but git.

## A token that can only push to one project

GitLab's `write_repository` scope allows git over HTTP and does not authenticate API requests. A project access token can push only to its own project, though GitLab also lets it read projects with Internal visibility. The Developer role is "enough to push non-protected branches", which is what a run needs to publish its branch. It can also push to a protected branch whose rules allow Developers.

DAIV mints one token per project, caches it for a day, and asks for an expiry three days out. GitLab expires tokens at midnight UTC on the date given, so a token served at the end of its day in the cache still has at least 24 hours left.

DAIV never revokes these tokens; each one expires on its own. Revoking a token would break every run still using it, because the egress proxy of a running sandbox holds that exact token ([clone_tokens.py, on revocation](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/clone_tokens.py#L34-L47)). When two workers miss the cache at the same moment, each mints a token. The code accepts that: it happens at most about once a day per project, and costs less than coordinating the workers ([clone_tokens.py, on races](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/clone_tokens.py#L53-L58)).

On GitHub the equivalent is an installation token for DAIV's GitHub App, valid for an hour ([GitHub client, clone](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/github/client.py#L303-L306)). An unrestricted installation token works on every repository the installation covers. PyGithub's helper cannot restrict it, so DAIV calls the REST endpoint itself with `repository_ids` set to the one repository. It asks for `contents: write`, plus `workflows: write` only when the installation grants it ([GitHub client, minting](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/github/client.py#L325-L356)).

Because a GitHub token lasts an hour, DAIV tries to mint a fresh one before it publishes and swap it into the proxy of the running session. If that fails, it publishes with the token from the start of the turn ([sandbox.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/automation/agent/workspace/sandbox.py#L78-L102)).

## The token never lands in the clone

DAIV clones the repository on its own worker, and the clone's `.git/config` is then seeded into the sandbox ([GitLab client, clone](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/client.py#L436-L443)). A token in the clone URL would be saved in that file, and a token passed as a command-line argument would show in a process listing.

So the token travels as git configuration in the clone command's environment:

```python
return {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": self.config_key,
    "GIT_CONFIG_VALUE_0": self.header.get_secret_value(),
    **GIT_NO_PROMPT_ENV,
}
```

([base.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/base.py#L163-L168))

The key is `http.<origin>.extraheader` and the value is an `Authorization: Basic` header for the token. git applies it to that one command and writes it nowhere. Prompts are switched off too, so a rejected token fails at once instead of waiting for a password.

Inside the sandbox, git talks to GitLab through the session's egress proxy, which adds the same `Authorization` header to requests for the git host ([egress.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/core/sandbox/egress.py#L1-L8)). The proxy, and why the sandbox has no other route out, is the subject of [the companion post on network access](/blog/coding-agent-network-access-without-secrets/).

## The agent's bash tool refuses `git push`; DAIV publishes

The agent has a shell in the sandbox, and git in it. Its `bash` tool parses each command before running it and refuses a fixed list: `git push`, `git commit`, `git config`, `git checkout` and other history- and branch-changing commands, plus the `gh` and `gitlab` command-line tools. The list is built in, and no setting can remove an entry from it ([command_policy.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/automation/agent/workspace/bash_policy/command_policy.py#L18-L56)).

DAIV makes the push itself when the run ends, through the same sandbox. So the agent decides what changes, and DAIV decides where they go: the merge request's own branch, or a new branch for a new merge request ([publishers.py, branch](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/automation/agent/publishers.py#L236-L241)).

## What a narrow token costs

Narrow tokens have three side effects. Two of them needed code; the third is accepted.

**A new token is not valid at once.** GitLab can reject a project token for git for a short while after creating it. DAIV retries the same token after 1, 2, 4 and 8 seconds, since minting another would only start the same wait again ([GitLab client, retry](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/client.py#L65-L73)). A cached token can also die early, if someone revokes it or the project is reset. When the retries run out, DAIV drops the cached token and mints a new one, once ([GitLab client, re-mint](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/client.py#L476-L494)).

**CI runs as the token's bot user.** A project access token pushes as a bot user of that one project. A pipeline started by that push cannot read CI configuration that the project includes from another private project. So DAIV pushes with `-o ci.skip`, and for a push to an existing merge request it then creates the merge request's pipeline itself, with its personal access token ([publishers.py, skip](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/automation/agent/publishers.py#L243-L256)).

If that request fails, DAIV cannot confirm that the new commit has a pipeline that runs. The code's rule for that case is to surface it "loudly … and visibly …, never silently": DAIV logs an error and writes a note on the merge request ([publishers.py, trigger](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/automation/agent/publishers.py#L371-L429)).

**Tokens pile up.** Since none is revoked, a project DAIV works on every day carries a few `daiv-clone` tokens at a time, each at most three days from expiry.

## When DAIV falls back to its own token

GitLab does not always let DAIV mint a project token. DAIV then uses its personal access token, for the clone and for the proxy, and logs that it did so on every clone ([GitLab client, fallback](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/client.py#L529-L558)). After a failed mint, DAIV uses the personal access token for that project without asking GitLab again: for 5 minutes after a network error, a rate limit or a server error, and for an hour after any other failure ([clone_tokens.py, failures](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/clone_tokens.py#L90-L116)), and a running session's proxy keeps that token until a mint succeeds and DAIV next updates the proxy, at the start of a turn or before a publish. The usual causes of the second kind are:

- DAIV's user is below Maintainer on the project;
- the GitLab plan has no project access tokens, as on GitLab.com Free;
- GitLab returns a project token without its secret.

If GitLab rejects DAIV's own token, the fallback uses that same token, and the clone most likely fails too.

The personal access token still never enters the sandbox. But the proxy adds it to every request for the GitLab host, and it has the `api` scope. While the fallback lasts, anything running in the sandbox can call the GitLab API with DAIV's permissions, through the proxy.

## What this does not protect against

- **Using the token through the proxy.** The sandbox cannot read the token, but the proxy authenticates whatever it sends to the git host. With a project token, that means pushing to any branch of the project a Developer may push to. The command policy stops the agent typing `git push`. As I read it, it checks the command line, so a script the agent writes and runs is not checked. The token's scope is the real limit.
- **CI on a branch the sandbox pushes.** A push from the sandbox can change the CI configuration on an unprotected branch, and that pipeline runs with the variables GitLab gives unprotected branches. On GitHub, a token with `workflows: write` can change `.github/workflows/` the same way. My reading: keep secrets that matter in protected variables, on branches Developers cannot push to, and grant `workflows: write` only where DAIV needs it.
- **Tokens outliving the run.** A GitLab project token stays valid for up to three days after the run that used it.
- **Several repositories in one session.** Each additional repository's token is added for its own path, and the main repository's token for every other request to the host, so the sandbox can push to each of them.
- **The fallback.** Whenever GitLab won't mint a project token, the sandbox can use an `api`-scoped token through the proxy.

## Trying it

The [platform setup guide](https://docs.daivagent.com/dev/getting-started/platform-setup/) covers DAIV's user, its role and its token on GitLab, and the GitHub App on GitHub.

After DAIV's first run on a GitLab project, the project's **Settings** → **Access tokens** page lists a `daiv-clone` token with the `write_repository` scope, expiring within three days. If none is listed, the token may have expired or been revoked, or the runs used the personal access token. For a given clone, DAIV's logs confirm the fallback with `with the configured PAT (ephemeral clone token unavailable)` at INFO level.
