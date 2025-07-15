PIPELINE_FIXER_TROUBLESHOOT_TEMPLATE = """### 🔧 Manual Remediation Required

The agent could not safely automate the items below.
Follow the **Suggested fix** (or ignore the agent's advice and solve another way) for job `{{ job_name }}`.

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

{% if show_warning %}
> ⚠️ {{ bot_name }} can make mistakes. Critical thinking is expected when interpreting the proposed remediation steps.
{% endif %}
"""  # noqa: E501


PIPELINE_FIXER_NO_FAILED_JOB_TEMPLATE = """### ✅ No Fixable Failures Detected

I scanned the latest pipeline but didn't find any job that met **all** of these criteria:

1. **Automatic** - not triggered as a manual step
2. **Required** - `allow_failure: false`
3. **Script-level failure** - exited with a non-zero status from the job script

---

#### Common reasons nothing shows up
- **Every job passed** 🎉
- A job failed but is marked `allow_failure: true` (e.g., lint, flaky test)
- Only **manual** or **skipped** jobs failed
- The failure came from infrastructure (timeout, runner outage) rather than the script itself

If you believe a failure should have been detected, review the job settings or rerun the pipeline with verbose logging.

{% if pipeline_url %}
📋 **Pipeline logs:** [Open in CI]({{ pipeline_url }})
{% endif %}
"""


PIPELINE_FIXER_REVIEW_PLAN_TEMPLATE = """### 🚀 Automatic Fix Plan

{% for plan_task in plan_tasks %}
<details>
<summary>

**Changes to apply {% if plan_task.file_path %}to `{{ plan_task.file_path }}`{% else %}to the repository{% endif %}**

</summary>

{{ plan_task.details }}
{% if plan_task.relevant_files %}
#### Relevant files:{% for file in plan_task.relevant_files %}
- `{{ file }}`{% endfor %}
{% endif %}

</details>

---

{% endfor %}
{% if manual_fix_template %}
{{ manual_fix_template }}
{% endif %}

### ✅ What Next?
* **Happy with the automatic plan?**
  Comment **`@daiv pipeline fix execute`** and I'll apply the changes to the PR.

---

> ⚠️ {{ bot_name }} can make mistakes. Critical thinking is expected when interpreting the proposed remediation steps.
"""  # noqa: E501
