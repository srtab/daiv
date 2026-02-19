---
name: plan
description: This skill should be used when the user asks to explore a codebase and design implementation plans without making any changes. Trigger when users say 'plan how to implement X', 'design an approach for Y', 'explore the codebase before changing Z', 'create an implementation strategy', 'analyze how to refactor X', 'map out dependencies for Y', or 'I want a plan before we start coding'.
---

# Plan Mode: Read-Only Codebase Exploration and Implementation Design

## CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS

This is a read-only planning task. Make no file modifications of any kind — no creating, editing, deleting, moving, or copying files. No package installs. No git state changes. No redirect operators or heredocs that write to disk.
Explore using only: `grep`, `ls`, `read_file`, `glob`, `task` or `bash` (for read-only operations only eg. `git status`, `git log`, `git diff`, etc.).

Focus EXCLUSIVELY on exploring the codebase and designing implementation plans. The deliverable is a plan document, not code changes.

---

## Step 1: Assess Complexity

Before exploring, determine which tier the task falls into. This drives how much effort to invest.

| Tier | Scope | Plan Length |
|------|-------|-------------|
| **Simple** | < 1 hour — bug fix, CSS tweak, copy change, single validation | A few paragraphs |
| **Medium** | 1 hour–1 day — new form field, search filter, component refactor | 1–3 pages |
| **Complex** | Multi-day — auth system, major refactor, migration, new service | Multiple pages |

**Guiding principle:** Provide enough detail for confident implementation, but no more. Reserve comprehensive documentation for genuinely complex changes.

---

## Step 2: Explore the Codebase

Scale exploration effort to complexity.

If the request references a platform issue or merge request (for example `#123`, `!456`, or an issue/PR URL), fetch the full issue or merge request details from the Git platform tools before drafting the plan.

**Simple tasks:** Read the specific files that need to change. Run a few targeted greps to verify the change fits existing patterns.

**Medium tasks:** Trace through relevant code paths. Identify integration points and component dependencies.

**Complex tasks:** Conduct thorough reconnaissance before designing anything.

- Review all files mentioned in the prompt first
- Map project structure: modules, config files, build system, key dependencies
- Trace data flow end-to-end (entry → transformation → persistence)
- Use parallel tool calls to explore independent areas simultaneously (e.g., frontend and backend at the same time)
- Identify existing patterns — naming conventions, architectural style, error handling, testing approach
- Look for similar features already implemented that can serve as templates

---

## Step 3: Design the Solution

**For All Tasks:**
- Consider how the change fits existing patterns
- Think through edge cases
- Plan for appropriate testing

**For Medium/Complex Tasks:**

**Architectural Analysis:**
- Evaluate multiple implementation approaches
- Consider trade-offs between different designs
- Assess impact on existing components
- Identify integration points with current codebase
- Plan for backwards compatibility where needed

**Pattern Alignment:**
- Follow established patterns in the codebase
- Justify deviations from existing patterns when necessary
- Ensure consistency with project conventions
- Respect architectural boundaries and separation of concerns

**Risk Assessment (Complex Tasks Only):**
- Identify potential challenges and blockers
- Note areas requiring special attention
- Flag dependencies on external systems or teams
- Consider edge cases and error scenarios
- Anticipate testing challenges

---

## Step 4: Write the Plan

Use the format that matches complexity. See `examples/` for worked samples.

### Simple Format

```markdown
# [Brief Title]

## What's Changing
[1–2 sentence description]

## Files to Modify
- `path/to/file.js` (line ~45) — [what changes]
- `path/to/test.js` — [add test for...]

## Implementation Notes
- [Key consideration]
- [Edge case to handle]
```

### Medium Format

```markdown
# [Title]

## Overview
[Problem and solution summary]

## Implementation Steps
1. [Step] — [purpose]
2. [Step] — [purpose]
3. [Step] — [purpose]

## Files
- `file1.js` — Create/Modify — [purpose]
- `file2.js` — Create/Modify — [purpose]

## Testing
[What to test and how]

## Edge Cases
[Scenarios to handle]
```

### Complex Format

Use the template in `references/complex-plan-template.md`.

---

## Plan Quality Checklist

**Simple:** Change is clearly located. Key considerations noted. Concise — don't over-explain obvious steps.

**Medium:** Logical change sequence. File purposes clear. Testing approach defined. Edge cases and dependencies noted.

**Complex:** Problem statement and objectives clear. Architectural overview provided. Complete file-by-file breakdown. Implementation sequence is logical. Dependencies, config, and environment changes identified. Testing strategy defined. Edge cases and error handling addressed. Trade-offs and risks discussed.

---

## Exploration Tips

- **Efficient exploration:** Proactively use the `task` tool with `subagent_type=explore` to investigate the codebase efficiently.
- **Parallelize:** When investigating independent areas, issue parallel tool calls to save time.
- **Follow the data:** Trace how data enters, transforms, and persists — this reveals integration points faster than reading files top-down.
- **Use git strategically:** `git log --follow <file>` surfaces intent behind existing decisions.
- **Find templates:** Search for similar features already in the codebase before designing from scratch.
- **Stay focused:** Use grep and find deliberately. Don't explore unrelated code.

---

## Remember

**The goal is to provide an appropriate, actionable plan** - not to write comprehensive documentation for every task. Simple changes deserve simple plans. Complex changes deserve thorough analysis.

Match your effort to the task complexity, and focus on what will help implementation proceed smoothly.
