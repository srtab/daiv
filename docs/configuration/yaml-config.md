# YAML Configuration File

Customize DAIV for your repository using a `.daiv.yml` YAML configuration file in the default branch and repository root directory.

This file lets you control features, code formatting, and more.

## Example Configuration

Below is a complete example of a `.daiv.yml` YAML configuration file.

You can copy and modify this template for your repository.

```yaml
# Repository settings
default_branch: main
context_file_name: "AGENTS.md"

# File access
extend_exclude_patterns:
  - "**/tests/**"
  - "**/*.test.ts"
  - "coverage/**"

omit_content_patterns:
  - "*.min.js"
  - "*.svg"
  - "*.sql"
  - "*.lock"

# Features
issue_addressing:
  enabled: true

code_review:
  enabled: true

quick_actions:
  enabled: true

# Pull request
pull_request:
  branch_name_convention: "always start with 'daiv/' followed by a short description."

# Sandbox
sandbox:
  base_image: "python:3.12-alpine"
  format_code:
    - "ruff check --fix"
    - "ruff format"
```

## Configure Repository Settings

Repository settings control the default branch and context file configuration.

| Option                   | Type           | Default                | Description                                                                 |
|--------------------------|----------------|------------------------|-----------------------------------------------------------------------------|
| `default_branch`         | `str \| null`  | Repository default branch | The branch DAIV uses by default to load the `.daiv.yml` YAML configuration file.            |
| `context_file_name`      | `str \| null`  | `"AGENTS.md"`         | File name to load the repository context in the format of https://agents.md/. |

!!! tip
    - The context file helps agents understand your repository structure and conventions. You can use the [AGENTS.md](https://agents.md/) format to define your repository context.
    - If not specified, DAIV will look for an "AGENTS.md" file by default.

## Enable or Disable Features

Control which DAIV features are active in your repository.

Configure the following feature sections in your `.daiv.yml` YAML configuration file:

### Issue Addressing
| Option    | Type   | Default | Description                                 |
|-----------|--------|---------|---------------------------------------------|
| `enabled` | `bool` | `true`  | Enable the [issue addressor feature](../features/issue-addressor.md). |

**Enable automated issue resolution with plan approval:**

```yaml
# Minimal configuration for automated issue resolution
issue_addressing:
  enabled: true

quick_actions:
  enabled: true  # Required for /approve-plan command
```

!!! note
    Plan approval is handled via the `/approve-plan` quick action. See [Quick Actions](../features/quick-actions.md) for details.

### Code Review
| Option    | Type   | Default | Description                                 |
|-----------|--------|---------|---------------------------------------------|
| `enabled` | `bool` | `true`  | Enable the [code review addressor feature](../features/review-addressor.md). |

### Quick Actions
| Option    | Type   | Default | Description                                 |
|-----------|--------|---------|---------------------------------------------|
| `enabled` | `bool` | `true`  | Enable [quick actions feature](../features/quick-actions.md).              |

!!! tip
    Disable features you do not need to reduce noise and speed up processing.

## Control File Access

Control which files DAIV can see and read.

!!! warning
    Files excluded from being seen and/or read will not be available to DAIV's AI agents.

| Option                    | Type           | Default                | Description                                                                 |
|---------------------------|----------------|------------------------|-----------------------------------------------------------------------------|
| `extend_exclude_patterns` | `list[str]`    | `[]`                   | Add patterns to exclude more files from being seen.                          |
| `exclude_patterns`        | `tuple[str]`   | `["*.pyc", "*.log", "*.zip", "*.coverage", "**/.git/**", "**/.mypy_cache/**", "**/.tox/**", "**/vendor/**", "**/venv/**", "**/.venv/**", "**/.env/**", "**/node_modules/**", "**/dist/**", "**/__pycache__/**", "**/data/**", "**/.idea/**", "**/.pytest_cache/**", "**/.ruff_cache/**"]` | Override the default exclude patterns.                                      |
| `omit_content_patterns`   | `tuple[str]`   | `["*package-lock.json", "*pnpm-lock.yaml", "*.lock", "*.svg", "*.sql"]` | Files that DAIV can see exist but won't read their content.               |

!!! tip
    - Exclude sensitive files and build artifacts.
    - Prefer using `extend_exclude_patterns` to add more patterns.
    - Use `omit_content_patterns` for large files that shouldn't be read but need to be seen.

## Configure Pull Request Settings

Control how DAIV creates pull requests and branches.

| Option                   | Type    | Default                                                    | Description                                          |
|--------------------------|---------|-----------------------------------------------------------|------------------------------------------------------|
| `branch_name_convention` | `str`   | `"always start with 'daiv/' followed by a short description."` | Naming convention for generating pull request branches. Max 100 chars. |

!!! tip
    Use clear and simple branch-naming conventions to maintain consistency across your repository.

## Set Up Sandbox

To take advantage of the sandbox to execute commands, you must have a `daiv-sandbox` instance running (see the [daiv-sandbox](https://github.com/srtab/daiv-sandbox) repository for more information), and **the `base_image` option must be set** to enable sandbox functionality.

Under your `.daiv.yml` YAML configuration file's `sandbox:` section, configure the following keys:

| Option        | Type             | Default | Description                                               |
|---------------|------------------|---------|-----------------------------------------------------------|
| `base_image`  | `str \| null`    | `null`  | Docker image for the sandbox. Use distro images only.    |
| `format_code` | `list[str] \| null` | `null`  | List of commands to format code before committing.       |

**Here's how it works:**

Before **committing code generated by DAIV**, DAIV will call `daiv-sandbox` to:

  - Create a container from the `base_image`.
  - Execute each command in the `format_code` list sequentially to format the code before committing.

**Example configuration:**
```yaml
sandbox:
  base_image: "python:3.12-alpine"
  format_code:
    - "pip install ruff"
    - "ruff check --fix"
    - "ruff format"
```

!!! warning
    If any of the commands fail, DAIV will commit the code as is to be manually fixed by the user, if needed.

!!! tip
    - Use specific image versions for reproducibility.
    - Include dependency installation commands in the `format_code` list if needed.
    - The sandbox is only enabled when `base_image` is specified and `daiv-sandbox` is running.
