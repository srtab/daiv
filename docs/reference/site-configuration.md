---
description: "The admin-only DAIV Site Configuration UI: tune agent models, tools, LLM providers and OAuth login at runtime, with environment variables taking precedence."
---

# Site configuration

Site Configuration is the admin-only page where you change DAIV's runtime behavior without redeploying. It groups settings by area and stores them in the database. You pick agent models, turn agent tools on or off, register LLM providers and switch OAuth login on or off, unless an environment variable or Docker secret overrides the field.

## Open the configuration page

You need an account with the **admin** role. Members do not see Site Configuration, and a member who opens its address is denied access.

1. Sign in as an admin.
2. Open `/dashboard/configuration/`. DAIV redirects you to the first group, **Agent**, at `/dashboard/configuration/agent/`.
3. Select a group. Each group is a page at `/dashboard/configuration/<group_key>/`, and an unknown key returns a 404.
4. Change the fields, then select **Save configuration**. DAIV shows "Configuration saved."

Saving a group writes only that group's fields. A saved change takes effect immediately for the next read: the save commits in a transaction and clears the read cache when it commits. An agent run reads its settings once, when it starts, so a change applies from the next run. The read cache keeps a value for 5 minutes, which matters only for changes made outside this page, such as a direct database edit: they can take that long to appear.

## Setting groups

The configuration has the following groups, organized by category.

| Group | Category | What it configures |
|-------|----------|--------------------|
| **Agent** | AI tasks | Primary, fallback and `daiv-max` models; their thinking levels; the agent recursion limit; the per-request LLM timeout and retry count (applied to every provider and model-backed task); whether to suggest a context file (e.g. `AGENTS.md`) on new merge requests; whether to link sessions from merge requests; and **Subagent models**, the model the general-purpose and explore subagents run on in every repository (see [Subagents](../features/subagents.md#subagent-model-precedence)). An entry already stored for a skill or subagent that only repositories define is listed there too, so it can be changed or cleared. A skill's model, and those of the code-review detectors, are set on the skill's page in **Skills** (see [Agent skills](../customization/agent-skills.md#run-a-skill-on-another-model)). |
| **Commit & PR Writer** | AI tasks | Primary and fallback models that generate commit messages and pull/merge request descriptions from diffs. |
| **Titling** | AI tasks | Primary and fallback models that generate session and run titles from prompts. |
| **Memory** | AI tasks | Capture learnings from finished runs, consolidate them into a per-repository memory document, and inject them into future runs. The models that extract observations from each finished run's transcript and rewrite the memory document, the consolidation thresholds (minimum pending observations, maximum pending age, minimum interval), and the caps on the memory document's lines and bytes. |
| **Run classifier** | AI tasks | Primary and fallback models that classify each finished scheduled run's report into an envelope. |
| **Providers** | Models | LLM provider records (slug, wire protocol, base URL, API key, extra headers and more) that every model field draws from. See [Providers](#providers). |
| **Web Search** | Agent tools | Turn the web search tool on or off, pick DuckDuckGo, Tavily or SearXNG, set the maximum results, store the Tavily API key, and set the SearXNG search URL and timeout. See [Set up SearXNG](#set-up-searxng). |
| **Web Fetch** | Agent tools | Turn the web fetch tool on or off, choose the model that processes fetched pages, set the cache TTL, timeout and maximum content size, and define per-domain auth headers. |
| **Sandbox** | Runtime | Sandbox request timeout and the sandbox API key. |
| **Jobs** | Runtime | Per-user rate limit for [Jobs API](../features/jobs-api.md) submissions (e.g. `20/hour`). |
| **Pipeline Watch** | Runtime | Watch CI on merge requests DAIV publishes and try to fix failures, and how many fix attempts (at least 1) to spend on one merge request before handing back. |
| **Rocket Chat** | Integrations | Offer Rocket.Chat as a notification channel, with the instance URL, bot user ID and auth token. |
| **Discord** | Integrations | Offer Discord as a notification channel, with the bot token from the Discord Developer Portal. See [Set up Discord](#set-up-discord). |
| **Telegram** | Integrations | Offer Telegram as a notification channel, with the bot token from @BotFather. See [Set up Telegram](#set-up-telegram). |
| **Authentication** | Integrations | OAuth login with your Git platform: the login toggle, open signup, the OAuth client ID and secret and, for GitLab, the instance URLs. See [Turn on OAuth login](#turn-on-oauth-login). |

## How environment variables and the UI interact

Each configurable field resolves through three tiers, highest first:

1. **Environment variable or Docker secret.** When set, the matching field is **locked**: the page shows a "Locked by environment variable" tooltip and you cannot edit the field.
2. **Database value.** The value you save here, when it is not empty.
3. **Built-in default.** The value DAIV ships with.

A value you save here takes effect only if no environment variable overrides it. If you leave a field blank, DAIV falls back to the environment variable, if any, and then to the built-in default. To keep a field editable, leave its variable unset rather than set to an empty value: an empty variable still locks the field.

The environment variable for a field is `DAIV_<FIELD_NAME>`; for example, `agent_recursion_limit` maps to `DAIV_AGENT_RECURSION_LIMIT`. The **Authentication** fields are the exception and map to the `ALLAUTH_*` variables:

| Field | Environment variable |
|-------|----------------------|
| OAuth client ID | `ALLAUTH_CLIENT_ID` |
| Auth Client Secret | `ALLAUTH_CLIENT_SECRET` |
| GitLab URL | `ALLAUTH_GITLAB_URL` |
| GitLab server URL | `ALLAUTH_GITLAB_SERVER_URL` |

[Environment variables](env-variables.md) lists every variable with its default, and marks the ones that are env-only.

## Secrets

DAIV stores the secret fields on this page encrypted in the database: the **Web Search API key**, **Sandbox API key**, **Auth Client Secret**, **Rocketchat Auth Token**, **Discord Bot Token**, **Telegram Bot Token**, each provider's **API key** and the per-domain web fetch header values. [External URL and encryption key](env-variables.md#external-url-and-encryption-key) lists everything DAIV encrypts. The page never shows a stored secret again: it shows a masked hint. Leave a secret field blank to keep the stored value, or select **Clear** to remove it. The key comes from `DAIV_ENCRYPTION_KEY`.

## Providers

The **Providers** group holds the LLM providers DAIV can call. Every model field in the other groups draws its models from the providers you enable here, and a model is written as `<slug>:<model-name>`; [LLM providers](../getting-started/llm-providers.md) explains how a model spec picks its provider.

DAIV creates four built-in providers the first time it runs: `anthropic`, `openai`, `google_genai` and `openrouter`, listed under **Built-in**. Their slug and provider type are locked, and you cannot delete them, but you can set their API keys and base URLs and turn them on or off. A built-in provider whose key was not seeded starts disabled; see [the LLM provider variables](env-variables.md#llm-providers) for how the keys are seeded. You add any other provider under **Custom**, for example a vLLM, llama.cpp or LiteLLM server or an internal gateway.

### Provider fields

| Field | Description |
|-------|-------------|
| **Slug** | The prefix of `<slug>:<model-name>`. It starts with a lowercase letter, holds lowercase letters, digits, `-` and `_`, and has at most 32 characters. `google` is reserved. It cannot change after creation: delete the provider and add it again to change it. |
| **Display name** | The name shown in the list. It has at most 64 characters. |
| **Provider type** | The wire protocol: OpenAI, Anthropic, Google Gemini or OpenRouter. |
| **Base URL** | The endpoint to call instead of the provider's default, with an `http` or `https` scheme. For an OpenAI-typed server, include the version segment, for example `/v1`: DAIV warns when it is missing, because requests would reach the wrong route. |
| **API key** | The credential for the endpoint, stored encrypted. |
| **Extra headers (JSON)** | A JSON object of additional request headers, for example `{"X-Foo": "bar"}`. Header values must be strings. Headers the agent manages itself take precedence. |
| **Enabled** | Whether models can use the provider. Enabling a provider needs an API key. |
| **Use Responses API** | Applies to OpenAI-typed providers only. Turn it on for servers that expose `/v1/responses`, and leave it off for servers that offer only `/v1/chat/completions`, such as most vLLM, llama.cpp and LiteLLM setups. |
| **Verify TLS certificates** | On by default. Applies to OpenAI- and OpenRouter-typed providers only: for the others, DAIV warns that turning it off has no effect. |

A model field anywhere in the configuration is rejected on save when its provider is disabled or has no API key, with a message that points you to the **Providers** group.

### Add a custom provider

Add a provider to use models from a server other than the four built-in providers. You need the admin role, the endpoint's URL and its API key.

1. In **Configuration** → **Providers**, select **Add custom provider**.
2. In **Slug**, enter the model prefix.
3. In **Display name**, enter the name to show in the list.
4. In **Provider type**, choose the wire protocol the server speaks.
5. In **Base URL**, enter the endpoint's URL.
6. In **API key**, enter the key.
7. Select **Save configuration**.
8. Use `<slug>:<model-name>` wherever a model is specified, with `<slug>` replaced by the slug and `<model-name>` by a model the endpoint serves.

!!! warning
    Turn off **Verify TLS certificates** only for self-hosted endpoints behind an internal or self-signed CA. Without verification, the connection is open to man-in-the-middle attacks. Mount the CA certificate into the DAIV containers instead when you can.

## Set up SearXNG

!!! warning
    SearXNG forwards queries to its upstream search engines. Self-hosting does not keep the query text local, and a query can contain repository code or other private information.

By default, web search uses DuckDuckGo. To send the agent's searches to a SearXNG instance instead, you need:

- A SearXNG instance that the DAIV app and workers can reach without authentication, and that allows JSON responses: `json` is in `search.formats` of its settings, for example `[html, json]`.
- The complete search endpoint, for example `https://search.example.com/search`, or `https://search.example.com/searxng/search` when SearXNG runs under a prefix. It must use `http` or `https` and hold no credentials, query string or fragment.

1. In **Configuration** → **Web Search**, set **Web search engine** to **SearXNG**.
2. Enter the endpoint in **SearXNG search URL**.
3. Optionally, set **SearXNG request timeout**, from 1 to 120 seconds. The default is 15 seconds.
4. Select **Save configuration**.

Leave the Tavily API key as it is: SearXNG does not use it. The existing maximum results and web search switch apply to SearXNG too. To configure it from the environment instead, set `DAIV_WEB_SEARCH_ENGINE`, `DAIV_WEB_SEARCH_SEARXNG_URL` and `DAIV_WEB_SEARCH_SEARXNG_TIMEOUT_SECONDS`; see [Web search](env-variables.md#web-search).

DAIV sends a `GET` request with `q` and `format=json` and does not follow redirects, so enter the final endpoint. Chat, GitLab and GitHub runs and other agent entry points share the web search tool, and the model decides when to call it. DAIV itself makes the request, so it does not need an opening in a sandbox environment's egress policy. DAIV keeps HTTPS certificate verification on. A failed search reaches the agent as a tool failure, not as an empty result.

### Allow DAIV through the SearXNG limiter

DAIV sends `Accept-Language: en`, which SearXNG's header checks expect. If the SearXNG limiter (`server.limiter`) is on, it can still answer automated requests with HTTP 429. To exempt DAIV, add its source address to `/etc/searxng/limiter.toml`:

```toml
[botdetection.ip_lists]
pass_ip = ["192.0.2.10"]
```

Replace `192.0.2.10` with the address SearXNG sees for the DAIV app and workers, or with a narrow trusted subnet. The list exempts those clients from bot detection, so keep it narrow rather than turning the limiter off. For pass lists and reverse-proxy client addresses, see the [SearXNG limiter documentation](https://docs.searxng.org/admin/searx.limiter). For the JSON format setting and the endpoint contract, see the [SearXNG Search API](https://docs.searxng.org/dev/search_api.html).

## Turn on OAuth login

OAuth login lets users sign in with their Git platform account. You need an OAuth app on that platform: see [Create the OAuth app](../getting-started/accounts.md#create-the-oauth-app).

1. In **Configuration**, open **Authentication**.
2. Turn on the toggle next to the group title. It is the **Enable OAuth login** setting, and DAIV hides the group's other fields while it is off.
3. Enter the **OAuth client ID** and the **Auth Client Secret**. Enter both: DAIV rejects a save that sets only one.
4. On GitLab, set **GitLab URL** to the address your users' browsers open. If DAIV reaches GitLab at a different address, such as one inside a Docker network, set **GitLab server URL** to it. GitHub installs do not show these two fields.
5. Optionally, turn on **Open social signup** to let anyone who authenticates through the Git platform create an account. When it is off, only users an admin created can sign in.
6. Select **Save configuration**.

The `CODEBASE_CLIENT` setting decides which fields the group shows and which provider DAIV offers. DAIV creates no accounts through standard email signup, which is off.

## Set up Discord

You need a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). The bot must share a server with each user who connects it: Discord does not let a bot open a direct message with a user who shares no server with it.

1. In the Discord Developer Portal, open your application and select **Bot**.
2. Copy the bot token, then in **Configuration** → **Discord**, turn on the toggle next to the group title. It is the **Enable Discord** setting.
3. Enter the token in **Discord Bot Token**.
4. Select **Save configuration**.

DAIV stores the token encrypted. Users connect their Discord user ID from the notification channels page; see [Connect Discord](../features/notifications.md#connect-discord). Add the bot to a server that those users belong to before they connect, or direct-message delivery fails.

## Set up Telegram

You need a bot token from [@BotFather](https://t.me/botfather), and a `DAIV_EXTERNAL_URL` that Telegram can reach.

!!! warning
    Give each DAIV instance its own bot. Telegram allows one webhook per bot, so if a second instance uses the same token, the webhook moves to it and the first instance stops receiving Telegram's updates, including the connect handshake. DAIV's reconcile job logs the URL it finds but cannot prevent this.

1. In **Configuration** → **Telegram**, turn on the toggle next to the group title. It is the **Enable Telegram** setting, and DAIV hides the group's other fields while it is off.
2. Enter the token in **Telegram Bot Token**.
3. Select **Save configuration**.

DAIV checks a typed token with Telegram when you save. It rejects a token Telegram refuses, and saves a token it could not check because Telegram was unreachable, with a warning, so an outage does not block an unrelated edit. The group then shows the bot username. Users connect their account from the notification channels page; see [Notifications](../features/notifications.md).

## Telegram derived values

You type only the bot token. DAIV derives the rest:

- **Bot username.** DAIV reads it from Telegram with `getMe` when you save the group, and again in a reconcile job that runs every 15 minutes. The page shows it read-only, because a hand-typed username that disagrees with the token breaks the connect link without any visible error. An empty value means the derivation has not run yet.
- **Webhook secret.** DAIV generates it, stores it encrypted and sends it to Telegram when it registers the webhook. You never see it or type it. The webhook rejects every update until the secret exists, so a blank value means the sync has not finished.

Saving is not the only trigger: the reconcile job compares the webhook Telegram has registered with the desired URL and registers it again when they differ. That brings an instance configured only through `DAIV_TELEGRAM_BOT_TOKEN` and `DAIV_TELEGRAM_ENABLED`, which may never open this page, into line, and it recovers a webhook that Telegram disabled after repeated failures.

Turning Telegram off keeps the stored webhook secret. If you turn it off with no bot token available, because you cleared it or never set it while a webhook was registered, DAIV cannot remove the webhook and shows a warning: remove it in BotFather, or restore the token and turn the channel off again. A group that never had a webhook registered saves without a warning.

## Related pages

<div class="grid cards" markdown>

-   **Environment variables**

    ---

    Every supported variable, its default, and which settings the UI manages.

    [:octicons-arrow-right-24: Environment variables](env-variables.md)

-   **LLM providers**

    ---

    How model prefixes resolve and how to mix providers.

    [:octicons-arrow-right-24: LLM providers](../getting-started/llm-providers.md)

-   **Deployment**

    ---

    Run DAIV with Docker, including the required secrets.

    [:octicons-arrow-right-24: Deployment](../getting-started/deployment.md)

</div>
