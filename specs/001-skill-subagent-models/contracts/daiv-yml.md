# Contract: `.daiv.yml` model maps

Two optional keys under `models`, read from the repository's default branch like the rest of the
file.

```yaml
models:
  skills:
    code-review: openrouter:anthropic/claude-sonnet-5.5
    plan: null              # unset: the site entry, if any, applies
  subagents:
    cr-security: openrouter:anthropic/claude-opus-5.5
    my-repo-subagent: openrouter:z-ai/glm-5.2
```

| Rule | Behaviour |
|------|-----------|
| Key | A skill or subagent name, matched exactly |
| Value | A model spec `provider-slug:model`; `null` or blank means unset |
| Precedence | Overrides the site configuration entry with the same name only |
| Invalid spec | The file still loads. At run time the entry falls back to the file-declared or run's model, not to the site entry, with an ERROR log and a notice on the run ([run-notice.md](run-notice.md)) |
| Unknown name | Ignored |
| `subagents.explore` | Ignored; explore keeps its own site setting |
| Run overrides | A run's picker choice, Max or exact pin does not change these entries |
