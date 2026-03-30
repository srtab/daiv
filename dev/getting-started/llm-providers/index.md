# LLM Providers

DAIV supports the following LLM providers:

- [OpenRouter](https://openrouter.ai) (**default**)
- [OpenAI](https://openai.com)
- [Anthropic](https://anthropic.com)
- [Google Gemini](https://gemini.google.com)

You can mix providers — for example, use OpenRouter for the main agent and a direct provider for a specific model override.

## How models are specified

Models use a **prefix system** to identify the provider:

| Prefix        | Provider               | Example                                  |
| ------------- | ---------------------- | ---------------------------------------- |
| `openrouter:` | OpenRouter             | `openrouter:anthropic/claude-sonnet-4.6` |
| `claude`      | Anthropic (direct)     | `claude-sonnet-4.6`                      |
| `gpt-`, `o4`  | OpenAI (direct)        | `gpt-5.3-codex`                          |
| `gemini`      | Google Gemini (direct) | `gemini-2.5-pro-preview-05-06`           |

DAIV resolves the provider automatically from the model name. No extra configuration is needed beyond setting the API key for the provider you want to use.

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
