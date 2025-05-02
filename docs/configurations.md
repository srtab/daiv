# Configurations

DAIV can be customised on a per-repository basis using a `.daiv.yml` file in the repository root. This configuration file allows you to customise various aspects of DAIV behaviour, from feature toggles to code formatting commands.

## Example Configuration

Here's a complete example configuration file:

```yaml
# Repository settings
default_branch: main
repository_description: "Python web application using Django and React. Follows PEP8 standards and Airbnb style guide for JavaScript."
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

## Repository Settings

### `default_branch`
* _Description_: The branch that DAIV will use as the default branch.
* _Type_: `str`
* _Required_: `false`
* _Default_: the repository's default branch will be used.

### `repository_description`
* _Description_: A brief description of your repository to help DAIV better understand it.
* _Type_: `str`
* _Required_: `false`
* _Maximum length_: 400 characters
* _Default_: the repository's description will be used.

!!! tip
    DAIV will try to understan

### `branch_name_convention`
* _Description_: A convention for branch names. This is used to generate the branch name for the pull requests.
* _Type_: `str`
* _Required_: `false`
* _Default_: the repository's default branch will be used.

!!! tip
    - Keep conventions simple and clear.
    - Define prefixes that match your team's workflow.


## Feature Flags

### `features`
* _Description_: Control which DAIV features are enabled for the repository.
* _Type_: `object`
* _Required_: `false`
* _Default_: all features are enabled.

### `features.auto_address_review_enabled`
* _Description_: Enable [code review addressor agent](ai-agents/code-review-addressor.md).
* _Type_: `boolean`
* _Required_: `false`
* _Default_: `true`

### `features.auto_address_issues_enabled`
* _Description_: Enable [issue addressor agent](ai-agents/issue-addressor.md).
* _Type_: `boolean`
* _Required_: `false`
* _Default_: `true`

### `features.autofix_pipeline_enabled`
* _Description_: Enable [pipeline fixing agent](ai-agents/pipeline-fixing.md).
* _Type_: `boolean`
* _Required_: `false`
* _Default_: `true`

## Code Indexing

In order to provide better context to the AI agents, DAIV will index your codebase and persist the extracted code snippets.

These are the available options to customize the code indexing process of the repository:

### `extend_exclude_patterns`
* _Description_: Extend the default exclude patterns. All the patterns will be ignored from the code indexing process.
* _Default_: `[]`.
* _Type_: `list[str]`
* _Required_: `false`

!!! tip
    - Always exclude sensitive files and directories
    - Exclude build artifacts and cache directories
    - Consider excluding test directories if they shouldn't be analyzed

!!! warning
    These affect directly the code indexing process. All the files that match the patterns will be excluded from the code indexing, so they **won't be available for the AI agents**.

### `exclude_patterns`
* _Description_: Default exclude patterns. Don't include this option if you want to use the default exclude patterns. Use `extend_exclude_patterns` instead to add more patterns.
* _Type_: `list[str]`
* _Required_: `false`
* _Default_: `["*package-lock.json", "*.lock", "*.svg", "*.pyc", "*.log", "*.zip", "*.coverage", "*.sql", "**/.git/**", "**/.mypy_cache/**", "**/.tox/**", "**/vendor/**", "**/venv/**", "**/.venv/**", "**/.env/**", "**/node_modules/**", "**/dist/**", "**/__pycache__/**", "**/data/**", "**/.idea/**", "**/.pytest_cache/**", "**/.ruff_cache/**"]`

## Commands

You can configure commands to be executed in the sandbox environment ([`daiv-sandbox`](https://github.com/srtab/daiv-sandbox)).

### `commands`
* _Description_: Configure commands to be executed in the sandbox environment.
* _Type_: `object`
* _Required_: `false`
* _Default_: `{}`

### `commands.base_image`
* _Description_: The base Docker image to use for the sandbox environment.
* _Type_: `str`
* _Required_: `false`
* _Default_: `null`

!!! warning
    The image need to be a distro image. Distroless images will not work.

!!! tip
    - Use specific versions in base images for reproducibility

### `commands.install_dependencies`
* _Description_: The command to install the project dependencies.
* _Type_: `str`
* _Required_: `false`
* _Default_: `null`

### `commands.format_code`
* _Description_: The command to format the code. This command will be executed before the code is committed to the repository in conjunction with the `install_dependencies` command.
* _Type_: `str`
* _Required_: `false`
* _Default_: `null`
