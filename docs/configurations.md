# Configurations

Customize DAIV for your repository using a `.daiv.yml` file.

This file lets you control features, code formatting, and more.

## Example Configuration

Below is a complete example of a `.daiv.yml` file.

You can copy and modify this template for your repository.

```yaml
# Repository settings
default_branch: main
repository_description: "Python web application using Django and React. Follows PEP 8 standards and the Airbnb style guide for JavaScript."
branch_name_convention: "Use 'feat/' for features, 'fix/' for bugfixes, 'docs/' for documentation"

# Feature toggles
features:
  auto_address_review_enabled: true
  auto_address_issues_enabled: true
  autofix_pipeline_enabled: true

# Code management
extend_exclude_patterns:
  - "**/tests/**"
  - "**/*.test.ts"
  - "coverage/**"
  - "*.min.js"

# Sandbox commands
commands:
  base_image: "python:3.12-alpine"
  install_dependencies: "pip install -r requirements.txt"
  format_code: "ruff check --fix && ruff format"
```

## Configure Repository Settings

Repository settings control the default branch, repository description, and branch naming convention.

| Option                   | Type   |  Default                | Description                                                                 |
|--------------------------|--------|------------------------|-----------------------------------------------------------------------------|
| `default_branch`         | `str | null`    | Repository default branch    | The branch DAIV uses by default to load the `.daiv.yml` file.            |
| `repository_description` | `str`    | `""`       | A brief description to help agents understand your repository. Max 400 chars. |
| `branch_name_convention` | `str`    | `"always start with 'daiv/' followed by a short description."`    | Naming convention for generating pull request branches.                                |

!!! tip
    - Use clear and simple branch-naming conventions.
    - Keep descriptions concise and informative.


## Enable or Disable Features

Control which DAIV features are active in your repository.

Under your `.daiv.yml` file's `features:` section, configure the following keys:

| Feature                          | Type    | Default | Description                                                      |
|-----------------------------------|---------|---------|------------------------------------------------------------------|
| `auto_address_review_enabled`     | `bool`    | `true`    | Enable the [code review addressor agent](ai-agents/code-review-addressor.md).                          |
| `auto_address_issues_enabled`     | `bool`    | `true`    | Enable the [issue addressor agent](ai-agents/issue-addressor.md).                                |
| `autofix_pipeline_enabled`        | `bool`    | `true`    | Enable the [pipeline fixing agent](ai-agents/pipeline-fixing.md).                                |

!!! tip
    Disable features you do not need to reduce noise and speed up processing.

## Customize Code Indexing

Control which files DAIV indexes for context.

!!! warning
    Files excluded from indexing will not be available to DAIV's AI agents.

| Option                   | Type    | Default                | Description                                                                 |
|--------------------------|---------|------------------------|-----------------------------------------------------------------------------|
| `extend_exclude_patterns` | `list[str]` | `[]`                   | Add patterns to exclude more files from indexing.                          |
| `exclude_patterns`         | `list[str]` | `["*package-lock.json", "*.lock", "*.svg", "*.pyc", "*.log", "*.zip", "*.coverage", "*.sql", "**/.git/**", "**/.mypy_cache/**", "**/.tox/**", "**/vendor/**", "**/venv/**", "**/.venv/**", "**/.env/**", "**/node_modules/**", "**/dist/**", "**/__pycache__/**", "**/data/**", "**/.idea/**", "**/.pytest_cache/**", "**/.ruff_cache/**"]`                   | Override the default exclude patterns.                                      |

!!! tip
    Exclude sensitive files and build artifacts.
    Prefer using `extend_exclude_patterns` to add more patterns.

## Set Up Sandbox Commands

To use sandbox commands, you must have a `daiv-sandbox` instance running (see the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repository for more information), and **all three options below (`base_image`, `install_dependencies`, and `format_code`) must be set**.

Under your `.daiv.yml` file's `commands:` section, configure the following keys:

| Option                | Type   | Default | Description                                               |
|-----------------------|--------|---------|-----------------------------------------------------------|
| `base_image`          | `str`    | `null`    | Docker image for the sandbox. Use distro images only.     |
| `install_dependencies`| `str`    | `null`    | Command to install project dependencies.                  |
| `format_code`         | `str`    | `null`    | Command to format code before committing.                |

**Here's how it works:**

Before **committing code generated by DAIV**, DAIV will call `daiv-sandbox` to:

  - Create a container from the `base_image`.
  - Execute the `install_dependencies` command in the container.
  - Execute the `format_code` command in the container after the `install_dependencies` command executed successfully.

!!! warning
    If any of the commands fail, DAIV will commit the code as is to be manually fixed, if needed.

!!! tip
    Use specific image versions for reproducibility.
