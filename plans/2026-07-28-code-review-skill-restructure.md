# Code-Review Skill Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the code-review skill to the pr-review-toolkit shape: one short orchestrator `SKILL.md`, five self-contained prose-reporting detector charters, zero scripts — per the approved spec `specs/2026-07-27-code-review-skill-restructure-design.md`.

**Architecture:** Detectors become rich personas that return markdown reports inline (no structured `response_format`, no deferred output files). The orchestrator filters applicable detectors, fans out in parallel, aggregates with a skeptical pass, and returns ONE report as its final message — the platform layer auto-posts it in MR context. Re-reviews scope to the diff since the last reviewed head, tracked via a hidden HTML marker in each posted report. All merge/marker/schema machinery is deleted.

**Tech Stack:** Python 3.14, Django, deepagents/LangGraph subagents, pytest (`uv run pytest`), ruff via `make lint-fix`.

## Global Constraints

- Python 3.14 only. Imports in tests have no `daiv.` prefix (`from automation.agent.subagents import ...`).
- Run tests with `uv run pytest ...` (never bare `pytest`). Full suite: `make test`.
- Lint/format with `make lint-fix` before the final commit of each task.
- Never edit `pyproject.toml`; no new dependencies are needed for this work.
- PEP 758: `except E1, E2:` (no parens) is valid and the repo style — do not "fix" it.
- Commit messages: Conventional Commits. Do NOT add `Co-Authored-By` lines or any AI footer.
- The spec is authoritative on behavior: `specs/2026-07-27-code-review-skill-restructure-design.md`. One correction to it discovered during planning: `tests/unit_tests/automation/agent/test_graph_deferred.py` is about deferred *tools*, not deferred output — leave that file untouched.
- The marker format is exactly: `<!-- daiv:code-review run=N head=<full-sha> -->` — Task 3 (SKILL.md) and Task 5 (docs) must use the identical string.
- Detector subagent names are unchanged: `cr-correctness`, `cr-security`, `cr-performance`, `cr-structure`, `cr-custom-rules`.

---

### Task 1: Charter guard test + rewrite the five detector charters

The five files under `daiv/automation/agent/skills/code-review/agents/` become fully self-contained personas. TDD: extend the shipped-charter guard tests first (they fail against the old thin charters), then write the new charters to make them pass.

**Files:**
- Modify: `tests/unit_tests/automation/agent/test_subagents.py` (class `TestShippedDetectorCharters`, lines ~919–981)
- Rewrite: `daiv/automation/agent/skills/code-review/agents/cr-correctness.md`
- Rewrite: `daiv/automation/agent/skills/code-review/agents/cr-security.md`
- Rewrite: `daiv/automation/agent/skills/code-review/agents/cr-performance.md`
- Rewrite: `daiv/automation/agent/skills/code-review/agents/cr-structure.md`
- Rewrite: `daiv/automation/agent/skills/code-review/agents/cr-custom-rules.md`

**Interfaces:**
- Consumes: `CODE_REVIEW_AGENTS_PATH`, `CODE_REVIEW_DETECTOR_NAMES`, `_parse_subagent_frontmatter` from `automation.agent.subagents` (unchanged).
- Produces: charters whose body is the *entire* per-detector system prompt (Task 2 stops prepending schema/archetype text; Task 3's orchestrator relies on the report contract defined here: findings as `### <Severity>: <title>` blocks, sentinel `No findings.`).

- [ ] **Step 1: Replace the principles-citation guard test with the charter-contract guard test**

In `tests/unit_tests/automation/agent/test_subagents.py`, inside `TestShippedDetectorCharters`:

1. DELETE `test_principle_citations_resolve_to_existing_sections` entirely (principles.md is deleted in Task 4; charters no longer cite it).
2. In `test_agents_dir_holds_exactly_the_five_cr_charters`, replace the stale docstring comment (it references `review-workflow.md`'s inline-detection fallback) with:

```python
        # SKILL.md's fan-out step and the loader both key on the literal `cr-*.md` glob under
        # agents/. Lock that it resolves to exactly the five detector charters, so renaming the
        # dir or a file (silently dropping a dimension) is caught.
```

3. ADD this test:

```python
    def test_charters_carry_precision_gate_and_report_contract(self):
        # The charters are fully self-contained (no shared references, no structured schema), so
        # the blocks that keep the prose pipeline precise live only inside each file: the >=80
        # confidence gate, the severity rubric, the never-flag rules, and the exact no-findings
        # sentinel the orchestrator keys on. A charter edit that drops one would degrade review
        # precision with no other test failing — lock the section headings and sentinels here.
        from automation.agent.subagents import CODE_REVIEW_AGENTS_PATH

        for md in sorted(CODE_REVIEW_AGENTS_PATH.glob("cr-*.md")):
            body = md.read_text(encoding="utf-8")
            assert "## Confidence gate" in body, f"{md.name} lost its confidence gate"
            assert "80" in body, f"{md.name} lost the >=80 reporting threshold"
            assert "## Severity" in body, f"{md.name} lost the severity rubric"
            for label in ("Critical", "Important", "Suggestion", "Question"):
                assert label in body, f"{md.name} lost the {label} severity label"
            assert "## Never flag" in body, f"{md.name} lost the never-flag rules"
            assert "No findings." in body, f"{md.name} lost the no-findings sentinel"

        custom = (CODE_REVIEW_AGENTS_PATH / "cr-custom-rules.md").read_text(encoding="utf-8")
        assert "**Rule:**" in custom, "cr-custom-rules lost the rule-citation field"
```

- [ ] **Step 2: Run the new test to verify it fails**

Run: `uv run pytest tests/unit_tests/automation/agent/test_subagents.py::TestShippedDetectorCharters -v`
Expected: `test_charters_carry_precision_gate_and_report_contract` FAILS (old charters have none of the sections); the other two tests PASS.

- [ ] **Step 3: Write the five charters**

The four blocks below are IDENTICAL in every charter (deliberate duplication — each file is self-contained). Insert them where the per-charter templates say `<CONFIDENCE-GATE>`, `<SEVERITY>`, `<NEVER-FLAG>`, `<REPORT-FORMAT>`.

`<CONFIDENCE-GATE>`:

```markdown
## Confidence gate

Score every candidate finding 0–100 before reporting it:

- 0–25: speculative, or you could not verify the claim in the surrounding code.
- 26–50: plausible but depends on context you did not confirm (config, callers, runtime).
- 51–79: probably real, but a plausible innocent explanation remains.
- 80–90: verified against the surrounding code — you can point to the exact line and articulate the failure or the concrete improvement.
- 91–100: certain — you could write the failing test or cite the violated rule.

**Report only findings scoring 80 or above.** Precision beats recall: a dropped true positive costs less than a false positive that erodes trust in the whole review. When in doubt, leave it out — or, if the doubt is about the author's intent, convert it into a Question.
```

`<SEVERITY>`:

```markdown
## Severity

Label every finding with exactly one severity:

- **Critical** — the change produces wrong results on common inputs, breaks authorization, loses data, or crashes. Should block the merge.
- **Important** — a likely bug, a broken contract for existing callers or consumers, or a meaningful performance regression. Should be fixed before or shortly after merge.
- **Suggestion** — a concrete structural improvement with a named fix: "use X instead of Y", "delete lines L–M", "extract to Z". If you cannot name the fix in one sentence, it does not ship.
- **Question** — the diff alone cannot tell whether this is intended; only the author can. Anchor it on a `file:line` and pose a concrete yes/no hypothesis. Questions carry no Critical/Important/Suggestion grade.
```

`<NEVER-FLAG>`:

```markdown
## Never flag

- Style, formatting, whitespace, or import ordering — a linter's or formatter's job, never yours.
- Issues that pre-date this change: you review the diff, not the codebase. If the diff merely moves an existing problem, leave it.
- Lines covered by an explicit suppression or an intentional marker (`noqa`, `pragma`, a comment explaining the choice).
- Code paths the change cannot actually reach.
```

`<REPORT-FORMAT>`:

```markdown
## Report format

Return a markdown report as your final message, and nothing else — no process narration, no preamble. For each finding:

### <Severity>: <one-line title>
- **Location:** `path/to/file.py:42` (the new-side line)
- **Why:** what breaks or misleads, in 1–3 sentences grounded in the surrounding code you read.
- **Fix:** the concrete change, as one sentence or a short fenced code block.

Order findings by severity, Critical first. If nothing clears the confidence gate, return exactly: `No findings.`
```

Now the five files. Write each exactly as templated (frontmatter descriptions are unchanged from today).

**`cr-correctness.md`:**

````markdown
---
name: cr-correctness
description: Code-review detector for logic and contract defects. Dispatch only during a code review (the code-review skill drives it); not a general-purpose agent.
---
You are the **correctness** detector in DAIV's code-review fan-out: an expert reviewer whose only job is to find defects the change introduces — code that computes the wrong thing, breaks a contract, or fails at runtime. You review one change and report logic and contract defects only; every other dimension (security, performance, structure, repo rules) belongs to a sibling detector.

## What you look for

- **Logic defects** — off-by-one boundaries, the wrong logical operator (`and`/`or`, `<`/`<=`), mutating a collection while iterating it, state initialised in one branch but read in all of them.
- **Error handling** — errors caught and swallowed without logging or re-raising, overly broad catches that hide unrelated failures, silent fallback values that mask errors instead of surfacing them, the same error logged at every layer.
- **Absent values** — unguarded dereference of a possibly-absent value, absent-value sentinels returned where an explicit error belongs, required inputs silently defaulted to zero/empty.
- **Unintended side effects** — a query-named function that mutates state, hidden global/module state coupling unrelated callers, I/O in constructors, a hook or signal now firing where it didn't before.
- **Concurrency** — shared mutable state accessed without a lock, inconsistent lock ordering, a lock held across slow I/O.
- **Migrations / schema** — a column or table removed while deployed code still reads it, a non-nullable column added without a default, an index added in the same step as a large backfill.
- **API contracts** — a public field/endpoint removed or renamed, changed semantics without versioning, a new required parameter on an existing public function.
- **Configuration / environment** — environment-specific values hardcoded, defaults that are wrong for production, config read but never validated at startup.
- **Fail-fast violations** — validation buried deep in the call stack instead of at the boundary, invalid input accepted and turned into a wrong-but-plausible result.

Read the surrounding code before you judge: trace the callers, the types, and the branch you think is wrong. Most false positives come from reading the diff alone.

<CONFIDENCE-GATE>

<SEVERITY>

<NEVER-FLAG>

<REPORT-FORMAT>

## Calibration example

### Critical: promotion email fires on every save, not only on create
- **Location:** `accounts/signals.py:24`
- **Why:** the `post_save` receiver checks `instance.role == "admin"` but never `created`, so any later edit of an admin profile re-sends the promotion email and re-writes the audit entry.
- **Fix:** guard the receiver with `if not created: return` before the role check.
````

**`cr-security.md`:**

````markdown
---
name: cr-security
description: Code-review detector for trust-boundary and exposure issues. Dispatch only during a code review (the code-review skill drives it); not a general-purpose agent.
---
You are the **security** detector in DAIV's code-review fan-out: an expert reviewer whose only job is to find trust-boundary and exposure issues the change introduces. You review one change and report security issues only; correctness, performance, structure, and repo rules belong to sibling detectors.

## What you look for

- **Input validation at trust boundaries** — external input (user-supplied, file-derived, network-received) reaching business logic unvalidated; invalid input silently coerced instead of rejected; error messages that leak internals (paths, schema names, stack frames).
- **Authorization / authentication gaps** — an endpoint or mutation that checks only authentication, not permission; resource ownership trusted from a client-supplied identifier instead of re-verified server-side; checks living only in the UI layer; permissive behavior when the authorization decision is ambiguous.
- **Secrets exposure** — credentials or tokens in source, logs, error messages, or API responses; request/response objects logged without redaction; secrets in version control or passed as command-line arguments.
- **Injection surfaces** — SQL, shell, or path fragments built by string concatenation from external input where a parameterised or library API exists.

Read the surrounding code before you judge: confirm the input really is externally reachable and the check really is absent (not performed by a decorator, middleware, or caller).

<CONFIDENCE-GATE>

<SEVERITY>

<NEVER-FLAG>

<REPORT-FORMAT>

## Calibration example

### Critical: ownership never checked on invoice download
- **Location:** `billing/views.py:58`
- **Why:** the view fetches `Invoice.objects.get(pk=pk)` for any authenticated user; nothing verifies the invoice belongs to `request.user`, so any user can read any invoice by iterating ids.
- **Fix:** filter by owner: `get_object_or_404(Invoice, pk=pk, account=request.user.account)`.
````

**`cr-performance.md`:**

````markdown
---
name: cr-performance
description: Code-review detector for performance defects (N+1, repeated work in loops). Dispatch only during a code review (the code-review skill drives it); not a general-purpose agent.
---
You are the **performance** detector in DAIV's code-review fan-out: an expert reviewer whose only job is to find performance defects the change introduces. You review one change and report performance issues only; correctness, security, structure, and repo rules belong to sibling detectors.

## What you look for

- **N+1 and loop-carried remote work** — a data-store query inside a loop over a list where one batched/parameterised query would do; cache, filesystem, or network calls inside a loop whose result does not change per iteration; N per-iteration writes where one batch write suffices.
- **Loop-invariant work** — allocations, compilations, serialisations, or lookups hoistable out of a tight loop.
- **Blocking on the hot path** — synchronous blocking calls on the main execution path that belong on a background worker or async path.
- **Algorithmic hazards** — O(n²) or worse over user-controlled input; repeated serialisation/deserialisation of the same immutable value that should be cached once.

Only flag work that is actually repeated or actually hot: confirm the loop bound is data-driven and the call really goes to a store/network, not an in-memory map. A micro-inefficiency on a cold path is not a finding.

<CONFIDENCE-GATE>

<SEVERITY>

<NEVER-FLAG>

<REPORT-FORMAT>

## Calibration example

### Important: per-member query inside the roster loop
- **Location:** `teams/services.py:71`
- **Why:** `for member in team.members.all(): member.profile.department` issues one query per member (the profile relation is not selected); rosters run to hundreds of members on the largest teams.
- **Fix:** fetch relations up front: `team.members.select_related("profile__department")`.
````

**`cr-structure.md`:**

````markdown
---
name: cr-structure
description: Code-review detector for maintainability and readability issues. Dispatch only during a code review (the code-review skill drives it); not a general-purpose agent.
---
You are the **structure** detector in DAIV's code-review fan-out: an expert reviewer whose only job is to find maintainability and readability issues the change introduces. You review one change and report structural issues only; correctness, security, performance, and repo rules belong to sibling detectors.

## What you look for

- **Dead code** — statements no path reaches, variables/parameters/imports declared but never used, commented-out blocks, leftover scaffolding and debug helpers.
- **Wrong placement** — logic in a layer that doesn't own its subject; caller-owned configuration hardcoded inside a helper; infrastructure reached directly instead of received as a dependency.
- **Missed framework/library idiom** — hand-rolled logic the standard library, the framework, or an already-imported dependency ships as a tested one-liner.
- **Misleading naming** — only when it materially misleads: a name that promises one thing while the body does another, a boolean that doesn't read as a predicate. Mere blandness is not a finding.
- **Duplication** — blocks logically identical up to literal values that belong in one parameterised function; the same invariant guarded in multiple places.
- **Magic values** — a literal repeated or opaque enough that future changes will miss a site; status codes and thresholds embedded in logic.
- **Typing / signatures** — a signature accepting far more than is valid, or a return type that lies about error/absent paths.
- **Logging** — messages without the context to act on them, wrong severity, sensitive data in log output.
- **i18n / a11y** — user-visible text outside the translation system, manual plural/date/number assembly; interactive elements without labels, colour as the only signal, keyboard-unreachable flows.

Read the surrounding module before you judge: the "duplicate" may be the established local pattern, and the "misplaced" logic may match the project's layering.

<CONFIDENCE-GATE>

<SEVERITY>

<NEVER-FLAG>

<REPORT-FORMAT>

## Calibration example

### Suggestion: hand-rolled query-string builder duplicates a stdlib call
- **Location:** `integrations/http.py:33`
- **Why:** the new `build_query()` loops and urlencodes pairs by hand — `urllib.parse.urlencode` does exactly this, tested, including sequence values.
- **Fix:** replace the function body with `return urllib.parse.urlencode(params)` (or inline it at the two call sites and delete the helper).
````

**`cr-custom-rules.md`:**

````markdown
---
name: cr-custom-rules
description: Code-review detector that enforces a repo's custom review rules. Dispatch only during a code review and only when a rule source exists; not a general-purpose agent.
---
You are the **custom-rules** detector in DAIV's code-review fan-out: an expert reviewer whose only job is to enforce the repository's own review rules against the change. You report rule violations only; generic correctness, security, performance, and structure belong to sibling detectors.

## Your rule sources

Beyond the standard scope, your dispatch prompt gives you the **paths** of the rule sources that exist (not their contents) — read them yourself:

- `.agents/review-rules.md` is **authoritative** (binding).
- `AGENTS.md` / `.agents/AGENTS.md` are **supplementary** — mine them only for concrete, diff-checkable rules (naming, layering/boundaries, required or forbidden patterns); ignore build/test/setup prose and vague aspirational lines.
- If the sources conflict, `review-rules.md` wins.

A finding must trace to a specific written rule. If the diff merely looks unusual but no rule covers it, it is not your finding.

<CONFIDENCE-GATE>

<SEVERITY>

A violation of a binding rule is at least **Important**; use **Critical** when the violated rule guards correctness, security, or data integrity.

<NEVER-FLAG>

<REPORT-FORMAT>

Every finding additionally cites its rule, as the first bullet:

- **Rule:** `<source file>: <the rule, quoted or tightly paraphrased>`

## Calibration example

### Important: external call without a timeout in payments/
- **Rule:** `review-rules.md: every external call in payments/ must set a timeout`
- **Location:** `payments/gateway.py:88`
- **Why:** the new `requests.post(...)` sets no `timeout`, so a hung gateway blocks the worker indefinitely — exactly what the rule exists to prevent.
- **Fix:** pass `timeout=settings.PAYMENT_GATEWAY_TIMEOUT` (used by the other calls in this module).
````

- [ ] **Step 4: Run the charter tests to verify they pass**

Run: `uv run pytest tests/unit_tests/automation/agent/test_subagents.py::TestShippedDetectorCharters -v`
Expected: all PASS (including `test_all_five_detectors_present_and_wellformed`, which re-parses the new frontmatter).

- [ ] **Step 5: Commit**

```bash
git add daiv/automation/agent/skills/code-review/agents/ tests/unit_tests/automation/agent/test_subagents.py
git commit -m "refactor(code-review): rewrite detector charters as self-contained prose reporters"
```

---

### Task 2: Runtime — slim the preamble, drop structured output and deferred output

`subagents.py` stops forcing `response_format` on detectors (killing the `tool_choice="any"` footgun) and stops deferring their output to files; `DeferredOutputMiddleware` is deleted (verified: its only construction site is `_build_detector_middleware`).

**Files:**
- Modify: `daiv/automation/agent/subagents.py` (preamble ~lines 52–69, `_build_detector_middleware` ~194–237, `_load_detector_response_format` 240–253, `load_builtin_code_review_detectors` 256–359, `_shared_subagent_middleware` comment ~127–132, imports)
- Delete: `daiv/automation/agent/middlewares/deferred_output.py`
- Delete: `tests/unit_tests/automation/agent/middlewares/test_deferred_output.py`
- Modify: `daiv/automation/agent/constants.py` (remove `SUBAGENT_OUTPUT_PATH`, ~line 22, and its comment ~line 21)
- Modify: `daiv/automation/agent/middlewares/loop_breaker.py` (docstring ~line 82)
- Modify: `daiv/automation/agent/middlewares/skills.py` (comments ~lines 221, 249–250 citing findings.py/marker.py as examples)
- Modify: `tests/unit_tests/automation/agent/test_subagents.py` (classes `TestDetectorMiddleware`, `TestShippedDetectorCharters` preamble test, `TestBuiltinCodeReviewDetectors`)
- Modify: `tests/unit_tests/automation/agent/middlewares/test_skills.py` (docstring at ~line 185 citing findings.py)

**Interfaces:**
- Consumes: charters from Task 1 (loader now compiles frontmatter + body verbatim after the slimmed preamble).
- Produces: `load_builtin_code_review_detectors(model, backend, runtime, working_directory, sandbox_enabled=True, fallback_models=None, client=None, sandbox_backend=None, *, agents_dir=CODE_REVIEW_AGENTS_PATH)` — the `schema_path` keyword is REMOVED. `graph.py`'s call site (line ~273) passes no `schema_path`, so it needs no change. `SHARED_DETECTOR_PREAMBLE` remains a public constant (tests import it).

- [ ] **Step 1: Update the detector tests to the new contract (write failing tests first)**

In `tests/unit_tests/automation/agent/test_subagents.py`:

1. In `TestDetectorMiddleware`: DELETE `test_includes_deferred_output_middleware` and `test_deferred_output_runs_before_sandbox_teardown`. In the four remaining tests of the class (`test_filesystem_is_read_only`, `test_includes_sandbox_but_not_git_platform_or_web`, `test_excludes_sandbox_when_disabled`, `test_threads_client_and_sandbox_backend_into_sandbox_middleware`), REMOVE the `name="cr-correctness"` kwarg from every `_build_detector_middleware(...)` call — Step 4 removes that parameter (it only existed to label the deferred output file). ADD:

```python
    def test_no_deferred_output_middleware(self, mock_model, mock_backend):
        # Detectors return their prose report inline as the task result; nothing may divert the
        # final message to a file. Guards against reintroducing DeferredOutputMiddleware (removed
        # with the structured-findings pipeline).
        from automation.agent.subagents import _build_detector_middleware

        middleware = _build_detector_middleware(mock_model, mock_backend, sandbox_enabled=True)
        assert not any(type(m).__name__ == "DeferredOutputMiddleware" for m in middleware)
```

2. In `TestShippedDetectorCharters.test_shared_preamble_carries_read_only_bash_directive`, keep the existing two asserts and ADD (same test):

```python
        assert "archetype" not in body, "slimmed preamble must not mention the deleted archetype enum"
        assert "final message" in body, "preamble must state the report-is-final-message contract"
```

3. In `TestBuiltinCodeReviewDetectors`: DELETE `test_response_format_wraps_finding_schema`, `test_returns_empty_when_schema_missing`, and `test_returns_empty_when_schema_corrupt`. REPLACE `test_detectors_compiled_with_structured_response_format` with:

```python
    def test_detectors_compiled_without_response_format(self, tmp_path, mock_model, mock_backend, mock_runtime_ctx):
        # Detectors are prose reporters: a response_format would force tool_choice="any", remove
        # the natural text stop, and re-open the runaway-loop failure mode the structured pipeline
        # had. Assert no compiled detector carries one.
        from automation.agent.subagents import load_builtin_code_review_detectors

        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "cr-correctness.md").write_text(
            _make_subagent_md(name="cr-correctness", description="Correctness detector", body="Find correctness bugs.")
        )
        (agents_dir / "cr-security.md").write_text(
            _make_subagent_md(name="cr-security", description="Security detector", body="Find security bugs.")
        )

        with patch("automation.agent.subagents.create_agent") as mock_create:
            mock_create.return_value = Mock()
            load_builtin_code_review_detectors(
                mock_model,
                mock_backend,
                mock_runtime_ctx,
                working_directory="/workspace/repo/",
                sandbox_enabled=False,
                agents_dir=agents_dir,
            )

        assert mock_create.call_count == 2
        assert all(call.kwargs.get("response_format") is None for call in mock_create.call_args_list)
```

- [ ] **Step 2: Run the updated tests to verify the new ones fail**

Run: `uv run pytest tests/unit_tests/automation/agent/test_subagents.py -v -k "detector or Charter or Detectors"`
Expected: `test_no_deferred_output_middleware`, the extended preamble test, `test_detectors_compiled_without_response_format`, and the four `TestDetectorMiddleware` tests (now calling without `name=`, a required kwarg until Step 4) all FAIL; the rest PASS.

- [ ] **Step 3: Rewrite the preamble and its header comment in `subagents.py`**

Replace the comment block + constant (current lines ~54–69) with:

```python
# Prepended to every cr-* detector's charter at compile time (load_builtin_code_review_detectors).
# Holds the parts that are identical across all detectors — how the change is delivered and read,
# the read-only contract, and the final-message-is-the-report contract — so each charter file
# carries its own dimension, precision gate, and report format without restating the plumbing.
# One source instead of five copies. The read-only contract is prompt-layer enforcement: the
# detector sandbox is a full bash shell with no per-subagent command policy, so this is the only
# thing stopping a detector from mutating the shared workspace via bash — keep it here.
SHARED_DETECTOR_PREAMBLE = """You are one of DAIV's code-review fan-out detectors. The procedure below is shared by every detector; the dimension you own — and the findings you may report — are defined after it.

You will be given the change's scope: source/target refs, the head SHA, the new-side path scope, and the path to a pre-computed unified diff file. **Read that diff file** to see the change. If no diff path was provided or the file is unreadable, fall back to reconstructing the change yourself — run `git diff <target>...<source>`, or, when `bash` is unavailable (a disk-backed run with no sandbox), read the changed files directly with `read_file`/`grep` over the new-side path scope. Either way, read surrounding code for context before deciding; context is what keeps false positives down.

**You are read-only.** Use `bash` only for read-only inspection: `git diff`/`show`/`log`/`status`, `grep`, `find`, `cat`, and read-mode `sed`/`awk` (never `sed -i`). Never mutate the workspace — no output redirects (`>`, `>>`, `tee`), no `sed -i` / `python -c` writes, no formatters, tests, builds, or package managers, and no `git add`/`commit`/`checkout`/`reset`/`restore`/`clean`. If confirming a finding would need code execution, raise it as a Question finding instead of running it.

Your **final message is the deliverable**: a markdown report in the exact shape your charter defines, returned directly to the review orchestrator. Return the report and nothing else — no process narration, no preamble."""  # noqa: E501
```

Note the scope wording change: "the head SHA" replaces "the SHA triplet" (inline positioning is gone; only the head matters).

- [ ] **Step 4: Remove response_format and deferred-output wiring in `subagents.py`**

1. Delete the whole `_load_detector_response_format` function (lines ~240–253) and the `CODE_REVIEW_FINDING_SCHEMA_PATH` constant (line 54). If `json` / `Path` imports become unused, remove them (ruff will flag).
2. Delete the import `from automation.agent.middlewares.deferred_output import DeferredOutputMiddleware` (line ~17) and drop `SUBAGENT_OUTPUT_PATH` from the `automation.agent.constants` import (line ~16).
3. In `_build_detector_middleware`: delete the trailing `DeferredOutputMiddleware(...)` append AND its "Keep this last..." comment block (lines ~230–235). The `name` keyword-only parameter is now unused — remove `*, name: str` from the signature and drop `name=frontmatter["name"]` from the call in `load_builtin_code_review_detectors`; update the docstring (drop any deferred-output mention).
4. In `load_builtin_code_review_detectors`: remove the `schema_path` parameter, the `try: response_format = ...` block (lines ~284–292), and `response_format=response_format` from the `_compile_subagent` call. Update the docstring: detectors compile "with a read-only middleware stack; each returns its markdown report as its final message" (drop the response_format sentence).
5. In `load_builtin_code_review_detectors`, update the comment above `body=f"{SHARED_DETECTOR_PREAMBLE}\n\n{body}"` — it still describes the preamble as carrying the "scope/read-only/archetype preamble"; change to "scope/read-only/final-message preamble".
6. In `_shared_subagent_middleware`, rewrite the `LoopBreakerMiddleware` comment (lines ~127–132): cr-* detectors are no longer forced to `tool_choice="any"`. New comment:

```python
        # Subagents compiled with a structured response_format (custom subagents may carry one)
        # are forced to tool_choice="any" and have no natural stop; and any subagent can pattern-
        # lock regardless. On a stuck loop the breaker finalizes the subagent with an explicit
        # ERROR message (NOT a raise — a raised exception would propagate out of the task tool's
        # ToolNode and abort the whole parent run). The error message flows back as the task
        # result, so the orchestrator sees a failed subagent, not an empty/absent report.
```

- [ ] **Step 5: Delete the middleware and its test; clean the stragglers**

```bash
git rm daiv/automation/agent/middlewares/deferred_output.py
git rm tests/unit_tests/automation/agent/middlewares/test_deferred_output.py
```

1. `daiv/automation/agent/constants.py`: delete `SUBAGENT_OUTPUT_PATH = f"{TMP_PATH}/subagent-output"` and its preceding comment line ("# consumed by the orchestrator (e.g. code-review's findings.py merge)...").
2. `daiv/automation/agent/middlewares/loop_breaker.py` docstring (~line 82): it says the error message reaches the parent "as the deferred-output text (`DeferredOutputMiddleware` writes the last...)". Rewrite that clause to: "as the task result text the parent reads."
3. `daiv/automation/agent/middlewares/skills.py` comments (~lines 221, 249–250): they cite "the code-review skill depends on scripts/findings.py and scripts/marker.py" as the example of a runtime asset. Reword to a living example, e.g. "the skill-creator skill depends on scripts/init_skill.py at runtime". Same for the test docstring in `tests/unit_tests/automation/agent/middlewares/test_skills.py` (~line 185).
4. `daiv/automation/agent/middlewares/git_platform.py` (~lines 684–686): the comment says the code-review skill's `gitlab-delivery.md` Step 1 keys its empty-listing handling off the "no file was written" phrasing. That reference file is deleted and the new SKILL.md does not key off the phrasing — trim the comment to end after "not a failed command." (keep the empty-result behavior itself untouched).
4. Confirm nothing else references the deleted names:

```bash
grep -rn "DeferredOutputMiddleware\|SUBAGENT_OUTPUT_PATH\|_load_detector_response_format\|CODE_REVIEW_FINDING_SCHEMA_PATH" daiv/ tests/ --include="*.py"
```

Expected: no matches.

- [ ] **Step 6: Run the affected test files**

Run: `uv run pytest tests/unit_tests/automation/agent/test_subagents.py tests/unit_tests/automation/agent/middlewares/test_skills.py tests/unit_tests/automation/agent/test_graph_deferred.py -v`
Expected: ALL PASS (`test_graph_deferred.py` is deferred-*tools* coverage and must be untouched and green).

- [ ] **Step 7: Lint and commit**

```bash
make lint-fix
git add -A daiv/automation tests/unit_tests/automation daiv/automation/agent/constants.py
git commit -m "refactor(agent): compile code-review detectors as prose reporters"
```

---

### Task 3: Rewrite the orchestrator `SKILL.md`

One file replaces the router + `review-workflow.md` + `gitlab-delivery.md`. The references it superseded are deleted in Task 4 — after this task, SKILL.md must not mention any `references/` or `scripts/` path.

**Files:**
- Rewrite: `daiv/automation/agent/skills/code-review/SKILL.md`

**Interfaces:**
- Consumes: detector names/charters (Task 1); the shared-diff-file contract from the preamble (Task 2): detectors expect refs, head SHA, path scope, diff file path.
- Produces: the marker format `<!-- daiv:code-review run=N head=<full-sha> -->` and the report layout that Task 5's docs describe.

- [ ] **Step 1: Write the new SKILL.md**

Keep the frontmatter `description` **byte-identical** to the current one (it drives skill triggering). Full new content:

````markdown
---
name: code-review
description: This skill should be used when a user asks for a code review, feedback on a PR or MR, diff assessment, or says things like 'can you review my changes', 'look at this diff', 'is this ready to merge', 'check my code', 'review this branch', 'what do you think of these changes', or 'LGTM check'. Covers correctness, performance, security, structural concerns, repo-specific review rules, and questions of intent on pull/merge requests or raw diffs from any platform (GitHub, GitLab).
metadata:
  version: 4.0.0
---

# Code Review

Review a change by fanning out specialized `cr-*` detector subagents, then aggregate their reports into **one review report returned as your final message**. You never post the report yourself: in merge-request context the platform layer posts your final message to the MR automatically as a new discussion.

## Step 1 — Mode and previous reviews

- **Delivery mode** — the runtime has merge-request context (`Scope.MERGE_REQUEST` with a `merge_request_id`) and the platform is GitLab. Your final message is auto-posted to the MR; dress the report with the marker, run number, and footer (Step 6).
- **Interactive mode** — anything else: a local diff, a referenced MR/PR with no runtime context, a GitHub PR, or ambiguous scope. Your final message is simply the reply — no marker, no run number, no footer.

In delivery mode, find what was already reviewed. Load the `gitlab` tool (`tool_search` for it if it isn't loaded) and list the MR's discussions/notes. Previous review reports embed a hidden marker:

```
<!-- daiv:code-review run=N head=<full-sha> -->
```

Take the marker with the highest `run`: its `head` is the **last reviewed head**, and your run number is that maximum plus one. No markers found → this is review run 1. If the notes cannot be read at all (tool won't load, API error), treat it as run 1 with **no run number in the header**, and review the full change.

## Step 2 — Review scope (incremental)

- **First review:** the full MR change — `git diff <target>...<source>`.
- **Re-review:** only what changed since the last review — `git diff <last_head>...<head>`, restricted to paths that are also in the full MR diff (`git diff <target>...<source> --name-only`); the restriction keeps target-branch merge-ins out of scope.
- **Before using `last_head`**, verify it: `git cat-file -e <last_head>` and `git merge-base --is-ancestor <last_head> <head>`. If either fails (force-push, rebase), review the full MR change instead and open the report body with one sentence saying so.
- **Head unchanged** (`head` equals `last_head`): there is nothing to review. Your final message is one short line — "Already reviewed at `<short-sha>` — no new commits since review #N." — with **no marker**. Stop.
- **Interactive mode:** derive scope from the conversation (a pasted diff is a scope aid only — always diff the checked-out refs yourself). A re-review within the same conversation covers what changed since the previous review, from conversation context. If scope is ambiguous, ask.

## Step 3 — Applicable detectors

Inspect the scoped diff (`--name-only` plus a skim of the hunks) and pick the detectors that apply:

| Detector | Dispatch when |
|---|---|
| `cr-correctness` | any code file changed |
| `cr-structure` | any code file changed |
| `cr-security` | the diff touches trust boundaries: request/input handling, endpoints/views, auth/permissions, secrets or config, SQL/subprocess/file-path construction, dependency manifests, CI/Docker files |
| `cr-performance` | the diff touches loops over collections, DB/ORM queries, network calls, caching, or async/concurrency code |
| `cr-custom-rules` | a rule source exists on disk: `.agents/review-rules.md`, `AGENTS.md`, or `.agents/AGENTS.md` — pass it the paths that exist |

**Bias to inclusion:** when unsure whether a dimension applies, dispatch it. If the diff contains no code at all (docs/assets only), dispatch only `cr-custom-rules` (if rules exist); with no rules either, your report body is a one-line "nothing applicable to review in this change" (this still counts as a completed review — marker included in delivery mode).

## Step 4 — Fan out

Write the scoped diff once so every detector reviews the identical change:

```
git diff <...scope from Step 2...> > /workspace/tmp/review-change.diff
```

If the write fails, dispatch anyway — detectors fall back to running `git diff` themselves.

Dispatch the applicable detectors **in parallel** — one `task` call per detector, all in a single turn, `subagent_type` set to the detector's name. The prompt carries **scope only**: source/target refs, the head SHA, the shared diff file path, and the new-side path scope (plus, for `cr-custom-rules`, the rule-source paths). Never restate a detector's charter, and never describe its output — charters define both.

- **Never dispatch detection to `general-purpose`** (or any other type): if a `cr-*` type is missing from the `task` tool's agent list, it failed to load — skip it and mention the uncovered dimension in the report body. Never substitute.
- If parallel dispatch is rejected, dispatch sequentially. If a detector's `task` call errors, continue with the rest and mention the uncovered dimension in the report body.
- If **every** detector fails, do not fabricate a review: your final message reports the failure (no marker — the scope was not reviewed).

## Step 5 — Aggregate (skeptical pass)

Read the detector reports (each is markdown findings or the literal `No findings.`). While assembling the report, adjudicate each finding — drop it if:

- it pre-dates this change (visible in the diff context or file history);
- it misreads the control flow or context (verify against the code when unsure);
- it is a style/formatting/whitespace/import-ordering nit — never ship those;
- the code path isn't actually reachable.

You may downgrade a finding's severity when the detector overstated impact. Deduplicate across detectors by judgment: same file, same line, same underlying issue → keep the strongest framing once. Keep only Questions that anchor a `file:line` and pose a concrete yes/no hypothesis. Over-pruning is acceptable — precision over recall. Present only confirmed survivors; no strikethrough, no "on closer reading this is fine".

## Step 6 — The report (your final message)

Delivery-mode layout — the marker is the FIRST line, `N` is the run number from Step 1, `<full-sha>` is the head you reviewed:

```markdown
<!-- daiv:code-review run=N head=<full-sha> -->
## Code Review #N

### Critical Issues
**1. <one-line title>** — `path/to/file.py:42`

<details>
<summary>Details</summary>

Why it's a problem (grounded in the code), then the concrete fix — as prose or a fenced code block.

</details>

### Important Issues
…

### Suggestions
…

### Questions
…

### Recommended Actions
1. <merge-blocking items first, then the rest — one line each>

---
_Reply in this discussion and mention `@<bot-username>` to ask about a finding or have DAIV apply a fix._
```

Rules:

- Omit any section with no entries. Number findings sequentially within each section.
- **No findings at all:** keep the marker and header, body is "No findings — the reviewed changes look good."; omit Recommended Actions; keep the footer.
- Force-push fallback (Step 2) or uncovered dimensions (Step 4): one italic sentence each, directly under the header.
- Omit Recommended Actions when there are no Critical/Important findings.
- `<bot-username>` is DAIV's real account username (from the runtime context or the `gitlab` tool's current user) — never a hardcoded guess.
- **Interactive mode:** header is `## Code Review` (add `#N` only when re-reviewing within the conversation); no marker, no footer. Use the file-reference link format from the system prompt's Code References section.

## Non-negotiables

- **Precision over recall.** Only confirmed survivors ship; over-pruning is acceptable.
- **Never post style, formatting, whitespace, or import-ordering findings.** That's a linter's job.
- **Detectors run as `cr-*` subagents, never `general-purpose`.** A missing detector is a reported gap, never a substitution.
- **The final message is the deliverable.** Never post the report through the `gitlab` tool — in delivery mode it is posted automatically, and a manual post would duplicate it.
- **Markers only on completed reviews.** "Already reviewed", failure messages, and interactive replies never carry a marker.
- **Never re-invoke the `skill` tool to restart a review.** On a tool failure, switch to an alternative and continue (platform tool instead of `bash git diff`, sequential instead of parallel dispatch).
````

- [ ] **Step 2: Verify no stale paths and the skill still loads**

```bash
grep -n "references/\|scripts/\|examples/\|findings.py\|marker.py\|archetype\|SHA triplet" daiv/automation/agent/skills/code-review/SKILL.md
```

Expected: no matches.

Run: `uv run pytest tests/unit_tests/automation/agent/middlewares/test_skills.py -v`
Expected: PASS (skill manifest parsing is generic; this catches frontmatter breakage).

- [ ] **Step 3: Commit**

```bash
git add daiv/automation/agent/skills/code-review/SKILL.md
git commit -m "refactor(code-review): replace staged pipeline with single-file orchestrator"
```

---

### Task 4: Delete the superseded machinery

Everything the new shape replaced: references, examples, scripts, and their tests. After this task the skill directory contains exactly `SKILL.md` + `agents/`.

**Files:**
- Delete: `daiv/automation/agent/skills/code-review/references/` (all 5 files)
- Delete: `daiv/automation/agent/skills/code-review/examples/`
- Delete: `daiv/automation/agent/skills/code-review/scripts/` (findings.py, marker.py, finding.schema.json; `__pycache__` is untracked)
- Delete: `tests/unit_tests/automation/agent/skills/code_review/test_findings.py`
- Delete: `tests/unit_tests/automation/agent/skills/code_review/test_marker.py`

**Interfaces:**
- Consumes: Tasks 1–3 already removed every runtime/test/prompt reference to these files.
- Produces: nothing — pure deletion.

- [ ] **Step 1: Delete**

```bash
git rm -r daiv/automation/agent/skills/code-review/references \
          daiv/automation/agent/skills/code-review/examples \
          daiv/automation/agent/skills/code-review/scripts \
          tests/unit_tests/automation/agent/skills/code_review/test_findings.py \
          tests/unit_tests/automation/agent/skills/code_review/test_marker.py
rm -rf daiv/automation/agent/skills/code-review/scripts  # clears untracked __pycache__ leftovers
```

Keep `tests/unit_tests/automation/agent/skills/code_review/__init__.py` only if other tests remain in that package; if the directory now holds just `__init__.py`, `git rm` it too.

- [ ] **Step 2: Sweep for stale references repo-wide**

```bash
grep -rn "review-workflow.md\|gitlab-delivery.md\|few-shot-examples\|marker-format\|principles.md\|findings.py\|marker.py\|finding.schema" \
  daiv/ tests/ docs/ AGENTS.md DESIGN.md CONTRIBUTING.md --include="*" | grep -v "Binary"
```

Expected: matches only in `docs/` and `AGENTS.md` (fixed in Task 5) and in `specs/`/`plans/`/`CHANGELOG.md` history (fine). Anything else → fix it now.

- [ ] **Step 3: Run the full unit suite**

Run: `make test`
Expected: PASS, no errors from deleted modules (collection errors here mean a missed import).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor(code-review): delete findings/marker machinery and reference files"
```

---

### Task 5: AGENTS.md, docs, changelog, final verification

**Files:**
- Modify: `AGENTS.md` (two invariants)
- Modify: `docs/reference/agent-architecture.md` (~line 152 + the earlier bullet ~line 10)
- Modify: `docs/features/pull-request-assistant.md` (custom-rules section ~lines 85–97; add review-report behavior)
- Check/modify: `docs/customization/agent-skills.md`, `docs/features/subagents.md` (grep-driven)
- Modify: `CHANGELOG.md` (rewrite the existing unreleased `/code-review` bullet)

**Interfaces:**
- Consumes: marker format and report layout from Task 3; runtime facts from Task 2.
- Produces: nothing downstream — final task.

- [ ] **Step 1: Rewrite the AGENTS.md invariants**

1. Replace the **"Code-review detector output"** invariant paragraph with:

```markdown
**Code-review detector output** — the `cr-*` detectors are prose reporters: each returns a
markdown report (findings ordered by severity, or the literal `No findings.`) as its final
message, which the `task` tool hands back to the review orchestrator directly — no structured
`response_format`, no deferred output files. Each charter under
`daiv/automation/agent/skills/code-review/agents/` is fully self-contained (severity rubric,
≥80 confidence gate, never-flag rules, report format); `SHARED_DETECTOR_PREAMBLE` in
`subagents.py` prepends only the shared plumbing — diff-file protocol and the read-only
contract, which is the sole guard against a detector mutating the shared workspace
(`test_charters_carry_precision_gate_and_report_contract` locks the charter blocks).
```

2. In the **"Skill asset paths"** invariant, replace the reference example — `daiv/automation/agent/skills/code-review/scripts/marker.py` no longer exists; point at `daiv/automation/agent/skills/skill-creator/scripts/init_skill.py` instead. Keep the rest of the invariant as-is.

- [ ] **Step 2: Update the docs**

1. `docs/reference/agent-architecture.md` — rewrite the detector sentence (~line 152) to match the new pipeline (prose reports, applicability filter, no structured findings):

```markdown
In addition, a set of read-only **code-review detector subagents** (`cr-correctness`, `cr-security`, `cr-performance`, `cr-structure`, `cr-custom-rules`) is built and registered on every run. The [code review](../features/pull-request-assistant.md) skill picks the detectors applicable to the change, fans out across them in parallel, and aggregates their markdown reports into a single review report; each detector runs with a read-only tool stack. [Custom subagents](../features/subagents.md#custom-subagents) defined per repository are also added to the available-agents list.
```

The bullet at ~line 10 ("a fan-out of read-only `cr-*` code-review detector subagents") stays accurate — leave it.

2. `docs/features/pull-request-assistant.md` — in the code-review area (around the "Custom review rules" section, lines ~85–97): the custom-rules prose stays accurate except the last line — reword "Every custom-rule finding passes the same false-positive checks as built-in findings" to "Every custom-rule finding passes the same confidence gate and skeptical aggregation as built-in findings". Then ADD a short subsection (place it before "Custom review rules") describing the review-report behavior:

```markdown
## Review reports

Each review is posted as a single discussion on the merge request — findings grouped by
severity (Critical / Important / Suggestions), open questions for the author, and a short
list of recommended actions. Reviews **stack**: a re-review posts a new report covering only
the commits since the previous one (after a force-push, the next report covers the full
change again and says so). Reply to a report's discussion and mention DAIV to ask about a
finding or have it apply a fix.
```

3. Grep-check the remaining pages and fix any stale mention of inline comments / structured findings / deleted files:

```bash
grep -n -i "inline\|findings\|marker" docs/features/pull-request-assistant.md docs/customization/agent-skills.md docs/features/subagents.md
```

- [ ] **Step 3: Rewrite the changelog entry**

`CHANGELOG.md` has an existing **Unreleased → Changed** bullet describing the v3 pipeline ("The `/code-review` skill now runs as a detector fan-out pipeline … posts inline plus summary review comments."). Since it is still unreleased, REWRITE that bullet in place (do not append a second, contradicting one):

```markdown
- **Breaking:** The `/code-review` skill was restructured: detectors (correctness, security, performance, structure, per-repo custom rules from `.agents/review-rules.md`) are now self-contained subagents selected by what the change touches, and each review is delivered as a single summary report discussion on the MR — inline per-line comments are no longer posted. Reviews stack: re-reviews cover only the commits since the previous report and post a new discussion.
```

- [ ] **Step 4: Final verification**

```bash
make lint-fix
make lint-typing   # pre-existing Django-descriptor errors are expected; no NEW error classes
make test
```

Expected: lint clean, tests all pass.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md docs/ CHANGELOG.md
git commit -m "docs: describe restructured code-review skill (summary reports, incremental re-reviews)"
```
