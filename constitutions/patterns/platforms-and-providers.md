# Platforms and providers

Operators choose their git platform and their LLM provider. DAIV adapts to that choice; no
feature assumes one.

## Rules

1. **Both git platforms.** A feature that touches the git platform goes through `RepoClient`
   (`daiv/codebase/clients/base.py`) and works on GitLab and GitHub. A feature scoped to one
   platform says so, and why, in its spec and on its docs page. _Prevents_: users on one
   platform silently missing a feature the other has.
2. **No hardcoded provider or model.** A model is a `provider:model` spec resolved against
   `Provider` rows (`daiv/automation/agent/model_spec.py`). Defaults live in
   `daiv/core/constants.py` (`ModelName`) and settings, and every default can be overridden
   from site configuration or the environment. _Prevents_: a deployment locked to a provider
   it has no key for.
3. **Provider quirks stay in the model layer.** Provider SDK arguments, thinking and request
   shaping live in `daiv/automation/agent/base.py` and `chat_models.py`; callers stay
   provider-agnostic. `model_spec.py` imports no LangChain or provider SDK, because forms,
   validators and run admission load it at URL-load time. _Prevents_: provider branches spread through the
   agent, and URL loading pulling in the whole SDK stack.
4. **Shared identifiers come from constants.** Bot labels (`daiv`, `daiv-max`, `daiv-auto`)
   and model names come from `daiv/core/constants.py`, never literals. _Prevents_: a renamed
   label or model that half the code still spells the old way.
