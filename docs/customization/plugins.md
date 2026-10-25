# Plugins

Plugins let you bring skills, commands and subagents that other people publish for Claude Code into DAIV, straight from a Git repository. You install a plugin once from the dashboard, and DAIV keeps it up to date and makes its skills available in your runs.

## What DAIV uses from a plugin

A plugin is a repository (or a folder inside one) with a `.claude-plugin/plugin.json` manifest, or a marketplace with a `.claude-plugin/marketplace.json` that lists several plugins. DAIV reads these parts:

| Part | What the agent gets |
|------|---------------------|
| Skills (`skills/<name>/SKILL.md`) | A skill named `<plugin>:<skill>`, listed in the `/` menu and loadable by the agent |
| Commands (`commands/*.md`) | A skill named `<plugin>:<command>`, used like a skill |
| Agents (`agents/*.md`) | A subagent named `<plugin>:<agent>` the agent can delegate to |

DAIV skips everything else: hooks, MCP servers (`.mcp.json` or `mcpServers`), output styles and LSP servers. A skipped part never runs. The plugin's page lists what was skipped, and so does any skill, command or agent that DAIV can't read (for example, one without a description or with an invalid name).

!!! note
    A marketplace entry is installable only when its `source` is a path inside the same repository (`./...`). Entries that point to another repository or a package are shown as unsupported.

Plugin, skill, command and agent names use lowercase letters, digits and hyphens. Plugin items are always addressed with the `<plugin>:` prefix, so they never replace a built-in or repository skill or subagent.

## Install a plugin

1. Open **Plugins** in the dashboard sidebar and add a plugin.
2. Paste the repository's plain `https` link, for example `https://github.com/owner/repo`.
3. Pick the plugins to install, and choose who gets them.
4. Confirm.

DAIV fetches the repository's default branch and records the commit it installed. Links must use `https`, carry no credentials, query string or port, and point to the repository itself rather than a page inside it. SSH links (`git@host:...`) are rejected.

A repository is limited to 200 MB, and a plugin to 1000 files, 50 MB in total and 5 MB per file. You can change these limits with the [`PLUGINS_*` variables](../reference/env-variables.md#plugins).

## Global and personal plugins

| | Personal | Global |
|---|---|---|
| Who can install | Any user | Admins |
| Who gets it | Only the user who installed it | Everyone |
| Who can change or remove it | Its owner (and admins) | Admins |

If a global and a personal plugin share a name, the personal one wins in that user's runs.

Admins manage global plugins from **Global plugins** in the sidebar. Any user can stop a global plugin from loading in their own runs with **Use in my runs**, without affecting anyone else.

### Who can read which repository

Where a plugin is installed from decides which repository DAIV can reach:

- **Public repositories on any host.** DAIV fetches them anonymously. The host must be reachable from DAIV over the public internet; hosts that resolve to private addresses are refused. This includes public repositories on DAIV's own GitLab or GitHub.
- **Private and internal repositories on DAIV's own platform.** DAIV fetches them with its own credentials. A personal install requires that you can read the repository on the platform, which DAIV checks when you install and again on every update. An admin installing a global plugin skips that check.
- **Private repositories elsewhere** are not supported.

Repository links with an explicit port are rejected, so a git platform served on a non-default port (or a local `http` development GitLab) can't install plugins from that platform. Public repositories on other `https` hosts still work.

!!! warning
    For a private plugin repository, DAIV's bot account must be able to read it. If it can't, the install and every later update fail with "DAIV can't read this repository."

If you lose access to a repository on the platform, your personal plugin from it stops loading in your runs and shows **Update failed**. It loads again after an update confirms you can read it again.

## Updates

DAIV checks every plugin hourly and installs every new commit on the repository's default branch. It doesn't wait for a version bump. Use **Update now** on a plugin's page to check right away.

- **A failed update keeps the last good commit.** If the new commit can't be fetched or read, the plugin keeps running on the previous one and shows **Update failed** with the reason. DAIV doesn't retry a commit it couldn't fetch or read until the repository has a newer one; **Update now** retries it. Other failures, such as the repository being unreachable or you losing access, are retried at the next hourly check.
- **Auto-update** is on by default. Turn it off to freeze a plugin at its installed commit; it still gets its access checked hourly.
- **Active** turns a plugin off without removing it. An inactive plugin isn't loaded in anyone's runs (for a global plugin, in nobody's), but it keeps updating.

!!! note
    A plugin with auto-update off whose files were lost, for example because the data volume was replaced, stays unloaded until someone clicks **Update now**. A plugin with auto-update on is restored by the next hourly check, unless its last update also failed.

## In your runs

Each run loads the plugins of the user who triggered it:

- **Chat, the Jobs API, MCP and scheduled runs** load that user's personal plugins plus the global ones, minus any global plugin they switched off with **Use in my runs**.
- **Webhook runs** (an `@daiv` mention or a label on an issue or merge request) load the personal plugins of the author only when their GitLab or GitHub account is linked to their DAIV account. Otherwise they load global plugins only. DAIV matches the linked platform account, never a username or email address.

The agent learns about plugin skills and agents when a turn starts, and again whenever the set changes. Plugins never change DAIV's system prompt or tools, so they don't invalidate the model's prompt cache.

A plugin that fails to load never fails the run: DAIV runs that turn without plugins and logs the problem.

## Operating DAIV

### Data volume

Plugin files live under `~/data/plugins` in the containers (`PLUGINS_DATA_PATH`). The web and worker containers must share this volume, like `~/data/skills`: the web app installs plugins and the workers read them. Without a shared volume, plugins installed from the dashboard aren't visible to runs.

### Network access

DAIV runs `git` over `https` to fetch plugins. It honors the proxy and CA variables of the container (`http_proxy`, `https_proxy`, `no_proxy`, `all_proxy`, `GIT_SSL_CAINFO`, `GIT_SSL_CAPATH`, `SSL_CERT_FILE` and `SSL_CERT_DIR`, in either case), but ignores the system and global Git configuration (`/etc/gitconfig`, `~/.gitconfig`) and `~/.netrc`. Set a proxy or custom CA with those variables, not in a Git config file.

### Configuration

See [Plugins](../reference/env-variables.md#plugins) in the environment variables reference.
