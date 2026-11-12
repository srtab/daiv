# MCP tools

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) tools extend DAIV's agent with access to external services. You manage MCP servers from the dashboard on two pages:

- **MCP Servers**, in the sidebar's **Settings** menu as **MCP servers**, shows your personal servers and a read-only list of the global servers that load in every run.
- **Global MCP Servers**, in the sidebar's **Settings** menu under **Admin** as **Global MCP servers**, is where admins manage the built-in and custom global servers.

The DAIV app opens the connections itself at runtime, so no sidecar containers are required. DAIV skips a server that does not return its tools within the load timeout (see [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/#mcp-tools)).

## Server settings

Each server is a row with these settings:

| Setting         | Description                                                                                                                                                                                                                                                                                             |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Name**        | Unique slug of lowercase letters, digits and dashes, up to 80 characters. You cannot change it after you create the server. `new` and `test` are reserved.                                                                                                                                              |
| **Description** | Optional text.                                                                                                                                                                                                                                                                                          |
| **Status**      | **Active** loads the server in every run (for a personal server, every run that acts as its owner). **On-demand** loads it only in runs that opt in, for example with `mcp_overrides` in the [jobs API](https://docs.daivagent.com/dev/features/jobs-api/#request-fields). **Disabled** never loads it. |
| **Transport**   | **HTTP (streamable)** or **SSE**.                                                                                                                                                                                                                                                                       |
| **URL**         | The MCP endpoint, an `http` or `https` URL of up to 200 characters.                                                                                                                                                                                                                                     |
| **Headers**     | HTTP headers sent to the server. Each header is a literal value, encrypted at rest, or an `env_ref`, the name of an environment variable that DAIV resolves at runtime.                                                                                                                                 |
| **Tool filter** | Which of the server's tools the agent sees (see [Tool filtering](#tool-filtering)).                                                                                                                                                                                                                     |

There is no scope field on the form: the page you create the server from decides it.

The **Tool filter** section is hidden until it has something to act on. On a new server it appears after a successful **Test connection**. On an existing server it appears once DAIV has synced the server's tools, or when a filter is active. The **Refresh tools** button next to the heading syncs the tools again, and the heading shows when the last sync ran and how many tools it found.

### Tool filtering

Set **Tool filter mode** to one of:

- **None**: the agent sees all the server's tools.
- **Allow only listed tools**: the agent sees only the listed tools.
- **Block listed tools**: the agent sees all tools except the listed ones.

A filter other than **None** needs at least one tool name. An allow-list fails closed: a tool that disappears upstream stops being exposed.

## Security

MCP connections bypass the sandbox egress proxy

MCP connections originate from the DAIV app and worker containers, so the sandbox egress proxy does not apply to them. Restrict outbound access at your network layer if you need to.

- DAIV encrypts header values at rest and never renders them back into the form. An `env_ref` header keeps the value out of the database entirely.
- Prefer allow-lists that exclude tools that change data, as the seeded Sentry filter does.

## Built-in servers

DAIV seeds two built-in servers that point at the official remote MCP endpoints. You can edit a built-in server's URL, headers and tool filter, but you cannot rename or delete it, only disable it.

| Server     | URL                                                                      | Status                                                       | Tool filter                                                        |
| ---------- | ------------------------------------------------------------------------ | ------------------------------------------------------------ | ------------------------------------------------------------------ |
| `sentry`   | `https://mcp.sentry.dev/mcp?disable-skills=seer,docs,project-management` | **Disabled**, because the endpoint requires authentication   | **Allow only listed tools**, with read-only tools only             |
| `context7` | `https://mcp.context7.com/mcp`                                           | **Active**, and works without credentials at low rate limits | **Allow only listed tools**: `resolve-library-id` and `query-docs` |

### Enable the Sentry server

You need an admin account and a [Sentry User Auth Token](https://docs.sentry.io/account/auth-tokens/) with the read scopes `org:read`, `project:read`, `team:read` and `event:read`.

1. In the sidebar, select **Settings** → **Global MCP servers**.
1. Under **Built-in**, select **Edit** on the `sentry` server.
1. Under **Headers**, select **Add header**.
1. Enter `Authorization` as the header name.
1. Enter `Sentry-Bearer <your token>` as the value, and leave the mode as `literal`.
1. Select **Test connection**. DAIV reports how many tools it discovered.
1. Optional: under **Tool filter**, adjust which tools the agent sees.
1. Under **Status**, select **Active**.
1. Select **Save**.

Replace `<your token>` with your Sentry token. `Sentry-Bearer`, not `Bearer`, is Sentry's scheme for passing an API token directly to its hosted MCP. To keep the token out of the database, set the header's mode to `env_ref` and enter the name of an environment variable that holds the whole `Sentry-Bearer <your token>` string.

### Raise the `context7` rate limits

You need an admin account and an API key from [context7.com/dashboard](https://context7.com/dashboard).

1. In the sidebar, select **Settings** → **Global MCP servers**.
1. Under **Built-in**, select **Edit** on the `context7` server.
1. Under **Headers**, select **Add header**.
1. Enter `CONTEXT7_API_KEY` as the header name.
1. Enter your API key as the value.
1. Select **Save**.

## Add a server

You can add any server that DAIV reaches over HTTP or SSE. A server that only speaks stdio needs a [bridge](#stdio-only-servers) first. Any member can add a personal server. Only admins can add a global one.

1. In the sidebar's **Settings** menu, open the page for the server's scope: **MCP servers** for a personal server, or, as an admin, **Global MCP servers** under **Admin** for a global one.
1. Select **New server**.
1. Enter a **Name**.
1. Choose a **Transport**.
1. Enter the **URL**.
1. Optional: select **Add header** and enter each header's name, mode and value.
1. Select **Test connection**. DAIV reports how many tools it discovered.
1. Optional: under **Tool filter**, choose a **Tool filter mode** and select the tools.
1. Set **Status**.
1. Select **Save**.

**Test connection** fails after 5 seconds. A personal server's headers are always literal (see [Personal servers](#personal-servers)).

## Stdio-only servers

DAIV connects over HTTP and SSE only. To use a stdio-based MCP server, wrap it with a stdio-to-HTTP bridge in its own container, then [add the bridge's URL](#add-a-server) as a server. Run one bridge container per MCP server, so a failure in one does not affect another.

## Connect an on-premise Sentry

Sentry's hosted MCP serves only sentry.io. The official [`@sentry/mcp-server`](https://www.npmjs.com/package/@sentry/mcp-server) package is stdio-only, so for a self-hosted Sentry you expose it over HTTP yourself and point the `sentry` server's URL at it. You have two options.

### Bridge the stdio package

Any stdio-to-HTTP bridge works. This example uses [supergateway](https://github.com/supercorp-ai/supergateway).

Run the bridge in stateful mode

Pass `--stateful`. In stateless mode every tool call starts a fresh child process chain, which can exhaust the system's thread limit under concurrent load. Pass `--sessionTimeout` in milliseconds (`300000` works well) to clean up idle sessions.

1. Add the bridge to your Compose file:

   ```
   mcp-sentry:
     image: supercorp/supergateway:latest
     restart: unless-stopped
     command:
       - --stdio
       - "npx @sentry/mcp-server@latest --host=sentry.example.com"
       - --outputTransport
       - streamableHttp
       - --healthEndpoint
       - "/healthz"
       - --stateful
       - --sessionTimeout
       - "300000"
     environment:
       SENTRY_ACCESS_TOKEN: ${SENTRY_ACCESS_TOKEN}
     healthcheck:
       test: ["CMD", "wget", "--spider", "-q", "http://localhost:8000/healthz"]
       interval: 30s
       timeout: 5s
       retries: 3
       start_period: 30s
   ```

1. Set `SENTRY_ACCESS_TOKEN` in the environment that the Compose file reads.

1. Start the container.

1. In the sidebar, select **Settings** → **Global MCP servers**.

1. Under **Built-in**, select **Edit** on the `sentry` server.

1. Set **URL** to `http://mcp-sentry:8000/mcp`.

1. Under **Headers**, remove the `Authorization` header, because the bridge authenticates with its own `SENTRY_ACCESS_TOKEN`.

1. Select **Test connection**.

1. Under **Status**, select **Active**.

1. Select **Save**.

Replace `sentry.example.com` with your Sentry host. For a Sentry that serves plain HTTP, add `--insecure-http` to the `npx` command.

### Deploy Sentry's worker

The [sentry-mcp](https://github.com/getsentry/sentry-mcp) repository ships the open-source Cloudflare worker behind mcp.sentry.dev. Deploying your own instance gives you an HTTP endpoint without a bridge. Set the `sentry` server's URL to your worker, and keep the `Authorization: Sentry-Bearer <token>` header on it.

## Personal servers

Every member, admins included, can add personal MCP servers from the **MCP Servers** page. **New server** there always creates a server that you own. Personal servers use the same transports, tool filters and **Test connection** as global custom servers, with these differences.

**Where they load.** A personal server loads only in runs where DAIV knows whom the run acts as. Those are chat sessions, dashboard-started runs, jobs submitted through the API or MCP interface, and runs that a GitLab or GitHub event starts for a person who signed in to DAIV with that platform account (an issue label or mention, or a mention on a merge request a DAIV session owns). A session's CI fixes act as its owner, and a resumed delegation as the user who delegated. A mention on a merge request no session owns, or by someone who never signed in with that account, has no acting user, and DAIV loads **global servers only**.

**Header values are always literal.** A personal server's headers cannot be an `env_ref`, so a member cannot probe the DAIV host's environment through their own configuration.

**Name collisions.** A personal server cannot take a global server's name when you create it. If an admin later adds a global server with the same name, the global server wins while it is **Active** or **On-demand**: DAIV skips the personal server in every run, and the **MCP Servers** page flags it with a **Shadowed** badge.

**Admin oversight.** On the **MCP Servers** page, admins have an owner dropdown, set to **You** by default, to list any member's personal servers. Admins can disable, delete or refresh those servers. They cannot edit them or see their header values.

### Resolve a name collision

You cannot rename a server, so you delete the shadowed personal server and create it again under another name.

Deleting a server deletes its headers

DAIV removes the server and its stored headers permanently, and the form never shows header values. Keep the values somewhere else before you delete. A new server starts with no headers.

1. In the sidebar, select **Settings** → **MCP servers**.
1. Find the server marked **Shadowed**, and note its settings. Have its header values from your own records, because the form never shows them.
1. Select **Delete** on it.
1. On the confirmation page, select **Delete**.
1. Select **New server**.
1. Enter a **Name** that no global server uses, then re-enter the other settings and headers (see [Add a server](#add-a-server)).
1. Select **Save**.

## Related pages

- [MCP endpoint](https://docs.daivagent.com/dev/features/mcp-endpoint/index.md): the reverse direction, which exposes DAIV itself as an MCP server so Claude Code, Cursor or Codex CLI can delegate to it.
- [Request Tracker triage](https://docs.daivagent.com/dev/integrations/rt/index.md): a worked example of DAIV driven from an external system.
- [Agent tools](https://docs.daivagent.com/dev/reference/agent-architecture/index.md): when MCP tools join the agent's tool set.
- [Environment variables](https://docs.daivagent.com/dev/reference/env-variables/#mcp-tools): the MCP-related settings.
