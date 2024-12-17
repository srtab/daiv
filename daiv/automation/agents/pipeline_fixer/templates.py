PIPELINE_FIXER_ROOT_CAUSE_TEMPLATE = """### ⚠️ **Pipeline Job `{{ job_name }}` Failed**

Unfortunately, the pipeline job for this Merge Request has failed and requires **manual intervention**:

#### 🛑 **Root Cause**
{{ root_cause }}

#### 🛠️ **Suggested Actions**
{% for action in actions %}
<details>
<summary>

**{{ action.description }}**

</summary>
{% for step in action.steps %}
  - [ ] {{ step }}{% endfor %}

</details>
{% endfor %}

---

> ⚠️ {{ bot_name }} can make mistakes. Critical thinking is expected when interpreting the proposed actions.
"""  # noqa: E501
