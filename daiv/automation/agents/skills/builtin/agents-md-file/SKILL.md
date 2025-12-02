---
name: creating-agents-md-file
description: Generates an AGENTS.md for a repository when users request creation or updates, ensuring it reflects the latest structure, commands, and guardrails.
scope: issue
---
# Creating AGENTS.md files

## Required inputs
- Repository root, existing AGENTS.md files, README, docs/, Makefiles, package scripts.
- Architecture references (ADRs, diagrams, Celery/LangChain notes) and security policies.
- Style guides, lint configs, and commit-message conventions used by the project.

## Authoring workflow
Use this checklist to guide your work:

```
AGENTS.md Progress
- [ ] Step 1: Inventory repo docs (README, docs/, existing AGENTS.md, scripts)
- [ ] Step 2: Capture top-level directory map with one-sentence explanations
- [ ] Step 3: Collect canonical commands (install, lint, test, type-check, run, deploy)
- [ ] Step 4: Summarize style guides and naming conventions
- [ ] Step 5: Describe architecture (diagram + prose data flow)
- [ ] Step 6: Document testing approach (local + CI)
- [ ] Step 7: Capture security/compliance guidance
- [ ] Step 8: Review for TODOs, ordering, and ≤100 char lines
```

## Section template (keep exact order)
1. `# Project Overview` – single paragraph elevator pitch with stack + differentiator.
2. `## Repository Structure` – bullet or nested list for top-level dirs, ≤1 sentence each; only drill into key subdirs.
3. `## Build & Development Commands` – fenced bash blocks grouped by install/lint/test/type-check/run/deploy; preserve commands verbatim and cite source files when helpful.
4. `## Code Style & Conventions` – describe formatters, lint rules, naming schemes, and commit-message templates; reference config files (e.g., `pyproject.toml`, `.editorconfig`).
5. `## Architecture Notes` – Mermaid or ASCII diagram plus prose that names major components (Django apps, Celery workers, LangChain flows) and data movement.
6. `## Testing Strategy` – list unit/integration/e2e coverage, how to run locally, relevant `make` targets, and CI automation.
7. `## Security & Compliance` – secrets/env handling, dependency scanning, guardrails, license notes; use `> TODO:` when evidence is missing.

## Style and quality guardrails
- Stay concise; Markdown only; ≤100 char lines; no HTML.
- Reference files with forward slashes and relative paths.
- Never invent facts—insert `> TODO:` markers for gaps.
- Preserve useful content from any prior AGENTS.md.
- Prefer ordered lists for sequences; tables only where they add clarity.
