---
description: "Create custom agent skills for DAIV: SKILL.md directories with specialized instructions and workflows that the agent follows when they are triggered."
---

# Agent skills

Skills are modular capabilities that extend DAIV's agent with specialized knowledge and workflows. Each skill is a directory containing a `SKILL.md` file with instructions the agent follows when the skill is triggered.

DAIV ships with [built-in skills](../features/slash-commands.md#built-in-skills). You can also write your own, for one repository or for all of them.

## How skills work

Skills use a **progressive disclosure** pattern:

1. **At startup**, DAIV reads the `name` and `description` from each skill's YAML frontmatter (about 100 tokens per skill).
2. **When triggered**, DAIV reads the full `SKILL.md` content and follows its instructions.
3. **As needed**, the agent reads the supporting files bundled with the skill (scripts, templates, configs).

This keeps context usage minimal until a skill is needed.

## Directory structure

Skills can live in any of these directories at the repository root:

- `.agents/skills/`
- `.claude/skills/`
- `.cursor/skills/`

DAIV scans all three. When more than one holds a skill with the same name, `.agents/skills/` wins over `.claude/skills/`, which wins over `.cursor/skills/`.

```text
my-repository/
├── .agents/
│   └── skills/
│       └── my-custom-skill/
│           ├── SKILL.md           # Required
│           └── scripts/
│               └── helper.py      # Optional supporting files
└── src/
```

DAIV does not copy built-in skills into your repository, and writes nothing to `.agents/skills/`. It loads them from its own global skills location (`/workspace/skills` inside the agent), a separate source next to the per-repository directories.

## Override priority

DAIV resolves a skill name in this order, highest priority first:

1. **Per-repository skills**, committed in the repository's `.agents/skills/`, `.claude/skills/` or `.cursor/skills/`.
2. **Custom global skills**, [mounted from the host or uploaded in the dashboard](#custom-global-skills).
3. **Built-in skills**, shipped with DAIV.

A custom global skill overrides the built-in skill of the same name, and a per-repository skill overrides both. To override a built-in skill in one repository, commit a skill with the same name to that repository.

The agent's system prompt lists the global source as `Global Skills`, and marks the last source, `.agents/skills`, `(higher priority)`.

## Skill security

!!! warning "Use skills from trusted sources only"
    Skills give the agent instructions and access to scripts. Use only skills you wrote or got from a source you trust.

Before you commit or upload a skill:

- Audit every file in it.
- Review its scripts carefully, because the agent can run any executable code in a skill.
- Check its external references, because a skill that fetches data from external URLs adds risk.

## Create a skill

To add a skill to one repository, create a directory for it and commit it. You need write access to the repository.

1. In `.agents/skills/` at the repository root, create a directory named after the skill, with lowercase letters, digits and hyphens only (for example `changelog`).
2. In that directory, create `SKILL.md`.
3. Add the YAML frontmatter with a `name` that matches the directory name and a `description`.
4. Below the frontmatter, write the instructions the agent follows.
5. Optional: add supporting files next to `SKILL.md`, such as scripts or templates.
6. Commit the directory to the repository.

DAIV loads the skill in runs on a branch that contains it. To have the agent walk you through these steps instead, run `@daiv /skill-creator` (see [`/skill-creator`](../features/slash-commands.md#skill-creator)).

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

Required fields:

| Field | Constraints | Description |
|-------|-------------|-------------|
| `name` | Max 64 chars, lowercase alphanumeric and hyphens, must match the directory name | Unique identifier for the skill. This is what users type after `/`. |
| `description` | Max 1024 chars | What the skill does **and when to use it**. The agent matches user requests against this text, so include trigger phrases. |

Optional fields:

| Field | Description |
|-------|-------------|
| `license` | License name or reference to a bundled license file |
| `compatibility` | Environment requirements (max 500 chars) |
| `metadata` | Arbitrary key-value pairs for additional properties |
| `allowed-tools` | Space-delimited list of tool names the skill recommends using |

### Write a good description

The `description` field is how the agent decides whether to use your skill. Include specific trigger phrases:

```yaml
# Good: includes trigger phrases the agent can match
description: >
  Diagnose and fix CI/CD pipeline failures. Use when the user mentions
  "pipeline is failing", "CI broken", "fix the build", or "debug the tests".

# Bad: too vague for the agent to match
description: Helps with CI/CD issues.
```

### Write specific instructions

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

Skills can include helper scripts, templates and reference documents. Reference them by paths relative to the skill directory:

```text
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

## Example: changelog skill

````markdown
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
5. Keep entries concise: one line per change

## Format

Use [Common Changelog](https://common-changelog.org/) format:

```markdown
## Unreleased

### Added
- New feature description
```
````

## Set branch and commit conventions

DAIV generates the branch names and commit messages for its changes. To make it follow your conventions, describe them in the repository's context file: `AGENTS.md`, or the file that `context_file_name` names in [`.daiv.yml`](repository-config.md#repository-settings). `.daiv.yml` has no option for the conventions themselves.

1. Open the context file in the repository root.
2. Add a section for each convention you want DAIV to follow.
3. Commit the file.

For example:

```markdown
## Branch naming
Use: pr/<issue-id>-<kebab-summary>

## Commit messages
Use: (<ISSUE-ID>) <type>: <summary>
Where <type> is one of: feat, fix, chore, docs, refactor, test
```

The angle-bracket parts belong to the convention: DAIV fills in the issue ID, type and summary for each change.

Without a convention, DAIV names branches `<type>/<short-kebab-summary>` and writes commit subjects in the Conventional Commits style, `<type>: <short summary>`, with a type of feat, fix, chore, docs, refactor or test.

## Custom global skills

A custom global skill is available in **all repositories**, unlike a per-repository skill. It has the same format as a built-in skill, a directory containing a `SKILL.md` file, and DAIV loads it from a directory on the host. You put skills there in one of two ways:

- [Mount a directory](#mount-a-directory-of-global-skills) of skills into the containers.
- [Upload a ZIP archive](#upload-a-global-skill) in the dashboard, as an admin.

Both use the same directory, `DAIV_AGENT_CUSTOM_SKILLS_PATH`, once it is mounted into the `app` container and into every container that runs the agent, so you can use both. A mounted skill appears in runs but not on the **Skills** page, which lists only uploaded skills.

DAIV materializes the skills at each agent invocation, so a new skill or a new file takes effect on the next task without a container restart. DAIV does not pick up an edit to a file it has already loaded, so replace the skill by upload, or recreate the containers that run the agent after editing a mounted file. A plain restart is not enough, because the cache lives in the container's `/tmp`. With Docker Compose, run `docker compose up -d --force-recreate worker worker-interactive`. With Docker Swarm, run `docker service update --force <stack>_worker` and `docker service update --force <stack>_worker-interactive`, replacing `<stack>` with your stack name.

### Mount a directory of global skills

1. On the host, create a directory with your skills, one subdirectory per skill:

    ```text
    custom-skills/
    ├── my-workflow/
    │   ├── SKILL.md
    │   └── scripts/
    │       └── helper.py
    └── team-review/
        └── SKILL.md
    ```

2. In your `docker-compose.yml` or `stack.yml`, mount the directory at `/home/daiv/data/skills` into `app`, which receives dashboard uploads and needs it read-write, and into `worker` and `worker-interactive`, which run the agent and can mount it read-only:

    ```yaml
    app:
      volumes:
        - ./custom-skills:/home/daiv/data/skills
    worker:
      volumes:
        - ./custom-skills:/home/daiv/data/skills:ro
    worker-interactive:
      volumes:
        - ./custom-skills:/home/daiv/data/skills:ro
    ```

3. Recreate the three containers.

A service-level `volumes:` replaces the list it inherits, so keep each service's existing `media-volume` line (see [Deployment](../getting-started/deployment.md)). To mount the directory elsewhere, set `DAIV_AGENT_CUSTOM_SKILLS_PATH` (see [Environment variables](../reference/env-variables.md#daiv-agent)).

### Upload a global skill

!!! warning "An upload replaces a skill of the same name"
    If a custom skill with the same name exists, uploading replaces it for every repository. DAIV moves the old version to `.trash/` under `DAIV_AGENT_CUSTOM_SKILLS_PATH` and removes it on the next upload or delete after 24 hours.

You need an admin account and a ZIP archive that meets the [ZIP requirements](#zip-requirements).

1. In the sidebar, select **Settings** → **Skills**.
2. Select **Upload skill**.
3. Under **Skill zip**, choose the ZIP archive.
4. Select **Upload**.
5. If DAIV shows **Replace existing skill**, choose the ZIP archive again and select **Replace**.

The skill appears under **Custom skills**. DAIV records which admin uploaded it.

#### ZIP requirements

DAIV validates the archive before it stores it:

| Constraint | Limit |
|------------|-------|
| Top-level directory | Exactly one, named after the skill (a valid slug: lowercase alphanumeric and hyphens, max 63 chars) |
| `SKILL.md` | Required at `<skill-name>/SKILL.md`, with `name` (matching the directory) and `description` in its frontmatter |
| Compressed size | 5 MiB max |
| Unpacked size | 25 MiB max |
| File count | 200 max |
| Path depth | 8 levels max |
| Per-file size | 1 MiB max |
| Allowed file types | `.md`, `.py`, `.json`, `.yaml`, `.yml`, `.txt`, `.sh`, `.toml`, `.png`, `.jpg`, `.jpeg`, `.svg`, `.gif` |

DAIV rejects symlinks, absolute paths, `..` path traversal and hidden path segments. It drops `__pycache__`, `.pyc`, `.git` and `.gitignore` entries silently.

### Manage global skills in the dashboard

The **Skills** page needs an admin account. **Custom skills** lists the skills uploaded in the dashboard, and **Built-in skills** lists the ones DAIV ships. Each card shows the skill's invocation count and, when its entry names one, its model. A custom skill whose name matches a built-in skill shadows it, and its card carries an **Overrides built-in** warning icon. A skill that you only mounted loads in runs but has no card or page, so it cannot be downloaded or deleted here.

Select a skill to open its page, which has four tabs:

- **SKILL.md**: the skill's instructions.
- **Files**: the files in the skill.
- **Models**: the model and thinking level the skill runs on (see [Run a skill on another model](#run-a-skill-on-another-model)).
- **Usage**: the invocations over the last 30 days, and the latest runs that invoked the skill, each linking to its run in its session.

To download a custom skill's original ZIP archive, select **Download zip** on its card menu or its page.

#### Delete a custom skill

!!! warning "Deleting removes the skill from every repository"
    DAIV moves the skill to a recycle bin for 24 hours before it removes it permanently. Download the ZIP archive first if you may need it. A custom skill that shadowed a built-in skill stops doing so: the built-in skill applies again.

You cannot delete a built-in skill.

1. On the **Skills** page, find the skill under **Custom skills**.
2. Open its card menu and select **Delete**.
3. Select **Delete skill**.

## Run a skill on another model

Once the agent invokes a skill, its model calls run on the skill's model and thinking level until your next message. The model and the level each come from the first of these that sets the field: the skill's `models.skills` entry in the repository's `.daiv.yml`, its entry on the **Models** tab or in `DAIV_AGENT_SKILL_MODELS`, the skill file's `metadata.model` and `metadata.thinking_level`, then the run's. A skill with only a level runs the run's model at that level. [Skill and subagent models](repository-config.md#skill-and-subagent-models) gives the order and the entry format.

The **Models** tab changes a skill's model and thinking level for every repository, built-in skills included, without copying or editing the skill file. On `/code-review` it also sets the model and level of each detector the skill ships, and a detector without its own setting inherits the skill's (see [Subagent model precedence](../features/subagents.md#subagent-model-precedence)). A skill that exists only in repositories, or that you only mounted, has no page: give it a model or thinking level with `DAIV_AGENT_SKILL_MODELS`, or with a `models.skills` entry in each repository's `.daiv.yml`.

A built-in or global skill file can name its own model in `metadata.model`, as a model spec (`provider-slug:model`, as in [subagents](../features/subagents.md)), and its thinking level in `metadata.thinking_level`: one of `minimal`, `low`, `medium`, `high`, `xhigh` or `off`. `off` turns thinking off.

```yaml
---
name: changelog
description: Update CHANGELOG.md for the current changes.
metadata:
  model: openrouter:anthropic/claude-haiku-5.5
  thinking_level: low
---
```

DAIV ignores a repository skill's `metadata.model` and `metadata.thinking_level`, because the file comes from the branch the run works on, which the author of a merge request controls.

A thinking level set at any step beats the run's, `daiv-max`'s included. The run's model serves any model call whose conversation no longer fits the skill model's context window. When the skill's model fails a call, the run's model serves that call and the rest of the skill's invocation, at the run's thinking level, before the run's fallback models. If the agent invokes the skill again later in the run, DAIV skips the model that failed and runs the next model in line at the skill's thinking level. Switching models starts a fresh prompt cache on each side, so a cheaper skill model pays off only for skills that do substantial work.

When a configured entry names a provider that is unknown, disabled or missing its API key, the skill runs on its `metadata.model`, else the run's model, and DAIV logs the failure at ERROR. A `metadata.model` that DAIV can't use keeps the run's model, logged the same way. DAIV drops an unknown thinking level and logs it as an error, and the rest of the entry still applies.
