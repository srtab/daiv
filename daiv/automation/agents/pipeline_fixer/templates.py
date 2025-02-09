PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE = """### 🚫 **Pipeline Job `{{ job_name }}` Failed**

Unfortunately, the pipeline job for this Merge Request has failed and requires **manual intervention**:

{% for troubleshooting in troubleshooting_details %}
<details>
<summary>

**{{ troubleshooting.details }}**

</summary>

**Remediation steps:**
{% for step in troubleshooting.remediation_steps %}
  - [ ] {{ step }}{% endfor %}

---
</details>
{% endfor %}

> ⚠️ {{ bot_name }} can make mistakes. Critical thinking is expected when interpreting the proposed remediation steps.
"""  # noqa: E501
