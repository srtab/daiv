# LLM Providers

DAIV supports the following LLM providers:

- [OpenRouter](https://openrouter.ai) (**default**)
- [OpenAI](https://openai.com)
- [Anthropic](https://anthropic.com)
- [Google Gemini](https://gemini.google.com)

You can mix providers — for example, use OpenRouter for the main agent and a direct provider for a specific model override.

## How models are specified

Models use a **prefix system** to identify the provider:

| Prefix                 | Provider               | Example                                  |
| ---------------------- | ---------------------- | ---------------------------------------- |
| `openrouter:`          | OpenRouter             | `openrouter:anthropic/claude-sonnet-4.6` |
| `claude`               | Anthropic (direct)     | `claude-sonnet-4.6`                      |
| `gpt-4`, `gpt-5`, `o4` | OpenAI (direct)        | `gpt-5.3-codex`                          |
| `gemini`               | Google Gemini (direct) | `gemini-2.5-pro-preview-05-06`           |

DAIV resolves the provider automatically from the model name. No extra configuration is needed beyond setting the API key for the provider you want to use.

Tip

In the dashboard, the agent picker offers a live model dropdown built from each enabled provider's API (cached for 15 minutes), so you can choose a model without memorizing its full spec. Free-text entry remains available as a fallback.

### Default models

| Role             | Model             | Provider   |
| ---------------- | ----------------- | ---------- |
| Main agent       | Claude Sonnet 4.6 | OpenRouter |
| Max mode         | Claude Opus 4.6   | OpenRouter |
| Explore subagent | Claude Haiku 4.5  | OpenRouter |
| Fallback         | GPT 5.3 Codex     | OpenRouter |

All defaults route through OpenRouter, so only `OPENROUTER_API_KEY` is required for a basic setup. Override models per repository in `.daiv.yml` — see [Repository Config](https://srtab.github.io/daiv/dev/customization/repository-config/#model-overrides).

______________________________________________________________________

## OpenRouter

OpenRouter is the **default provider** for DAIV. It provides access to models from multiple vendors with built-in fallback support.

**Setup:**

1. Obtain an API key from [OpenRouter Settings](https://openrouter.ai/settings/keys)

1. Set the environment variable:

   ```
   OPENROUTER_API_KEY=your-api-key-here
   ```

**Model format:**

Use the `openrouter:` prefix followed by the [model name from OpenRouter](https://openrouter.ai/models):

```
openrouter:anthropic/claude-sonnet-4.6
openrouter:openai/gpt-5.3-codex
```

**Per-repository override** (`.daiv.yml`):

```
models:
  agent:
    model: "openrouter:anthropic/claude-sonnet-4.6"
```

______________________________________________________________________

## OpenAI

**Setup:**

1. Obtain an API key from [OpenAI](https://platform.openai.com/api-keys)

1. Set the environment variable:

   ```
   OPENAI_API_KEY=your-api-key-here
   ```

**Model format:**

Use the [model name from OpenAI](https://platform.openai.com/docs/models) directly (no prefix needed):

```
gpt-5.3-codex
o4-mini
```

**Per-repository override** (`.daiv.yml`):

```
models:
  agent:
    model: "gpt-5.3-codex"
```

______________________________________________________________________

## Anthropic

**Setup:**

1. Obtain an API key from [Anthropic](https://console.anthropic.com/settings/keys)

1. Set the environment variable:

   ```
   ANTHROPIC_API_KEY=your-api-key-here
   ```

**Model format:**

Use the [model name from Anthropic](https://docs.anthropic.com/en/docs/about-claude/models/all-models#model-names) directly (no prefix needed):

```
claude-sonnet-4.6
claude-opus-4.6
```

**Per-repository override** (`.daiv.yml`):

```
models:
  agent:
    model: "claude-sonnet-4.6"
```

Warning

We love Anthropic, but unfortunately their API is very unstable and often returns errors. Also, the rate limits could be exceeded very quickly.

______________________________________________________________________

## Google Gemini

**Setup:**

1. Obtain an API key from [AI Studio](https://aistudio.google.com/apikey)

1. Set the environment variable:

   ```
   GOOGLE_API_KEY=your-api-key-here
   ```

**Model format:**

Use the [model name from Gemini](https://ai.google.dev/gemini-api/docs/models) directly (no prefix needed):

```
gemini-2.5-pro-preview-05-06
gemini-2.4-flash-preview-04-17
```

**Per-repository override** (`.daiv.yml`):

```
models:
  agent:
    model: "gemini-2.5-pro-preview-05-06"
```

______________________________________________________________________

## Custom providers

Beyond the four providers above, admins can register additional providers from the **Configuration UI** at `/dashboard/configuration/` (under the providers section). This is mainly for self-hosted, OpenAI-compatible endpoints — vLLM, llama.cpp, LiteLLM, or an internal gateway — but applies to any of the supported wire protocols.

When you add a provider, you choose:

| Field                       | Purpose                                                                                                                                                                                                                                                             |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Slug**                    | A short identifier that becomes the model prefix (`your-slug:model_name`). Must start with a lowercase letter; lowercase letters, digits, `-`, `_`; max 32 chars. Immutable after creation.                                                                         |
| **Provider type**           | The wire protocol used to talk to the server — one of OpenAI, Anthropic, Google Gemini, or OpenRouter.                                                                                                                                                              |
| **Base URL**                | The endpoint to call instead of the provider's default (for example, your vLLM server). For OpenAI-typed servers, include the version segment (e.g. `/v1`).                                                                                                         |
| **API key**                 | The credential for the endpoint (encrypted at rest).                                                                                                                                                                                                                |
| **Extra headers**           | A JSON object of additional request headers (e.g. `{"X-Foo": "bar"}`). Agent-managed headers take precedence.                                                                                                                                                       |
| **Use Responses API**       | Only honored for OpenAI-typed providers. Enable for servers that expose `/v1/responses`; leave off for `/v1/chat/completions`-only servers (vLLM, llama.cpp, most LiteLLM setups).                                                                                  |
| **Verify TLS certificates** | Disable only for self-hosted endpoints behind an internal or self-signed CA. Skipping verification exposes the connection to MITM attacks — prefer mounting the CA into the DAIV containers when possible. Honored only for OpenAI- and OpenRouter-typed providers. |

Once the row exists, reference its models using the slug as a prefix:

```
your-slug:model_name
```

You can use this prefix anywhere a model is specified, including the `.daiv.yml` [model overrides](https://srtab.github.io/daiv/dev/customization/repository-config/#model-overrides).

Note

The four built-in providers (OpenRouter, OpenAI, Anthropic, Google Gemini) are locked: their slug and provider type cannot be changed and the rows cannot be deleted. Custom providers you add are fully editable and removable.

Admin only

Managing providers requires an admin account. See [Automation: LLM Providers](https://srtab.github.io/daiv/dev/reference/env-variables/#automation-llm-providers) for how the built-in providers map to environment variables.
