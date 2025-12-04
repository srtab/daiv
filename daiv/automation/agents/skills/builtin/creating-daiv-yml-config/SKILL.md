---
name: creating-daiv-yml-config
description: Creates or updates .daiv.yml configuration file with sandbox settings (base_image and format_code commands) based on repository content. Use when users request DAIV configuration setup or sandbox configuration.
scope: issue
---
# Creating .daiv.yml configuration files

## Authoring workflow
Use this checklist to guide your work:

```
.daiv.yml Progress
- [ ] Step 1: Fetch latest configuration documentation from the URL above
- [ ] Step 2: Check if .daiv.yml already exists and read its contents
- [ ] Step 3: Check CI/CD configs for Docker images (GitHub Actions, GitLab CI, etc.)
- [ ] Step 4: If no CI/CD config, detect repository language/stack and version
- [ ] Step 5: Determine appropriate base_image (from CI/CD or detected language)
- [ ] Step 6: Identify formatters/linters from config files, scripts, README.md, AGENTS.md, or CLAUDE.md
- [ ] Step 7: Construct format_code commands matching repository conventions
- [ ] Step 8: Create or update .daiv.yml with sandbox section only
- [ ] Step 9: Validate YAML syntax and structure
```

## Documentation reference
Always fetch the latest configuration schema before creating or updating .daiv.yml: https://srtab.github.io/daiv/latest/configuration/yaml-config/

This ensures you use the most current schema and options available.

## Base image selection

Determine the base image using the following priority order:

1. **Check CI/CD configuration files first**:
   - Look for Docker images declared in CI/CD configs (GitHub Actions workflows, GitLab CI, CircleCI, etc.). These typically specify the exact images used in the project's build pipeline and are the most reliable source.

2. **If no CI/CD config found, detect language and version**:
   - Identify the primary programming language from package managers and config files (pyproject.toml, package.json, Cargo.toml, go.mod, etc.)
   - Determine the language version from version files (.python-version, .nvmrc, rust-toolchain.toml, go.mod, etc.)
   - Select an appropriate base image matching the detected language and version
   - Prefer Alpine-based images for smaller size when available

## Format code command detection

Identify formatting and linting tools from repository configuration files and scripts. Preserve existing conventions rather than imposing new ones.

**Detection approach**:
- Check package manager config files for formatter/linter tool declarations
- Look for tool-specific configuration files (e.g., `.prettierrc`, `ruff.toml`, `.eslintrc`, etc.)
- Review Makefile or package.json scripts for `format`, `lint`, `check` targets
- Check README.md, AGENTS.md, or CLAUDE.md for documented formatting/linting commands
- Prefer existing scripts/commands over constructing new ones
- **Avoid** using `.pre-commit-config.yaml` as reference—pre-commit hooks often include additional checks not suitable for `format_code`

**Command construction principles**:
- Use the exact commands found in scripts or CI/CD configs when available
- If tools need installation, add dependency installation commands first
- Run linter fixes before formatters (order: lint fixes, then formatting)
- Use safe execution flags (--fix, write modes) to avoid errors
- When multiple formatters are detected, include all relevant commands in appropriate order

**When no formatters/linters are detected**:
- Limit investigation to checking common config files and scripts only
- Do not spend excessive tool calls searching for formatters that may not exist
- If no formatting/linting tools are found after reasonable checks, omit `format_code` from the configuration (set to `null` or exclude the key)
- Never invent or impose formatters that don't exist in the repository

## Format code command construction

Build commands that match repository conventions:

1. **Dependency installation**: If tools aren't in base image, add install step using the appropriate package manager for the detected language

2. **Command order**: Run linters with --fix before formatters

3. **Working directory**: Use `.` for repository root, or specific paths if tools are scoped

4. **Preserve existing patterns**: If Makefile or package.json scripts exist, prefer those over direct tool invocations

5. **Error handling**: Commands should be safe to run (--fix flags, write modes)

## Output template

Create or update .daiv.yml with only the sandbox section:

```yaml
sandbox:
  base_image: "python:3.12-alpine"
  format_code:
    - "pip install ruff"
    - "ruff check --fix"
    - "ruff format"
```

If .daiv.yml exists with other sections, preserve them and only add/update the `sandbox` section.

## Quality guardrails

- Only configure `sandbox` section (`base_image` and `format_code`)
- Never remove or modify other `.daiv.yml` sections if they exist
- Use specific image versions (not `latest`) for reproducibility
- Include dependency installation in `format_code` if tools aren't in base image
- Match repository's existing formatting conventions
- Validate YAML syntax before completing
- Keep `format_code` commands concise and executable
- Prefer Alpine-based images for smaller container size
- Use official Docker images from Docker Hub or verified registries
- If no formatters/linters are detected after checking common config files and scripts, omit `format_code` rather than inventing commands
- Limit investigation time—do not exhaustively search for formatters that may not exist

## Examples

### Example 1: Python project with ruff
**Detected**: pyproject.toml with [tool.ruff], Python 3.12

**Output**:
```yaml
sandbox:
  base_image: "python:3.12-alpine"
  format_code:
    - "pip install ruff"
    - "ruff check --fix"
    - "ruff format"
```

### Example 2: Node.js project with prettier and eslint
**Detected**: package.json with prettier and eslint configs, Node 20

**Output**:
```yaml
sandbox:
  base_image: "node:20-alpine"
  format_code:
    - "npm install"
    - "npx eslint --fix ."
    - "npx prettier --write ."
```

### Example 3: Rust project
**Detected**: Cargo.toml, rust-toolchain.toml with 1.75.0

**Output**:
```yaml
sandbox:
  base_image: "rust:1.75-alpine"
  format_code:
    - "cargo fmt"
```

### Example 4: Go project
**Detected**: go.mod with Go 1.21

**Output**:
```yaml
sandbox:
  base_image: "golang:1.21-alpine"
  format_code:
    - "gofmt -w ."
```

### Example 5: Python project using uv
**Detected**: pyproject.toml with uv, Python 3.14

**Output**:
```yaml
sandbox:
  base_image: "ghcr.io/astral-sh/uv:python3.14-bookworm-slim"
  format_code:
    - "uv sync --only-group=dev"
    - "uv run --only-group=dev ruff check . --fix"
    - "uv run --only-group=dev ruff format ."
```

### Example 6: Project with no formatters detected
**Detected**: Base image from CI/CD, but no formatting/linting tools found

**Output**:
```yaml
sandbox:
  base_image: "python:3.12-alpine"
```


