# Contract: `automations` in `.daiv.yml`

DAIV reads this section from the default branch only, and caches it with the rest of the file. A push to the default branch clears the cache.

```yaml
automations:
  - name: review
    on: [merge_request.opened, merge_request.updated, merge_request.ready]
    prompt: /code-review
    filters:
      drafts: false
      target_branches: [main, "release/*"]
      paths: ["src/**", "!src/**/*.md"]
      daiv_merge_requests: true
```

## Entry fields

| Key | Type | Required | Default | Rules |
|---|---|---|---|---|
| `name` | string | yes | — | `^[a-z0-9][a-z0-9-]{0,63}$`. Unique within the file: a later duplicate is dropped |
| `on` | list of strings | yes | — | A non-empty subset of `merge_request.opened`, `merge_request.updated`, `merge_request.ready` |
| `prompt` | string | yes | — | Not blank. Sent as the request on the merge request, as a mention's text would be. A skill is invoked as `/name` |
| `filters.drafts` | bool | no | `false` | `false`: no run while the merge request is a draft, then `merge_request.ready` fires when it is marked ready |
| `filters.target_branches` | list of globs | no | any | The merge request's target branch must match one |
| `filters.paths` | list of globs | no | any | At least one changed path must match. An entry starting with `!` excludes. When the platform can't list every changed path, the run starts anyway |
| `filters.daiv_merge_requests` | bool | no | `false` | Whether the automation runs on merge requests a DAIV session owns |

## Events

| Event | Fires when |
|---|---|
| `merge_request.opened` | A merge request is opened |
| `merge_request.updated` | An open merge request receives new commits |
| `merge_request.ready` | A draft merge request is marked ready |

## Validation

- **Per entry:** each entry is validated on its own. An invalid entry is dropped and logged at ERROR with the repository and the reason, and every other entry and every other section still apply.
- **Wrong type:** a value that is not a list drops the whole `automations` section, logged at ERROR. The rest of the file still applies.
- **Unknown keys** inside an entry are errors. That catches a misspelled filter that would otherwise widen the automation.

## Runs an automation starts

- **Session:** one per automation and merge request.
- **Attribution:** to the platform account whose action caused the event; the run acts as no one (`acts_as_user = false`).
- **Silent and non-interactive:** muted, and it cannot ask a question.
- **Model:** the repository's and site's configured models, including `models.skills` for the skill a prompt invokes.
- **Result:** posted as one comment on the merge request, or a failure comment.
- **Allowlist:** when `allowed_usernames` is set and the actor is not on it, no run starts.
