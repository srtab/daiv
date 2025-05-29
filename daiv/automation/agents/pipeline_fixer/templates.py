PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE = """### 🚫 **Pipeline Job `{{ job_name }}` Failed**

The pipeline job has failed and requires **manual intervention**.

Here are the details to help you troubleshoot the issue:

{% for troubleshooting in troubleshooting_details %}
<details>
<summary>

**{{ troubleshooting.title }}{% if troubleshooting.file_path %} - `{{ troubleshooting.file_path }}`{% endif %}**

</summary>

{{ troubleshooting.details }}

{% for step in troubleshooting.remediation_steps %}
  - [ ] {{ step }}{% endfor %}

</details>

---
{% endfor %}

> ⚠️ {{ bot_name }} can make mistakes. Critical thinking is expected when interpreting the proposed remediation steps.
"""  # noqa: E501
