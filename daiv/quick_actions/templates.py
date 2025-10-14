QUICK_ACTIONS_TEMPLATE = """### 🤖 {{ bot_name }} Quick-Actions
Comment **one** of the commands below on this {{ scope.lower() }} to trigger the bot:

{% for action in actions -%}
  {{ action }}
{% endfor -%}
"""

INVALID_ARGS_QUICK_ACTION_TEMPLATE = """### ⚠️ Invalid Arguments for Quick-Action

`@{{ bot_name }} /{{ command }} {{ invalid_args }}` aren't a recognised arguments for **/{{ command }}**.

**Here's how to use it correctly:**

{{ help }}

Need more options? Comment **`@{{ bot_name }} help`** to see the full quick-action reference.
"""

QUICK_ACTION_ERROR_MESSAGE = """### ❌ Quick-Action Error

I tried to run **`{{ command }}`**, but something unexpected happened and the action didn't complete.

**What you can do now**

1. 🔄 **Retry** - simply add the same quick-action comment again.
2. 📜 **Check the app logs** - open the DAIV logs to see the full stack trace and [open an issue](https://github.com/srtab/daiv/issues/new) if the problem persists.
"""  # noqa: E501
