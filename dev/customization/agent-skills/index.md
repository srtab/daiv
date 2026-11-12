# Agent Skills

Skills are modular capabilities that extend DAIV's agent with specialized knowledge and workflows. Each skill is a directory containing a `SKILL.md` file with instructions the agent follows when the skill is triggered.

DAIV ships with [built-in skills](https://docs.daivagent.com/dev/features/slash-commands/#built-in-skills) (`/plan`, `/code-review`, `/security-audit`, `/init`, `/skill-creator`). You can also create your own.

## How skills work

Skills use a **progressive disclosure** pattern:

1. **At startup** — DAIV reads the `name` and `description` from each skill's YAML frontmatter (~100 tokens per skill)
1. **When triggered** — DAIV reads the full `SKILL.md` content and follows its instructions
1. **As needed** — the agent accesses supporting files bundled with the skill (scripts, templates, configs)

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
│       └── my-custom-skill/
│           ├── SKILL.md           # Required
│           └── scripts/
│               └── helper.py      # Optional supporting files
└── src/
```

Built-in skills are **not** copied into your repository. They are loaded from DAIV's own global skills location (`/workspace/skills` inside the agent), registered as a separate "Global" source alongside the per-repository directories above. Nothing is written to `.agents/skills/` and no `.gitignore` is generated.

You can still override any built-in skill by creating one with the same name in one of the per-repository directories and committing it: per-repository sources take precedence over the global source (see [Override priority](#override-priority)).

## Custom global skills

Custom global skills are user-defined skills that are available across **all repositories**, unlike per-repository skills. They follow the same directory format as built-in skills (a directory containing a `SKILL.md` file) and are loaded from a configurable path on the host filesystem.

### Setup

1. Create a directory with your custom skills on the host machine:

   ```
   custom-skills/
   ├── my-workflow/
   │   ├── SKILL.md
   │   └── scripts/
   │       └── helper.py
   └── team-review/
       └── SKILL.md
   ```

1. Mount it as a Docker volume into the DAIV worker container at `/home/daiv/data/skills/`:

   ```
   # In your docker-compose.yml or stack.yml
   worker:
     volumes:
       - ./custom-skills:/home/daiv/data/skills:ro
   ```

   The path inside the container is configurable via the `DAIV_AGENT_CUSTOM_SKILLS_PATH` environment variable (see [Environment Variables](https://docs.daivagent.com/dev/reference/env-variables/#daiv-agent)).

### Manage skills from the dashboard

Admin only

The Skills dashboard requires an admin account.

Admins can manage custom global skills from the web dashboard at **Skills** (`/dashboard/skills/`) instead of editing the mounted directory by hand. Both approaches write to the same `DAIV_AGENT_CUSTOM_SKILLS_PATH` location, so they coexist — the dashboard is an alternative to, not a replacement for, the Docker-volume approach.

From the Skills page you can:

- **List skills** — built-in and custom global skills are shown together, each with its invocation count and, when it has one, the model its entry names. A custom skill whose name matches a built-in shadows it and is flagged **Overrides built-in**.
- **Upload a skill** as a ZIP archive (see constraints below). Uploading a skill whose name already exists asks you to confirm before it replaces the existing one.
- **View a skill** — its page has four tabs: **SKILL.md**, **Files**, **Models** (the model it runs on; see [Running a skill on another model](#running-a-skill-on-another-model)) and **Usage** (invocations over the last 30 days, and the latest runs that invoked it, each linking to the run in its session).
- **Download** a custom skill's original ZIP.
- **Delete** a custom skill (built-in skills cannot be deleted; deleting a custom skill that shadowed a built-in restores the built-in).

A successful upload writes the skill into `DAIV_AGENT_CUSTOM_SKILLS_PATH` and records an audit row attributing it to the uploading admin.

#### ZIP requirements

The uploaded archive is validated before it is stored:

| Constraint          | Limit                                                                                                          |
| ------------------- | -------------------------------------------------------------------------------------------------------------- |
| Top-level directory | Exactly one, named after the skill (a valid slug: lowercase alphanumeric and hyphens, max 63 chars)            |
| `SKILL.md`          | Required at `<skill-name>/SKILL.md`, with `name` (matching the directory) and `description` in its frontmatter |
| Compressed size     | 5 MiB max                                                                                                      |
| Unpacked size       | 25 MiB max                                                                                                     |
| File count          | 200 max                                                                                                        |
| Path depth          | 8 levels max                                                                                                   |
| Per-file size       | 1 MiB max                                                                                                      |
| Allowed file types  | `.md`, `.py`, `.json`, `.yaml`, `.yml`, `.txt`, `.sh`, `.toml`, `.png`, `.jpg`, `.jpeg`, `.svg`, `.gif`        |

Symlinks, absolute paths, `..` path traversal, and hidden path segments are rejected; `__pycache__` and `.pyc` entries are dropped silently.

### Override priority

Skills are resolved in this order (highest priority wins):

1. **Per-repository skills** — committed in the repository's `.agents/skills/`, `.cursor/skills/`, or `.claude/skills/`
1. **Custom global skills** — mounted via Docker volume or uploaded from the dashboard
1. **Built-in skills** — shipped with DAIV

This means custom global skills override built-in skills with the same name, and per-repository skills override both.

### Notes

- Skills are materialized at each agent invocation, so changes to the mounted volume take effect on the next task without a container restart.
- Custom global skills load from the agent's global skills source, which appears as **Global Skills** in the system prompt's skills-locations block. Per-repository sources are listed after it and marked `(higher priority)`, so the agent knows a per-repository skill of the same name wins.

## Creating a skill

### SKILL.md format

Every skill requires a `SKILL.md` file with YAML frontmatter:

```
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

| Field         | Constraints                                                                 | Description                                                                                                                |
| ------------- | --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `name`        | Max 64 chars, lowercase alphanumeric and hyphens, must match directory name | Unique identifier for the skill. This is what users type after `/`.                                                        |
| `description` | Max 1024 chars                                                              | What the skill does **and when to use it**. The agent matches user requests against this text, so include trigger phrases. |

### Optional fields

| Field           | Description                                                   |
| --------------- | ------------------------------------------------------------- |
| `license`       | License name or reference to a bundled license file           |
| `compatibility` | Environment requirements (max 500 chars)                      |
| `metadata`      | Arbitrary key-value pairs for additional properties           |
| `allowed-tools` | Space-delimited list of tool names the skill recommends using |

### Running a skill on another model

Once the agent invokes a skill, its model calls run on the skill's model until your next message. The first of these that is set names that model:

1. the skill's entry under `models.skills` in the repository's `.daiv.yml` (see [Model overrides](https://docs.daivagent.com/dev/customization/repository-config/#skill-and-subagent-models));
1. the skill's entry on its **Models** tab in the dashboard's Skills pages (or in `DAIV_AGENT_SKILL_MODELS`);
1. the `metadata.model` of a built-in or global skill file;
1. the run's model.

The **Models** tab changes a skill's model for every repository, built-in skills included, without copying or editing the skill file. On `/code-review` it also sets the model of each detector the skill ships (see [Subagents](https://docs.daivagent.com/dev/features/subagents/#choosing-a-subagents-model)). A model the user picks for the run, even an exact model passed to the jobs API, does not override an entry.

A skill that exists only in repositories has no page: give it a model with a `models.skills` entry in each repository's `.daiv.yml`.

A built-in or global skill file can name its own model in `metadata.model`, as a model spec (`provider-slug:model`, as in [subagents](https://docs.daivagent.com/dev/features/subagents/index.md)):

```
---
name: changelog
description: Update CHANGELOG.md for the current changes.
metadata:
  model: openrouter:anthropic/claude-haiku-5.5
---
```

A repository skill's `metadata.model` is ignored, because the file comes from the branch the run works on, which the author of a merge request controls. Give a repository skill a model with a `models.skills` entry in `.daiv.yml` instead.

The run's thinking level carries over. The run's model serves any model call whose conversation no longer fits the skill model's context window, and, once the skill model fails a call, that call and every later one in the run, before the run's fallback models. Switching models starts a fresh prompt cache on each side, so a cheaper skill model pays off only for skills that do substantial work.

When a configured entry names a provider that is unknown, disabled or missing its API key, the skill runs on its `metadata.model`, else the run's model. The failure is logged at ERROR. A `metadata.model` that can't be used keeps the run's model, logged the same way.

### Writing a good description

The `description` field is how the agent decides whether to use your skill. Include specific trigger phrases:

```
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

```
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

```
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

````
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
3. Add an entry under the `## Unreleased` section
4. Follow the existing format and style in the file
5. Keep entries concise — one line per change

## Format

Use [Common Changelog](https://common-changelog.org/) format:

```markdown
## Unreleased

### Added
- New feature description
````

````

## Security considerations

Use skills from trusted sources only

Skills provide agents with instructions and access to scripts. Only use skills you created yourself or obtained from trusted sources.

- **Audit all files** in a skill before committing it to your repository
- **Review scripts carefully** — any executable code in a skill can be run by the agent
- **Check external references** — skills that fetch data from external URLs pose additional risk```
````
