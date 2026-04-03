# MCP Endpoint

DAIV exposes a [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server endpoint at `/mcp/`, allowing MCP clients like [Claude Code](https://docs.anthropic.com/en/docs/claude-code) to connect directly to your DAIV instance via a remote URL with browser-based OAuth 2.0 authentication.

This is useful when you want to:

- **Use DAIV from Claude Code** — submit jobs and check results directly from your terminal
- **Connect any MCP client** — any tool that speaks MCP over Streamable HTTP can use DAIV as a remote server
- **Leverage browser-based auth** — OAuth 2.0 with PKCE handles authentication via your existing DAIV login

## Authentication

The MCP endpoint uses **OAuth 2.0 with PKCE** (Proof Key for Code Exchange). MCP clients handle the full flow automatically:

1. The client discovers OAuth endpoints via `/.well-known/oauth-authorization-server`
2. The client registers itself via dynamic client registration (`/oauth/register/`)
3. The user is redirected to the browser to log in and authorize the client
4. The client receives an access token and uses it for subsequent MCP requests

No manual API key creation is needed — the browser-based flow handles everything.

### OAuth endpoints

| Endpoint | Description |
|----------|-------------|
| `/.well-known/oauth-authorization-server` | OAuth 2.0 metadata discovery (RFC 8414) |
| `/oauth/register/` | Dynamic client registration (RFC 7591) |
| `/oauth/authorize/` | Authorization endpoint |
| `/oauth/token/` | Token endpoint |
| `/oauth/revoke_token/` | Token revocation |

### Token lifecycle

| Setting | Default | Description |
|---------|---------|-------------|
| Access token expiry | 1 hour | After expiry, the client uses the refresh token to obtain a new one |
| Refresh token expiry | 24 hours | After expiry, the user must re-authenticate via the browser |

## Available tools

The MCP endpoint exposes the same capabilities as the [Jobs API](jobs-api.md):

| Tool | Description |
|------|-------------|
| `submit_job` | Submit a prompt to the DAIV agent for a repository |
| `get_job_status` | Poll the status and result of a submitted job |
| `list_repositories` | Discover repositories that DAIV has access to |

## Connecting from Claude Code

Add the DAIV MCP server to Claude Code:

```bash
claude mcp add daiv --transport http https://daiv.example.com/mcp/
```

On first use, Claude Code will open a browser window for you to log in and authorize access. After that, you can use DAIV tools directly from Claude Code.

## Rate limiting

The `/oauth/register/` endpoint is unauthenticated by design (RFC 7591 — clients need it to obtain credentials before they can authenticate). To prevent abuse, **rate limiting should be configured at the reverse proxy layer**.

See the [Reverse Proxy configuration](../getting-started/deployment.md#rate-limiting) for the recommended Nginx setup.

## Security considerations

- **HTTPS required** — always run the MCP endpoint behind a TLS-terminating reverse proxy
- **PKCE enforced** — all OAuth flows require PKCE (`S256` challenge method) to prevent authorization code interception
- **Scope-limited tokens** — access tokens are scoped to `mcp` and cannot access other parts of the application
- **Rate limit registration** — the unauthenticated `/oauth/register/` endpoint should be rate-limited at the reverse proxy to prevent resource exhaustion
