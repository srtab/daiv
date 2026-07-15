# Site Configuration

Site Configuration is the **admin-only** web UI for tuning DAIV's runtime behavior without redeploying. Settings are grouped by area, stored in the database, and **override the matching environment-variable defaults** — so you can pick agent models, toggle agent tools, register LLM providers, and switch OAuth login on or off, all from the dashboard.

It lives at `/dashboard/configuration/` and is reachable only by users with the **admin** role. Members do not see it.

!!! info "Admin only"
    Both the index and the per-group pages are guarded by `AdminRequiredMixin`. A signed-in member who navigates to `/dashboard/configuration/` is denied access.

## Where to find it

Open `/dashboard/configuration/`. The index immediately redirects you to the first group, **Agent**, at `/dashboard/configuration/agent/`. Each group is a separate page reached at `/dashboard/configuration/<group_key>/`; an unknown key returns a 404.

Saving a group writes only that group's fields, then shows a "Configuration saved." banner. A change you make here takes effect immediately: the save commits inside a transaction and invalidates the read cache on commit, so the next read sees the new value. The 5-minute read-cache TTL only matters for changes made outside this UI (for example, a direct database edit), which can take up to that long to propagate.

## Setting groups

The configuration is split into the following groups, organized by category.

| Group | Category | What it configures |
|-------|----------|--------------------|
| **Agent** | AI tasks | Primary, fallback, `daiv-max`, and explore models; their thinking levels; the agent recursion limit; the per-request LLM timeout and max retries (applied to every provider and model-backed task); and whether to suggest a context file (e.g. `AGENTS.md`) on new merge requests. |
| **Commit & PR Writer** | AI tasks | Primary and fallback models that generate commit messages and pull/merge request descriptions from diffs. |
| **Titling** | AI tasks | Primary and fallback models that generate session and run titles from prompts. |
| **Providers** | Models | LLM provider records — slug, wire protocol, base URL, API key, and extra headers — that every model field draws from. |
| **Web Search** | Agent tools | Enable/disable the web search tool, pick the engine (DuckDuckGo or Tavily), set max results, and store the Tavily API key. |
| **Web Fetch** | Agent tools | Enable/disable the web fetch tool, choose the model that processes fetched pages, set cache TTL / timeout / max content size, and define per-domain auth headers. |
| **Sandbox** | Runtime | Sandbox request timeout and the sandbox API key. |
| **Jobs** | Runtime | Per-user rate limit for [Jobs API](../features/jobs-api.md) submissions (e.g. `20/hour`). |
| **Rocket Chat** | Integrations | Enable Rocket Chat as a notification channel, with the instance URL, bot user ID, and auth token. |
| **Authentication** | Integrations | OAuth login with your Git platform — toggle login, control open signup, and store the OAuth client ID/secret and (for GitLab) instance URLs. |

!!! tip "Model pickers and providers"
    The model fields in **Agent**, **Commit & PR Writer**, **Titling**, and **Web Fetch** only offer models from providers you configure under **Providers**. Selecting a model whose provider is disabled or has no API key is rejected on save with a message pointing you to the Providers section.

## Relationship to environment variables

Site Configuration shares a value space with DAIV's environment variables. Each configurable field resolves through a three-tier priority chain (highest to lowest):

1. **Environment variable or Docker secret** — a hard override. When set, the matching UI field is shown **locked** with a "Locked by environment variable" tooltip and cannot be edited from the dashboard.
2. **Database value** — the value you set through this UI (when non-empty).
3. **Built-in default** — the hardcoded fallback DAIV ships with.

So a value you save here is stored in the database and takes effect **only if no environment variable overrides it**; if you leave a field blank, DAIV falls back to the environment variable (if any) or the built-in default.

The environment-variable name for a field follows the `DAIV_<FIELD_NAME>` convention (for example, `agent_recursion_limit` maps to `DAIV_AGENT_RECURSION_LIMIT`). The **Authentication** fields are the exception — they map to the `ALLAUTH_*` variables:

| Field | Environment variable |
|-------|----------------------|
| OAuth client ID | `ALLAUTH_CLIENT_ID` |
| OAuth client secret | `ALLAUTH_CLIENT_SECRET` |
| GitLab URL | `ALLAUTH_GITLAB_URL` |
| GitLab server URL | `ALLAUTH_GITLAB_SERVER_URL` |

For the full list of variables, their defaults, and which settings are env-only versus UI-manageable, see [Environment Variables](env-variables.md).

!!! note "Secrets are encrypted at rest"
    API keys, the OAuth client secret, the sandbox key, the Rocket Chat token, and per-domain web-fetch header values are stored Fernet-encrypted in the database. The UI never re-displays a stored secret — it shows a masked hint instead. Leaving a secret field blank keeps the existing value; use the field's **Clear** action to remove it. Encryption depends on `DAIV_ENCRYPTION_KEY`, so do not rotate that key without re-entering your secrets.

## Providers and API keys

The **Providers** group manages the LLM endpoints DAIV can call. Four provider rows (OpenAI, Anthropic, Google Gemini, and OpenRouter) are seeded on first run and **locked** — their slug and provider type cannot change and they cannot be deleted, though you still supply their API keys, base URLs, and toggle them on or off. You can add custom rows for OpenAI-compatible gateways (vLLM, LiteLLM, hosted proxies, and so on).

Each row carries:

- **Slug** — used as the `slug:model_name` prefix when selecting a model (immutable after creation).
- **Provider type** — the wire protocol: OpenAI, Anthropic, Google Gemini, or OpenRouter.
- **Base URL** and optional **extra headers** (a JSON object).
- **API key** (encrypted at rest).
- **Use Responses API** — only honored for OpenAI-typed providers; enable for servers exposing `/v1/responses`, disable for `/v1/chat/completions`-only servers.
- **Verify TLS certificates** — disable only for self-hosted endpoints behind an internal CA.

!!! warning "Enabled providers need a key"
    Enabling a provider row requires an API key. A model field anywhere in the configuration will fail validation if it points at a provider that is disabled or unkeyed.

For how model prefixes resolve and how to mix providers, see [LLM Providers](../getting-started/llm-providers.md).

## Toggling OAuth login

The **Authentication** group controls whether users can sign in with their Git platform account. The fields shown adapt to your configured Git platform (`CODEBASE_CLIENT`): GitLab installs see the GitLab URL fields; GitHub installs do not.

- **Enable OAuth login** — the group's toggle; lets users sign in via the configured Git platform (GitHub or GitLab).
- **Open social signup** — when enabled, anyone who authenticates via the Git platform can create an account; when disabled, only users an admin pre-created may sign in.
- **OAuth client ID** and **client secret** — must be configured as a pair; saving one without the other is rejected.
- **GitLab URL** / **GitLab server URL** — the browser-facing instance URL and the optional server-to-server URL for API calls in Docker-internal networks.

!!! note
    Standard email signup is disabled in DAIV; account creation flows through the configured Git platform's OAuth.

## Related pages

<div class="grid cards" markdown>

-   **Environment Variables**

    ---

    Every supported variable, its default, and which settings are UI-manageable.

    [:octicons-arrow-right-24: Environment Variables](env-variables.md)

-   **LLM Providers**

    ---

    How model prefixes resolve and how to mix providers.

    [:octicons-arrow-right-24: LLM Providers](../getting-started/llm-providers.md)

-   **Deployment**

    ---

    Run DAIV with Docker, including the required secrets.

    [:octicons-arrow-right-24: Deployment](../getting-started/deployment.md)

</div>
