---
name: init
description: This skill should be used when a user asks to initialize a repository for AI agents, create or update an AGENTS.md file, scaffold agent instructions for a codebase, or improve existing agent guidance. Triggers include phrases like 'create an AGENTS.md', 'set up agent instructions', 'initialize this repo for coding agents', 'update my AGENTS file', or 'analyze this repo and generate agent docs'.
---

Analyze this repository and produce the full contents of `AGENTS.md` for future coding agents.

## Goal (very important):
- Make this file as SHORT as possible while still capturing REPO-SPECIFIC, NON-OBVIOUS, HIGH-LEVERAGE guidance.
- Avoid redundancy: do not restate README/docs/rules unless you are adding a concise delta that changes agent behavior.
- Prefer pointing to existing docs by path (e.g., "See docs/xyz.md") rather than copying text.

## Hard constraints:
- Target length: 250–500 words. Only exceed if there is a critical repo-specific reason.
- Only include commands that you can VERIFY exist in the repo (Makefile, scripts, pyproject, CI files, docs). Do not guess.
- Do not add generic engineering advice, generic style guides, or “be careful” warnings.
- Do not invent sections (e.g., “Tips”, “Support”, “Common tasks”) unless you found that exact content in-repo and it is essential.
- Do not include “ask for confirmation” directives; instead, express risk as plan-time checks.

## If `AGENTS.md` already exists:
- Produce an updated version that reduces duplication and removes low-signal text.
- Keep any existing repo-specific invariants and verified commands, but rewrite for brevity and clarity.

## Inputs to consult (only extract what is truly needed and not redundant):
- Existing `AGENTS.md` (if present)
- `.cursor/rules/`, `.cursorrules`, `CLAUDE.md`, `.github/copilot-instructions.md` (if present)
- `README.md` and docs (only for verified commands / invariants)
- Build/test/lint definitions: Makefile, pyproject.toml, CI workflow files, scripts directories

## Required output format:
- Produce ONE Markdown document: the full contents of `AGENTS.md`.
- Prefix EXACTLY:

\`\`\`
# AGENTS.md

This file provides guidance to agents when working with code in this repository.
\`\`\`

See examples/agents-md-sample.md for a reference output demonstrating all four sections done well.

## Sections (keep terse; omit any section if you have nothing verified to add):

1. **Commands (verified)**: fastest path for test + lint + single-test invocation
2. **Repo map (only what’s non-obvious)**: 5–10 bullets max; name directories only if it helps navigation
3. **Invariants / footguns**: only rules that prevent wasted work or incorrect changes (e.g., state update patterns, “don’t edit X directly”)
4. **Where changes usually go** (optional): only if there are non-obvious entry points that save time

## Writing style:

* Bullets over paragraphs.
* No long examples. No duplicated explanations.
* If unsure, omit.
