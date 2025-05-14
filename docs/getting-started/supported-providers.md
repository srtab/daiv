# Supported Providers

DAIV currently supports integration with the following LLM providers:

- [OpenRouter](https://openrouter.ai)
- [OpenAI](https://openai.com)
- [Anthropic](https://anthropic.com)
- [Gemini](https://gemini.google.com)

A combination of providers may be configured. For example, you can use OpenAI provider for one agent and Gemini provider for another.

---

## OpenRouter

OpenRouter is the **default provider for DAIV** due to its fallback mechanism and wide range of models from multiple providers.

**Setup:**

1. Obtain an API key from [OpenRouter Settings](https://openrouter.ai/settings/keys).
2. Set the `OPENROUTER_API_KEY` environment variable:
   ```sh
   export OPENROUTER_API_KEY=your-api-key-here
   ```

**Usage:**

When declaring a model, use the model name [provided by OpenRouter](https://openrouter.ai/models), prefixed with `openrouter:`. For example:

```
openrouter:openai/gpt-4.1
openrouter:anthropic/claude-3-7-sonnet
```

---

## OpenAI

**Setup:**

1. Obtain an API key from [OpenAI](https://platform.openai.com/api-keys).
2. Set the `OPENAI_API_KEY` environment variable:
   ```sh
   export OPENAI_API_KEY=your-api-key-here
   ```

**Usage:**

When declaring a model, use the model name [provided by OpenAI](https://platform.openai.com/docs/models). For example:

```
gpt-4.1
o4-mini
```

---

## Anthropic

**Setup:**

1. Obtain an API key from [Anthropic](https://console.anthropic.com/settings/keys).
2. Set the `ANTHROPIC_API_KEY` environment variable:
   ```sh
   export ANTHROPIC_API_KEY=your-api-key-here
   ```

**Usage:**

When declaring a model, use the model name [provided by Anthropic](https://docs.anthropic.com/en/docs/about-claude/models/all-models#model-names). For example:

```
claude-3-7-sonnet-20250219
claude-3-5-sonnet-20241022
```

!!! warning
    We love Anthropic, but unfortunately their API is very unstable and often returns errors.
    Also, the rate limits could be exceeded very quickly.

---

## Gemini

**Setup:**

1. Obtain an API key from [AI Studio](https://aistudio.google.com/apikey).
2. Set the `GOOGLE_API_KEY` environment variable:
   ```sh
   export GOOGLE_API_KEY=your-api-key-here
   ```

**Usage:**

When declaring a model, use the model name [provided by Gemini](https://ai.google.dev/gemini-api/docs/models). For example:

```
gemini-2.4-flash-preview-04-17
gemini-2.5-pro-preview-05-06
```