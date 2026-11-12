---
description: "Configure the LLM providers DAIV runs on (OpenRouter, OpenAI, Anthropic, Google Gemini or a custom provider) and how models are specified."
---

# LLM Providers

DAIV supports the following LLM providers:

- [OpenRouter](https://openrouter.ai) (**default**)
- [OpenAI](https://openai.com)
- [Anthropic](https://anthropic.com)
- [Google Gemini](https://gemini.google.com)

You can mix providers — for example, use OpenRouter for the main agent and a direct provider for a specific model override.

## How models are specified

Models use a **prefix system** to identify the provider:

| Prefix | Provider | Example |
|--------|----------|---------|
| `openrouter:` | OpenRouter | `openrouter:anthropic/claude-sonnet-5.5` |
| `claude` | Anthropic (direct) | `claude-sonnet-5-5` |
| `gpt-4`, `gpt-5`, `gpt-6`, `o4` | OpenAI (direct) | `gpt-6.1-sol` |
| `gemini` | Google Gemini (direct) | `gemini-3.8-flash` |

DAIV resolves the provider automatically from the model name. No extra configuration is needed beyond setting the API key for the provider you want to use, before DAIV's first start, as an environment variable or as a Docker secret named after it in lowercase (`openrouter_api_key`): DAIV copies the keys into its database then, and from there on you change them under **Configuration → Providers** in the dashboard, not in the environment (see [LLM providers](../reference/env-variables.md#llm-providers)).

!!! tip
    In the dashboard, the agent picker offers a live model dropdown built from each enabled provider's API (cached for 15 minutes), so you can choose a model without memorizing its full spec. Free-text entry remains available as a fallback.

### Default models

| Role | Model | Fallback |
|------|-------|----------|
| Main agent | Claude Sonnet 5.5 | GPT-6.1 Sol |
| Max mode | Claude Opus 5.5 | The main agent's models |
| Explore subagent | Claude Haiku 5.5 | The main agent's models |

Smaller tasks such as titling, merge request metadata and memory have their own defaults, listed in [Environment Variables](../reference/env-variables.md). All defaults route through OpenRouter, so only `OPENROUTER_API_KEY` is required for a basic setup. Choose the explore subagent's model with an `explore` entry in the subagent models (see [Subagents](../features/subagents.md#choosing-a-subagents-model)). Override models per repository in `.daiv.yml` — see [Repository Config](../customization/repository-config.md#model-overrides).

---

## OpenRouter

OpenRouter is the **default provider** for DAIV. It provides access to models from multiple vendors with built-in fallback support.

**Setup:**

1. Obtain an API key from [OpenRouter Settings](https://openrouter.ai/settings/keys)
2. Set the environment variable:
   ```bash
   OPENROUTER_API_KEY=<api-key>
   ```

   Replace `<api-key>` with the key from the previous step.

**Model format:**

Use the `openrouter:` prefix followed by the [model name from OpenRouter](https://openrouter.ai/models):

```text
openrouter:anthropic/claude-sonnet-5.5
openrouter:openai/gpt-6.1-sol
```

**Per-repository override** (`.daiv.yml`):

```yaml
models:
  agent:
    model: "openrouter:anthropic/claude-sonnet-5.5"
```

---

## OpenAI

**Setup:**

1. Obtain an API key from [OpenAI](https://platform.openai.com/api-keys)
2. Set the environment variable:
   ```bash
   OPENAI_API_KEY=<api-key>
   ```

   Replace `<api-key>` with the key from the previous step.

**Model format:**

Use the [model name from OpenAI](https://platform.openai.com/docs/models) directly (no prefix needed):

```text
gpt-6.1-sol
gpt-6-luna
```

**Per-repository override** (`.daiv.yml`):

```yaml
models:
  agent:
    model: "gpt-6.1-sol"
```

---

## Anthropic

**Setup:**

1. Obtain an API key from [Anthropic](https://console.anthropic.com/settings/keys)
2. Set the environment variable:
   ```bash
   ANTHROPIC_API_KEY=<api-key>
   ```

   Replace `<api-key>` with the key from the previous step.

**Model format:**

Use the [model name from Anthropic](https://docs.anthropic.com/en/docs/about-claude/models/all-models#model-names) directly (no prefix needed):

```text
claude-sonnet-5-5
claude-opus-5-5
```

**Per-repository override** (`.daiv.yml`):

```yaml
models:
  agent:
    model: "claude-sonnet-5-5"
```

!!! warning
    When Anthropic's API returns an error or a rate-limit response, DAIV retries the call up to `DAIV_MODEL_MAX_RETRIES` times (default `2`) and then fails the run. If runs fail this way, raise `DAIV_MODEL_MAX_RETRIES` or ask Anthropic for a higher rate limit.

---

## Google Gemini

**Setup:**

1. Obtain an API key from [AI Studio](https://aistudio.google.com/apikey)
2. Set the environment variable:
   ```bash
   GOOGLE_API_KEY=<api-key>
   ```

   Replace `<api-key>` with the key from the previous step.

**Model format:**

Use the [model name from Gemini](https://ai.google.dev/gemini-api/docs/models) directly (no prefix needed):

```text
gemini-3.8-flash
```

**Per-repository override** (`.daiv.yml`):

```yaml
models:
  agent:
    model: "gemini-3.8-flash"
```

---

## Custom providers

Beyond the four providers above, admins can register additional providers in [Site Configuration](../reference/site-configuration.md#providers). This is mainly for self-hosted, OpenAI-compatible endpoints — vLLM, llama.cpp, LiteLLM, or an internal gateway — but applies to any of the supported wire protocols.

Each provider has a slug, a provider type, a base URL, an API key and a few options; [Provider fields](../reference/site-configuration.md#provider-fields) describes them, and [Add a custom provider](../reference/site-configuration.md#add-a-custom-provider) gives the steps.

Once the row exists, reference its models using the slug as a prefix:

```text
<slug>:<model-name>
```

Replace `<slug>` with the provider's slug and `<model-name>` with a model the endpoint serves.

You can use this prefix anywhere a model is specified, including the `.daiv.yml` [model overrides](../customization/repository-config.md#model-overrides).

!!! tip "Prompt caching with on-demand tools"
    The agent loads most of its tools on demand. On models listed in `DEFERRED_TOOLS_FROZEN_TOOLS_MODELS`, loading a tool keeps the provider's prompt cache; on any other model, the first use of each tool re-sends the conversation uncached. Before adding a self-hosted model to that list, check that it can call a tool it only saw in a `tool_search` result: run `tests/integration_tests/test_deferred_tools_frozen.py` against it (the command is in that file's docstring). List the model name without your provider slug, e.g. `qwen3-coder` for `vllm:qwen3-coder`.
