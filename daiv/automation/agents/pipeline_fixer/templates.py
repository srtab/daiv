PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE = """🚨 The pipeline can't be fixed automatically. The job `{{ job_name }}` has failed and requires **manual intervention** to fix.

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


PIPELINE_FIXER_NO_FAILED_JOB_TEMPLATE = """I couldn't find any job in the pipeline with the following conditions:

- The job is not a manual job.
- The job is not allowed to fail.
- The job failure was caused by a script failure.

---

📋 Please check the pipeline logs 👉 [here]({{ pipeline_url }}) for more details.
"""
