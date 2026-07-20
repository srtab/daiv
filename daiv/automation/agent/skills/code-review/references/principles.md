# Code Review Principles

The canonical, language-agnostic list of code-review principles and the rationale for raising or defending a finding.

---

## 1. Dead code / unused / leftover

Remove anything never called, referenced, or reachable — including commented-out blocks (version control is the history), unused variables/parameters/imports, and leftover scaffolding, debug helpers, or TODO-stubs. Dead code creates false surface area and misleads future readers.

## 2. Wrong placement / responsibility

Wrong-layer logic, or mixed unrelated concerns in one module, causes hard-to-test coupling; a helper must not hardcode caller config or embed un-injected infra dependencies.

## 3. Use existing framework or library feature

Prefer a tested built-in or well-known library abstraction over hand-rolled logic, which accrues its own bugs; check existing dependencies before adding a new helper.

## 4. Naming that misleads

A misleading name — wrong description, non-predicate boolean, generic placeholder, or odd abbreviation — makes readers trace the value, not trust it; fix the name, not the mental model.

## 5. Duplication / reuse opportunity

Consolidate near-identical blocks, copy-paste edits, and repeated invariant checks before a fix lands once and misses the rest; unify duplicated abstractions before they diverge.

## 6. Convention deviation

Deviating from convention — naming, file/module organisation, ordering, or mixed error-handling style — forces context-switching and hides real differences; follow the pattern absent a deliberate reason not to.

## 7. Correctness defect

Off-by-one boundaries, wrong logical operators (`and`/`or`, `<`/`<=`), mutating a collection while iterating it, and state initialised in one branch but read in another are defects that produce wrong results on ordinary inputs. Silently returning a fallback on unexpected input masks errors instead of surfacing them.

## 8. i18n / localization

Hardcoded UI text, manual date/currency formatting, naive pluralisation, and concatenated sentences break most languages; use translation, locale-aware formatting, and pluralisation libraries.

## 9. UI / UX / accessibility

Interactive elements need assistive-tech labels, error states must not rely on colour alone, and controls must be keyboard-reachable; manage focus after dynamic changes or screen readers lose users.

## 10. Configuration / environment

Environment-dependent values must come from config, not source; a bad default must fail at startup, not silently apply; validate config at startup, and never couple deployables via shared config.

## 11. Magic values / hardcoded literals

A repeated or non-obvious literal must become a named constant; embedded status/error codes and thresholds create invisible producer-consumer coupling — group related constants as an intentional enumeration.

## 12. Fail fast vs defensive coding

Validate preconditions at the boundary — a plausible-but-wrong fallback is harder to debug than failing immediately, and an assertion documents the invariant; skip checks that never trigger in a loop.

## 13. Unintended side effects

A query-named function must not mutate state; global state mutated in a helper hides coupling; constructor I/O fragilises testing/startup; skipped-branch effects cause order dependence.

## 14. Input validation

Every external input must be validated at the trust boundary, not inside domain logic; reject invalid input with a descriptive error rather than silently coercing it, and never let error messages leak internal structure.

## 15. Absent-value handling

Every absent value must be handled at the point of use, not propagated deep before checking; return an explicit error, not a sentinel value, and never default a missing input to zero or empty.

## 16. Performance (general)

Allocating inside a loop instead of once outside it, blocking calls on the main path, and uncached serialisation waste resources; an O(n²) algorithm on user input risks DoS.

## 17. Repeated queries / lookups in loops

A data-store query, remote call, cache lookup, or filesystem read inside a loop is a batch-fetch candidate — N+1 round-trips where one parameterised query (or one batch write for accumulated results) would do; hoist anything whose result doesn't change per iteration.

## 18. Authorization / authentication gaps

Sensitive actions need authorisation, not just authentication, before running, not only in the UI; re-verify ownership per mutation instead of trusting the client ID; default ambiguous decisions to deny.

## 19. Secrets exposure

Secrets must never appear in source, logs, errors, or responses; redact anything carrying credentials; a committed secret is compromised immediately; CLI args leak via process listing — use env vars.

## 20. Typing / signatures

An overly general parameter type, or a return type omitting error/absent cases, defers failures to callers; drop needless unions/overloads, and alias domain concepts against transposition.

## 21. Logging / observability

An unactionable log lacks context to reproduce its trigger; wrong severity causes alert fatigue or missed incidents; prefer structured output over free text, and keep sensitive data out of logs.

## 22. Concurrency / locking

Unlocked shared mutable state under concurrency is a data race no matter how unlikely, and inconsistent lock order risks deadlock; release locks before slow I/O, or prefer immutable values needing none.

## 23. Error handling

A swallowed error hides failures; re-wrapping must add context to keep causation visible; overly broad catch types block targeted recovery; don't log the same error at both origin and every layer.

## 24. Migrations / schema changes

Dropping a column/table, or adding non-nullable without default, ahead of full rollout breaks code; a backfill-bundled index can lock tables too long; migrations must be reversible or explicitly not.

## 25. API contract / backward compatibility

Removing/renaming a field/endpoint, changing type/semantics without a version bump, or adding required params breaks lagging consumers; prefer optional defaults, announce deprecations first.

## 26. Question for the author

When intent or a deliberate trade-off lacks a comment, ask rather than assume; apparent duplication may be intentional, and a missing test on a non-trivial path deserves a question.
