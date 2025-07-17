QUICK_ACTIONS_TEMPLATE = """### 🤖 {{ bot_name }} Quick-Actions
Comment **one** of the commands below on this {{ scope }} to trigger the bot:

{% for action in actions -%}
  {{ action }}
{% endfor -%}
"""

UNKNOWN_QUICK_ACTION_TEMPLATE = """### ⚠️ Unknown Quick-Action

`@{{ bot_name }} {{ verb }} {{ invalid_action}}` isn't a recognised **{{ verb }}** sub-command.

**Try one of these instead:**

{{ help }}

Need more options? Comment **`@{{ bot_name }} help`** to see the full quick-action reference.
"""
