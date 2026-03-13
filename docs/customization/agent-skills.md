# Agent Skills

Skills are modular capabilities that extend DAIV's agent with specialized knowledge and workflows. Each skill is a directory containing a `SKILL.md` file with instructions the agent follows when the skill is triggered.

DAIV ships with [built-in skills](../features/slash-commands.md#built-in-skills) (`/plan`, `/code-review`, `/security-audit`, `/init`, `/skill-creator`). You can also create your own.

## How skills work

Skills use a **progressive disclosure** pattern:

1. **At startup** — DAIV reads the `name` and `description` from each skill's YAML frontmatter (~100 tokens per skill)
2. **When triggered** — DAIV reads the full `SKILL.md` content and follows its instructions
3. **As needed** — the agent accesses supporting files bundled with the skill (scripts, templates, configs)

This keeps context usage minimal until a skill is actually needed.

## Directory structure

Skills can live in any of these directories at the repository root:

- `.agents/skills/`
- `.cursor/skills/`
- `.claude/skills/`

DAIV scans all three and loads skills from each. If multiple directories contain a skill with the same name, the last one loaded wins.

```
your-repository/
├── .agents/
│   └── skills/
│       ├── my-custom-skill/
│       │   ├── SKILL.md           # Required
│       │   └── scripts/
│       │       └── helper.py      # Optional supporting files
│       └── plan/                   # Built-in (auto-copied, gitignored)
│           └── SKILL.md
└── src/
```

Built-in skills are automatically copied to `.agents/skills/` at agent startup with a `.gitignore` to prevent them from being committed. You can override any built-in skill by creating one with the same name and committing it to the repository.

## Creating a skill

### SKILL.md format

Every skill requires a `SKILL.md` file with YAML frontmatter:

```markdown
---
name: my-skill
description: Brief description of what this skill does and when the agent should use it
---

# My Skill

## Instructions

1. First, do this
2. Then, do that
3. Finally, verify the result
```

### Required fields

| Field | Constraints | Description |
|-------|-------------|-------------|
| `name` | Max 64 chars, lowercase alphanumeric and hyphens, must match directory name | Unique identifier for the skill. This is what users type after `/`. |
| `description` | Max 1024 chars | What the skill does **and when to use it**. The agent matches user requests against this text, so include trigger phrases. |

### Optional fields

| Field | Description |
|-------|-------------|
| `license` | License name or reference to a bundled license file |
| `compatibility` | Environment requirements (max 500 chars) |
| `metadata` | Arbitrary key-value pairs for additional properties |
| `allowed-tools` | Space-delimited list of tool names the skill recommends using |

### Writing a good description

The `description` field is how the agent decides whether to use your skill. Include specific trigger phrases:

```yaml
# Good — includes trigger phrases the agent can match
description: >
  Diagnose and fix CI/CD pipeline failures. Use when the user mentions
  "pipeline is failing", "CI broken", "fix the build", or "debug the tests".

# Bad — too vague for the agent to match
description: Helps with CI/CD issues.
```

## Authoring best practices

### Be specific and actionable

Tell the agent exactly what steps to follow:

```markdown
## Instructions

1. Fetch the merge request diff using the platform tools
2. For each changed file, check for:
   - Missing error handling
   - Unvalidated inputs
   - Hardcoded credentials
3. Report findings grouped by severity (High / Medium / Low)
```

### Bundle supporting files

Skills can include helper scripts, templates, and reference documents. Reference them using relative paths from the skill directory:

```
my-skill/
├── SKILL.md
├── scripts/
│   └── validate.py
└── templates/
    └── report.md
```

```markdown
## Resources
- Run `scripts/validate.py` to check prerequisites
- Use `templates/report.md` as the output format
```

### Use the skill-creator

DAIV includes a built-in `/skill-creator` skill that walks you through creating a new skill:

```
@daiv /skill-creator
```

## Example: changelog skill

```markdown
---
name: changelog
description: >
  Update the CHANGELOG.md file when changes are made. Use when the user
  asks to "update the changelog", "add a changelog entry", or after
  completing a feature implementation.
---

# Changelog Maintenance

## Instructions

1. Read `CHANGELOG.md` from the repository root
2. Determine the change type: Added, Changed, Fixed, Removed
3. Add an entry under the `[Unreleased]` section
4. Follow the existing format and style in the file
5. Keep entries concise — one line per change

## Format

Use [Keep a Changelog](https://keepachangelog.com/) format:

```markdown
## [Unreleased]

### Added
- New feature description
```
```

## Security considerations

!!! warning "Use skills from trusted sources only"
    Skills provide agents with instructions and access to scripts. Only use skills you created yourself or obtained from trusted sources.

- **Audit all files** in a skill before committing it to your repository
- **Review scripts carefully** — any executable code in a skill can be run by the agent
- **Check external references** — skills that fetch data from external URLs pose additional risk
