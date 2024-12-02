PIPELINE_FIXER_ROOT_CAUSE_TEMPLATE = """⚠️ **Pipeline Job `{{ job_name }}` Failed**

Unfortunately, the pipeline job for this Merge Request has failed and requires manual intervention:

### 🛑 **Root Cause**
{{ root_cause }}

### 🛠️ **Suggested Actions**
{% for action in actions %}
**{{ action.description }}**
{% for step in action.steps %}
  - [ ] {{ step }}{% endfor %}

{% endfor %}
---

Thank you for your attention to this matter! 😊
"""  # noqa: E501
