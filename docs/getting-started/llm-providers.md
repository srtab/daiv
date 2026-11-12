---
description: "Configure the LLM providers DAIV runs on (OpenRouter, OpenAI, Anthropic, Google Gemini or a custom provider) and how models are specified."
---

# LLM providers

DAIV runs on these LLM providers:

- [OpenRouter](https://openrouter.ai) (default)
- [OpenAI](https://openai.com)
- [Anthropic](https://anthropic.com)
- [Google Gemini](https://gemini.google.com)

You can mix providers, for example OpenRouter for the main agent and a direct provider for one model override. An admin can add any other OpenAI-compatible endpoint as a [custom provider](#custom-providers).

## Provider API keys

DAIV reads a provider's API key once, at its first start, from the provider's environment variable or from a Docker secret named after the variable in lowercase, such as `openrouter_api_key`. From then on you change the keys, and the providers' other settings, under **Configuration → Providers** in the dashboard. A change to the variable or the secret after the first start has no effect.

[LLM providers](../reference/env-variables.md#llm-providers) in the environment variables reference lists the variables. [Providers](../reference/site-configuration.md#providers) in the site configuration reference describes the fields of a provider.

## Set up a provider

Set up a provider before DAIV's first start. You need an API key from the provider, and the default models need only OpenRouter's key.

| Provider | Get an API key | Variable |
|----------|----------------|----------|
| OpenRouter | [OpenRouter Settings](https://openrouter.ai/settings/keys) | `OPENROUTER_API_KEY` |
| OpenAI | [OpenAI](https://platform.openai.com/api-keys) | `OPENAI_API_KEY` |
| Anthropic | [Anthropic](https://console.anthropic.com/settings/keys) | `ANTHROPIC_API_KEY` |
| Google Gemini | [AI Studio](https://aistudio.google.com/apikey) | `GOOGLE_API_KEY` |

1. Get an API key from the provider's page in the table.
2. In DAIV's `.env` file, set the provider's variable to the key. Alternatively, mount a Docker secret named after the variable in lowercase.
3. Start DAIV.

A provider whose key was not set at the first start begins disabled. To set up a provider on a running install, open **Configuration → Providers**, set the provider's **API key**, turn on **Enabled**, and select **Save configuration**. Saving a key does not enable a disabled provider. [Providers](../reference/site-configuration.md#providers) describes the fields.

## Model specs

A model spec names a model and its provider. OpenRouter models take the `openrouter:` prefix. Direct providers' models take no prefix, and DAIV picks the provider from the start of the name:

| Provider | Model spec | Example |
|----------|------------|---------|
| OpenRouter | `openrouter:` followed by the [model name from OpenRouter](https://openrouter.ai/models) | `openrouter:anthropic/claude-sonnet-5.5` |
| Anthropic (direct) | A name starting with `claude`, from the [model names Anthropic lists](https://docs.anthropic.com/en/docs/about-claude/models/all-models#model-names) | `claude-sonnet-5-5` |
| OpenAI (direct) | A name starting with `gpt-4`, `gpt-5`, `gpt-6` or `o4`, from the [model names OpenAI lists](https://platform.openai.com/docs/models) | `gpt-6.1-sol` |
| Google Gemini (direct) | A name starting with `gemini`, from the [model names Gemini lists](https://ai.google.dev/gemini-api/docs/models) | `gemini-3.8-flash` |

A provider's slug also works as a prefix, so `anthropic:claude-sonnet-5-5` is the same model as `claude-sonnet-5-5`. The slug of Google Gemini is `google_genai`, and DAIV also accepts `google:`.

To use a model spec for one repository, set it in `.daiv.yml`; [Model overrides](../customization/repository-config.md#model-overrides) lists every option. For example:

```yaml
models:
  agent:
    model: "claude-sonnet-5-5"
```

In the dashboard, the agent picker offers a model dropdown built from each enabled provider's API and cached for 15 minutes, so you can choose a model without typing its spec. Free-text entry stays available as a fallback.

## Default models

| Role | Model | Fallback |
|------|-------|----------|
| Main agent | `openrouter:anthropic/claude-sonnet-5.5` | `openrouter:openai/gpt-6.1-sol` |
| Max mode | `openrouter:anthropic/claude-opus-5.5` | The main agent's models |
| Explore subagent | `openrouter:anthropic/claude-haiku-5.5` | The main agent's models |

Smaller tasks such as titling, merge request metadata and memory have their own defaults, listed in [Environment variables](../reference/env-variables.md). All defaults route through OpenRouter, so only `OPENROUTER_API_KEY` is required for a basic setup. Choose the explore subagent's model with an `explore` entry in the subagent models: see [Subagents](../features/subagents.md#subagent-model-precedence).

## Retries on provider errors

DAIV retries a failed call to a provider's API when the failure is transient. For Anthropic's API, that means a connection error, or a 408, 409, 429 or 5xx response. DAIV retries up to `DAIV_MODEL_MAX_RETRIES` times (default `2`; see [Model requests](../reference/env-variables.md#model-requests)). A 400 or 401 response is not retried. If the call still fails, DAIV tries the fallback model, when one is set. If that fails too, the run ends with the error.

If runs end on rate-limit errors, raise `DAIV_MODEL_MAX_RETRIES` or ask Anthropic for a higher rate limit.

## Custom providers

An admin can register providers beyond the four above, mainly self-hosted, OpenAI-compatible endpoints such as vLLM, llama.cpp, LiteLLM or an internal gateway. A custom provider works with any of the supported wire protocols.

You need the admin role to add a custom provider. Under **Configuration → Providers**, the steps in [Add a custom provider](../reference/site-configuration.md#add-a-custom-provider) create the provider with a slug, a provider type, a base URL, an API key and a few options. [Provider fields](../reference/site-configuration.md#provider-fields) describes them.

Once the provider exists, reference its models with the slug as a prefix:

```text
<slug>:<model-name>
```

Replace `<slug>` with the provider's slug and `<model-name>` with a model the endpoint serves. You can use this prefix anywhere a model is specified, including the `.daiv.yml` [model overrides](../customization/repository-config.md#model-overrides).

### Keep prompt caching on a self-hosted model

The agent loads most of its tools on demand. On models listed in `DEFERRED_TOOLS_FROZEN_TOOLS_MODELS`, loading a tool keeps the provider's prompt cache. On any other model, the first use of each tool re-sends the conversation uncached.

Before you add a self-hosted model to that list, check that it can call a tool it only saw in a `tool_search` result: run `tests/integration_tests/test_deferred_tools_frozen.py` against it, using the command in that file's docstring. List the model name without your provider slug, for example `qwen3-coder` for `vllm:qwen3-coder`. [Environment variables](../reference/env-variables.md) describes the setting.
