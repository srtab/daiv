# Code Review Principles

This file is the canonical reference for code-review findings. Each principle is language-agnostic
and framework-agnostic so it applies equally across all mainstream languages and stacks. Use these
as the authoritative rationale when raising or defending a finding during review.

---

## 1. Dead code / unused / leftover

- Remove code that is never called, referenced, or reachable; it creates false surface area and misleads future readers.
- Delete commented-out blocks that were not intentionally preserved as documentation; version control is the right place for history.
- Remove variables, parameters, and imports that are declared but never used; compilers and linters flag them because they indicate incomplete thinking.
- Eliminate scaffolding, debug helpers, and TODO-stubs that were never meant to survive past the spike; shipping them signals carelessness.

## 2. Wrong placement / responsibility

- Move logic to the layer that owns its subject matter; a function that crosses layer boundaries will be found and tested in neither.
- A module that does two unrelated things should be split; mixed responsibility is the root cause of most surprising coupling.
- Configuration that belongs to the caller must not be hardcoded inside a helper; the helper should be reusable without modification.
- A class or module that talks directly to infrastructure it did not receive as a dependency is harder to test and harder to replace.

## 3. Use existing framework or library feature

- Replace hand-rolled logic with the equivalent built-in; the built-in has been tested, handles edge cases, and communicates intent.
- Using a well-known abstraction (pagination, serialisation, retry) from the ecosystem reduces the code reviewers must reason about.
- Custom re-implementations of standard utilities accrue their own bugs over time while diverging from community expectations.
- Before adding a new helper, check whether the standard library or an already-imported dependency already solves the problem.

## 4. Naming that misleads

- A name that describes what a thing *does* incorrectly causes callers to misuse it; fix the name, not the callers' mental model.
- Boolean names must read as predicates; a name that sounds like an action (not a state) inverts the reader's expectation.
- Generic names (`data`, `result`, `temp`, `info`) that carry no domain meaning force the reader to trace the value to understand it.
- When a function's name promises one thing but its body does another, fix the name first before changing any logic.
- Abbreviations that are not universally standard in the domain create unnecessary decoding overhead for every future reader.

## 5. Duplication / reuse opportunity

- Two or more blocks that are logically identical, differing only in literal values, should be extracted into a parameterised function.
- Copy-paste with minor edits is a maintenance trap: a bug fixed in one copy is silently left in the others.
- Repeated conditional logic that guards the same invariant belongs in one place so the invariant can be changed once.
- A shared abstraction that is expressed in two modules will diverge; unify it before it drifts into incompatibility.

## 6. Convention deviation

- Inconsistent naming style within a module forces context-switching that slows reading; follow the established pattern.
- File, module, and type organisation that departs from the project's established structure makes the change harder to discover later.
- Ordering of elements (fields, methods, declarations) that departs from the team convention is noise that obscures intentional differences.
- Error handling that mixes styles (early return in one branch, exception in another) without reason violates the implicit contract readers rely on.

## 7. Correctness defect

- An off-by-one in a boundary condition produces wrong output on the case that matters most — the edge.
- A condition that uses the wrong logical operator (`and` vs `or`, `<` vs `<=`) will silently be wrong for one of its inputs.
- Mutating a collection while iterating it produces undefined or implementation-dependent results.
- A function that silently returns a fallback value on an unexpected input masks errors instead of surfacing them.
- State that is initialised in one branch but read without a corresponding initialisation in another branch is an intermittent defect.

## 8. i18n / localization

- User-visible text hardcoded outside the translation system will never be translatable without invasive refactoring later.
- Formatting of dates, times, numbers, and currencies must use locale-aware utilities rather than manual string assembly.
- Plural forms constructed with simple `if count == 1` logic are wrong for most of the world's languages; use a pluralisation library.
- String concatenation to build localised sentences breaks for languages with different word order; use template substitution instead.

## 9. UI / UX / accessibility

- Interactive elements must have descriptive labels that convey purpose to assistive technology, not just visual style.
- Error states must be communicated through more than colour alone; users with colour-vision deficiencies cannot rely on colour as the sole signal.
- Keyboard navigation must reach every interactive element; mouse-only flows exclude entire user populations.
- Focus management after a dynamic content change must be explicit; leaving focus stranded disorients screen-reader users.

## 10. Configuration / environment

- Values that differ between environments (development, staging, production) must come from configuration, not source code.
- A default value baked into source code that is wrong for production is a latent incident; make the absence of configuration a startup error.
- Configuration keys should be validated at startup rather than at the first use; failing fast surfaces misconfiguration before it causes data damage.
- Coupling two separately-deployed components by sharing a configuration file defeats independent deployment.

## 11. Magic values / hardcoded literals

- A numeric or string literal that appears more than once must be a named constant; repetition makes future changes error-prone.
- A bare literal whose meaning is not obvious from context must carry a name that makes its purpose self-documenting.
- Status codes, error codes, and threshold values embedded in logic create invisible coupling between the producer and all consumers.
- Named constants grouped by domain communicate that a set of values forms an intentional enumeration, not an accident of history.

## 12. Fail fast vs defensive coding

- Validate preconditions at the boundary where input first enters the system; detecting violations deep inside a call stack obscures the cause.
- A function that silently accepts invalid input and returns a wrong-but-plausible result is harder to debug than one that immediately fails.
- An explicit panic, assertion, or error at a precondition violation is documentation: it tells future readers what the invariant is.
- Defensive checks inside a hot inner loop that can never be triggered in practice are noise; move real validation to the entry point.

## 13. Unintended side effects

- A function named as a query must not mutate state; callers will call it repeatedly, expecting idempotence.
- Global or module-level state mutated inside a helper creates hidden coupling between callers that appear unrelated.
- A constructor or initialiser that performs I/O or network calls makes testing hard and startup order fragile.
- Side effects inside a conditional branch that is only sometimes evaluated create execution-order-dependent behaviour.

## 14. Input validation

- Every external input — user-supplied, file-derived, or received over a network — must be validated before it influences any business logic.
- Validation must happen at trust boundaries, not inside domain logic; business logic should assume it receives well-formed data.
- An error message that reveals internal structure (file paths, schema names, stack frames) aids attackers and confuses legitimate users.
- Rejecting invalid input with a descriptive error is always preferable to silently coercing it into something that may be subtly wrong.

## 15. Absent-value handling

- Every absent-value must be explicitly handled at the point of use; unguarded dereference of an absent value is a crash waiting to happen.
- Propagating an absent value deep into a call chain before checking it makes the failure site misleading; check early.
- A function that returns an absent value as a sentinel for an error should instead return an explicit error so callers cannot ignore it.
- Defaulting silently to a zero or empty value when a caller did not supply a required input masks missing data rather than surfacing it.

## 16. Performance (general)

- Allocating inside a tight loop when the allocation could be done once outside it accumulates garbage-collection pressure unnecessarily.
- Synchronous blocking calls on the main execution path prevent the runtime from doing other work; move them to a background worker or async path.
- Repeated serialisation or deserialisation of the same immutable value should be cached at the first call site.
- An O(n²) algorithm that operates on user-controlled input is a denial-of-service risk once real data is larger than the test fixture.

## 17. Repeated queries / lookups in loops

- A data-store query inside a loop that processes a list is always a candidate for a batch fetch before the loop.
- Cache lookups, filesystem reads, and remote calls should be hoisted out of loops when the result does not change per iteration.
- Emitting N+1 requests to a data store when one parameterised query would suffice is a latency and resource-usage defect.
- When a loop accumulates results that could be written in one batch write, the single write eliminates per-iteration round-trip overhead.

## 18. Authorization / authentication gaps

- Every endpoint or action that operates on sensitive data must verify that the caller has the right to perform it, not just that they are authenticated.
- Authorisation checks placed after the work is done, or only in the UI layer, can be bypassed by direct API calls.
- Resource ownership must be verified on every mutation; trusting a client-supplied identifier without re-reading the record allows horizontal privilege escalation.
- Defaulting to permissive when an authorisation decision is ambiguous violates the principle of least privilege; default to deny.

## 19. Secrets exposure

- Secrets, credentials, and tokens must never appear in source code, log output, error messages, or API responses.
- Printing or logging a request or response object that may contain credentials should be guarded by an explicit redaction step.
- A secret stored in a configuration file that is checked into version control is compromised from the moment of the first commit.
- Passing secrets as command-line arguments exposes them to process-listing tools; use environment variables or secret-store integration instead.

## 20. Typing / signatures

- A function signature that accepts a maximally general type when only a subset is valid makes incorrect calls undetectable until runtime.
- Return types must accurately reflect all possible outputs, including error and absent-value paths; lying return types shift the burden to callers.
- Overloaded or union types in a public signature that could be narrowed to a single type add complexity without value.
- Type aliases for domain concepts (identifiers, amounts, durations) prevent entire classes of accidental argument transposition.

## 21. Logging / observability

- Log messages must include enough context to reproduce the conditions that triggered them; a message without identifiers is unactionable.
- Logging at the wrong severity (debug noise as error, real errors as info) causes alert fatigue or missed incidents.
- Structured log output (key-value or JSON) is searchable and aggregatable; free-form concatenated strings are not.
- Sensitive data — tokens, passwords, personal identifiers — must never appear in log output regardless of severity level.

## 22. Concurrency / locking

- Shared mutable state accessed from concurrent execution contexts without a lock is a data race, regardless of how unlikely simultaneous access appears.
- Acquiring locks in inconsistent order across code paths is a deadlock waiting to happen; establish and document a canonical lock order.
- A lock held across a slow I/O call serialises all other waiters for the duration of the I/O; release it before the I/O and re-acquire after.
- Immutable values shared across execution contexts require no locking; prefer immutability over protective locks where the domain allows it.

## 23. Error handling

- An error that is caught and swallowed without logging or re-raising silently hides failures from operators and callers.
- Re-wrapping an error should add context (what was attempted, with what input) so the chain of causation is visible from the outermost message.
- Error types that are too broad (catching everything) prevent callers from reacting specifically to recoverable conditions.
- The same error must not be logged at both the point of origin and every layer above it; decide where it is handled and log once.

## 24. Migrations / schema changes

- Removing a column or table before all deployed code stops reading it will cause failures in any instance that has not yet been updated.
- Adding a non-nullable column without a default will break existing rows and any insert that does not supply the new value.
- An index added in the same migration step as a large table backfill may lock the table for an unacceptable duration in production.
- Migrations must be reversible or explicitly documented as irreversible; silent one-way changes block rollback in an incident.

## 25. API contract / backward compatibility

- Removing or renaming a public field or endpoint is a breaking change for every consumer that has not been updated simultaneously.
- Changing the type or semantics of an existing field without a version increment breaks consumers that rely on the old contract.
- Adding required parameters to an existing public function is a breaking change; use optional parameters with defaults when evolving.
- Versioning policy (deprecation notices, sunset dates, migration guides) must be communicated before the breaking change is deployed, not after.

## 26. Question for the author

- When the intent of a block is unclear from its structure and no comment explains it, ask before assuming — the logic may be intentionally non-obvious.
- A deliberate trade-off (performance vs correctness, simplicity vs extensibility) deserves a comment; if one is absent, ask whether the trade-off was conscious.
- Code that appears to duplicate an existing abstraction may have a reason for its existence; ask whether consolidation was considered before flagging it.
- When a test is absent for a non-trivial code path, ask whether it was intentionally skipped and what the plan is to cover it.
