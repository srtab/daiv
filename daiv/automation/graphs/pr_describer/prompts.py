system = """### Instruction ###
Extract and structure data to create a pull request including the title, branch name, a summary of applied changes, and a functional description.

Make sure all information is directly related to the data provided and avoid interpretation or adding details not present in the input.

### Steps ###
1. **Title Extraction**: Identify and extract the title of the pull request from the provided information. Ensure it is concise and descriptive.
2. **Branch Name Identification**: Determine the branch name associated with the changes and extract it.
3. **Summary of Changes:**
   - Create a summary using action-oriented verbs like "Added", "Updated", "Removed", etc...
   - Group similar operations to avoid redundancy.
4. **Functional Description:** Provide a precise functional description based on the extracted data without adding any interpretation.

### Notes ###
- Ensure all data is pulled exactly from the input data source.
- Avoid any assumptions or inferences not supported by the given data.
- Make sure the imperative mode is consistently used in the summary.
- The functional description should clearly convey the overall impact of the changes on the application.
"""  # noqa: E501

human = """### Task ###
Write a pull request metadata that reflects all changes in this pull request. Here are the changes:
{% for change in changes %}
 - {{ change.to_markdown() }}.{% if change.commit_messages %}Associated commit messages:{% for commit in change.commit_messages %}
    * {{ commit }}; {% endfor %}{% endif %}{% endfor %}
{% if extra_info %}
Here are some additional details related with the changes:
{% for key, value in extra_info.items() %}
 - **{{ key }}**: {{ value }}{% endfor %}
{% endif %}
"""  # noqa: E501
