---
name: init
description: Generate or update an AGENTS.md file by analyzing a repository's structure, commands, tests, and conventions. Use when asked to create or improve `AGENTS.md` or initialize a new repository.
---

Analyze this codebase and create an `AGENTS.md` file, which will be given to future
instances of DAIV and other AI agents to operate in this repository.

Output:
- Produce one Markdown document: the full contents of `AGENTS.md`.

What to add:
1. Commands that will be commonly used, such as how to build, lint, and run tests. Include the necessary commands to develop in this codebase, such as how to run a single test.
2. High-level code architecture and structure so that future instances can be productive more quickly. Focus on the "big picture" architecture that requires reading multiple files to understand.

Usage notes:
- If an `AGENTS.md` exists, suggest improvements to it.
- Include important rules from `.cursor/rules/`, `.cursorrules`, `CLAUDE.md`, `.github/copilot-instructions.md` when present.
- If there is a README.md, make sure to include the important parts.
- When you make the initial AGENTS.md, do not repeat yourself and do not include obvious instructions like "Provide helpful error messages to users", "Write unit tests for all new utilities", "Never include sensitive information (API keys, tokens) in code or commits".
- Avoid listing every component or file structure that can be easily discovered.
- Don't include generic development practices.
- Do not write "ask for confirmation" directives; express risk as plan-time requirements.
- Do not make up information such as "Common Development Tasks", "Tips for Development", "Support and Documentation" unless this is expressly included in other files that you read.
- Be sure to prefix the file with the following text:

\`\`\`
# AGENTS.md

This file provides guidance to agents when working with code in this repository.
\`\`\`
