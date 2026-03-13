# Complex Plan Template

Use this template for multi-day tasks: new systems, major refactors, data migrations, architectural changes.

Adapt sections to fit the task — not every section applies to every complex task. Remove sections that add no value.

---

```markdown
# Implementation Plan: [Feature/Refactor Name]

## 1. Overview

[High-level description of the change, its purpose, and success criteria. 2–4 sentences.]

## 2. Current State Analysis

[What exists today. Relevant patterns, architectural decisions, and dependencies discovered during exploration. Note anything that constrains the design.]

## 3. Proposed Solution

[Architecture overview. Key design decisions and why they were made. Diagrams or ASCII art if helpful. Alternatives considered and why they were rejected.]

## 4. Implementation Phases

### Phase 1: [Foundation/Setup]
1. **[Action]** — [Purpose]
   - `path/to/file1.js` — Create — [what to add]
   - `path/to/file2.js` (line ~N) — [what changes]

2. **[Action]** — [Purpose]
   - `path/to/file3.js` — [what changes]

### Phase 2: [Core Implementation]
1. **[Action]** — [Purpose]
   - `path/to/file4.js` — Create — [what to add]

2. **[Action]** — [Purpose]
   - `path/to/file5.js` (line ~N) — [what changes]

### Phase 3: [Integration and Testing]
1. **[Action]** — [Purpose]
   - `path/to/file6.test.js` — Create — [what to test]

## 5. Dependencies and Configuration

[New packages to install. Config file changes. Environment variables to add. Infrastructure requirements.]

## 6. Testing Strategy

[Unit tests — what to cover and where. Integration tests — key flows to verify. Manual testing steps.]

## 7. Edge Cases and Error Handling

[Specific scenarios that need explicit handling. Validation requirements. Error messages and states. Failure modes and recovery behavior.]

## 8. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| [Risk 1] | High/Med/Low | High/Med/Low | [How to address] |
| [Risk 2] | High/Med/Low | High/Med/Low | [How to address] |

## 9. Open Questions

[Decisions that need clarification before or during implementation. Flag ambiguities discovered during exploration.]
```
