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
- Step 1.1: [Action] — [Purpose]
- Step 1.2: [Action] — [Purpose]

### Phase 2: [Core Implementation]
- Step 2.1: [Action] — [Purpose]
- Step 2.2: [Action] — [Purpose]

### Phase 3: [Integration and Testing]
- Step 3.1: [Action] — [Purpose]
- Step 3.2: [Action] — [Purpose]

## 5. File Changes

| File | Action | Purpose |
|------|--------|---------|
| `path/to/file1.js` | Create | [Purpose] |
| `path/to/file2.js` | Modify | [Purpose] |
| `path/to/file3.test.js` | Create | [Purpose] |

## 6. Dependencies and Configuration

[New packages to install. Config file changes. Environment variables to add. Infrastructure requirements.]

## 7. Testing Strategy

[Unit tests — what to cover and where. Integration tests — key flows to verify. Manual testing steps.]

## 8. Edge Cases and Error Handling

[Specific scenarios that need explicit handling. Validation requirements. Error messages and states. Failure modes and recovery behavior.]

## 9. Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| [Risk 1] | High/Med/Low | High/Med/Low | [How to address] |
| [Risk 2] | High/Med/Low | High/Med/Low | [How to address] |

## 10. Open Questions

[Decisions that need clarification before or during implementation. Flag ambiguities discovered during exploration.]
```
