# Provider Failure Messages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a provider stops a run (out of credits, spending limit, bad key, rate limit, outage), the user sees
a fixed, provider-named message on every failure surface. Admins also see a banner while a provider needs their
action.

**Architecture:** A callback attached by `get_model` to every chat model classifies provider errors. It tags the
exception with `(kind, provider_slug)` and keeps a health mark on the `Provider` row. The executor copies the tag
onto the `Run`. Each surface renders the fixed message from the tag (live) or from the `Run` (reload). A core
context processor feeds an admin-only banner from the cached provider rows.

**Tech Stack:** Django 6, LangChain core callbacks, openai / anthropic / google-genai SDK exceptions, AG-UI
events, Alpine/Tailwind templates.

**Spec:** `specs/004-provider-failure-messages/design.md`

## Global Constraints

- Users never see raw provider or exception text. Only `failure_message()` output (Task 1), with the provider's
  display name filled in.
- Unknown error → `classify_provider_error` returns `None` → today's generic text stays, byte for byte.
- The health callback never raises into a model call and never changes which exception the call raises.
- Admin kinds (banner + health mark): `out_of_credits`, `spend_limit_reached`, `auth_failed`. User-only kinds:
  `rate_limited`, `provider_unavailable`.
- A mark expires 24 h after `last_failed_at` (`PROVIDER_FAILURE_TTL = timedelta(hours=24)`).
- Messages use gettext with `%(provider)s` placeholders. Message copy is fixed in Task 1.
- Org comment policy: no comments by default; at most 2 lines, only for what a reader would otherwise get wrong.
- Async tests that write to the DB use `@pytest.mark.django_db(transaction=True)`. Plain `django_db` leaks
  committed rows across tests.
- Run Python tools through `uv run`. Fix lint only on your own paths
  (`uv run --only-group=dev ruff check --fix <paths>`); don't run repo-wide `make lint-fix`.
- Commits: Conventional Commits, no `Co-Authored-By` line.

## Review Focus

1. **An OpenRouter 403 guardrail/moderation block** (no budget `limit_source`) stays unclassified. It must never
   be labelled a spending-limit or key problem. → Task 2 test.
2. **Primary model gets a 402, a cheaper model on the same provider row then succeeds.** The `out_of_credits` mark
   must survive. → Task 3 test.
3. **An apostrophe in a message posted as an issue/MR comment** ("couldn't") reaches GitLab/GitHub unescaped, not
   as `&#x27;`. The `.txt` templates autoescape. → Task 7 test.
4. **A user stops a chat run after a provider error was tagged.** The transcript keeps the grey "Stopped by user."
   marker, not the provider message. → Task 6 test.
5. **The health DB write fails** (DB down, locked table). The model call still raises the original provider
   exception, untouched apart from the tag. → Task 3 test.

## File map

| File | Responsibility |
|---|---|
| `daiv/core/models.py` | `ProviderFailureKind`, `PROVIDER_FAILURE_TTL`, `Provider` health fields, `Provider.Cached` fields + `active_failure_kind`, `Provider.display_name_for`, save clears the mark |
| `daiv/core/provider_failures.py` (new) | `ADMIN_KINDS`, `ProviderFailure`, `failure_message`, `active_provider_failures` |
| `daiv/automation/agent/provider_errors.py` (new) | `classify_provider_error`, `tag_provider_failure`, `provider_failure_of` |
| `daiv/automation/agent/provider_health.py` (new) | `ProviderHealthCallback`, `mark_provider_failure`, `clear_provider_failure` |
| `daiv/automation/agent/base.py` | `get_model` attaches the callback |
| `daiv/sessions/models.py` | `Run.failure_kind`, `Run.failure_provider` |
| `daiv/sessions/executor/run.py` | record the tag on the run at both failure points |
| `daiv/sessions/live/publisher.py` | public `RootOutcome`; `sanitize_run_error(event, error)` |
| `daiv/chat/api/streaming.py` | capture the root error; provider RUN_ERROR; chain `_ReportedRunError` |
| `daiv/sessions/transcript.py` | provider marker on reload |
| `daiv/notifications/tasks.py`, `daiv/jobs/api/views.py`, `daiv/mcp_api/server.py` | provider message as summary / `error` |
| `daiv/webhooks/managers/{issue,review}_addressor.py` + templates | provider message in the unable-to-address note |
| `daiv/core/context_processors.py`, `daiv/core/templates/core/_provider_failure_banner.html`, `daiv/dashboard/templates/base_app.html`, settings | admin banner |

---

### Task 1: Core — failure kinds, provider health fields, messages

**Files:**
- Modify: `daiv/core/models.py` (near `ProviderType` at :87; `Provider` at :1042-1220)
- Create: `daiv/core/provider_failures.py`
- Create: `daiv/core/migrations/0027_provider_failure.py` (generated)
- Test: `tests/unit_tests/core/test_models_provider.py`, `tests/unit_tests/core/test_provider_failures.py` (new)

**Interfaces:**
- Produces, in `core.models`:
  - `class ProviderFailureKind(models.TextChoices)`: `OUT_OF_CREDITS = "out_of_credits"`,
    `SPEND_LIMIT_REACHED = "spend_limit_reached"`, `AUTH_FAILED = "auth_failed"`, `RATE_LIMITED = "rate_limited"`,
    `PROVIDER_UNAVAILABLE = "provider_unavailable"`.
  - `PROVIDER_FAILURE_TTL = timedelta(hours=24)`.
  - `Provider` fields, all `editable=False`:
    - `failure_kind`: CharField 32, choices, blank, default `""`
    - `failure_model`: CharField 255, blank, default `""`
    - `failing_since`: DateTimeField, null
    - `last_failed_at`: DateTimeField, null
  - `Provider.Cached` gains the same four fields, with defaults (`""`, `""`, `None`, `None`).
  - `Provider.Cached.active_failure_kind(now: datetime) -> str`: the kind, or `""` when blank or when
    `last_failed_at` is older than `PROVIDER_FAILURE_TTL`.
  - `Provider.display_name_for(slug: str) -> str` (classmethod): reads `get_cached_by_slug()` and falls back to
    the slug.
- Produces, in `core.provider_failures`:
  - `ADMIN_KINDS: frozenset[str]`
  - `@dataclass(frozen=True) class ProviderFailure: kind: str; provider_slug: str`, with `message() -> str`
  - `failure_message(kind: str, provider_slug: str) -> str`
  - `active_provider_failures(now: datetime | None = None) -> list[Provider.Cached]`: enabled rows whose active
    kind is in `ADMIN_KINDS`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit_tests/core/test_provider_failures.py
@pytest.mark.django_db
@pytest.mark.parametrize(("kind", "fragment"), [
    (ProviderFailureKind.OUT_OF_CREDITS, "is out of credits"),
    (ProviderFailureKind.SPEND_LIMIT_REACHED, "spending limit"),
    (ProviderFailureKind.AUTH_FAILED, "rejected DAIV's API key"),
    (ProviderFailureKind.RATE_LIMITED, "rate-limiting DAIV"),
    (ProviderFailureKind.PROVIDER_UNAVAILABLE, "having problems"),
])
def test_failure_message_names_the_provider(kind, fragment):
    message = failure_message(kind, "openrouter")
    assert "OpenRouter" in message and fragment in message

@pytest.mark.django_db
def test_failure_message_falls_back_to_the_slug_for_a_deleted_provider():
    assert failure_message(ProviderFailureKind.AUTH_FAILED, "gone").startswith("gone ")

@pytest.mark.django_db
def test_active_provider_failures_lists_only_admin_kinds_within_the_ttl():
    # openrouter: out_of_credits, last_failed_at now → listed
    # anthropic: rate_limited → not listed; openai: auth_failed 25 h ago → not listed
    assert [row.slug for row in active_provider_failures()] == ["openrouter"]
```

```python
# tests/unit_tests/core/test_models_provider.py (add)
def test_save_clears_the_failure_mark(): ...      # set the 4 fields via .update(), then .save() → all blank/None
def test_cached_row_carries_the_failure_fields(): ...
def test_active_failure_kind_expires_after_the_ttl(): ...  # last_failed_at = now - 25h → ""
```

- [ ] **Step 2: Run them, expect failures**

Run: `uv run pytest tests/unit_tests/core/test_provider_failures.py tests/unit_tests/core/test_models_provider.py -v`
Expected: FAIL (ImportError / missing fields)

- [ ] **Step 3: Implement**

`Provider.save()` blanks the four failure fields before `super().save()`. The listener writes through queryset
`.update()`, so only settings saves reach `save()`. `_build_from_db` copies the new fields. Message copy, exact,
through `gettext` with `%(provider)s`:

- out_of_credits: "%(provider)s is out of credits, so DAIV couldn't finish this run. An admin needs to top up the %(provider)s account."
- spend_limit_reached: "DAIV has reached the spending limit set on its %(provider)s key, so it couldn't finish this run. An admin needs to raise the limit or wait for it to reset."
- auth_failed: "%(provider)s rejected DAIV's API key, so DAIV couldn't finish this run. An admin needs to update the key in the provider settings."
- rate_limited: "%(provider)s is rate-limiting DAIV right now. Try again in a few minutes."
- provider_unavailable: "%(provider)s is having problems right now. Try again in a few minutes."

Then: `uv run daiv/manage.py makemigrations core -n provider_failure`.

- [ ] **Step 4: Run the tests, expect a pass**

Run: same as Step 2. Expected: PASS

- [ ] **Step 5: Commit** — `feat(core): Track provider failure kinds and health on provider rows`

---

### Task 2: Classifier

**Files:**
- Create: `daiv/automation/agent/provider_errors.py`
- Test: `tests/unit_tests/automation/agent/test_provider_errors.py`

**Interfaces:**
- Consumes: `ProviderFailureKind`, `ProviderFailure` (Task 1).
- Produces:
  - `classify_provider_error(exc: BaseException) -> ProviderFailureKind | None`
  - `tag_provider_failure(exc: BaseException, failure: ProviderFailure) -> None`: sets attribute
    `daiv_provider_failure` and swallows `AttributeError`/`TypeError`
  - `provider_failure_of(exc: BaseException | None) -> ProviderFailure | None`: walks `__cause__` /
    `__context__` (with a cycle guard) for the first tag

- [ ] **Step 1: Write the failing table test**

Build real SDK exceptions from a response helper (`httpx2.Response(status, json=..., request=httpx2.Request("POST", url))`
for openai/anthropic; `google.genai.errors.ClientError/ServerError(code, response_json)` wrapped as
`ChatGoogleGenerativeAIError(...)` raised `from` it, as langchain-google-genai does).

```python
@pytest.mark.parametrize(("exc", "expected"), [
    (openai_status(402, {"error": {"code": 402, "message": "x", "metadata": {"limit_source": "openrouter_credits"}}}), OUT_OF_CREDITS),
    (openai_status(402, {"error": {"code": 402, "message": "Insufficient credits"}}), OUT_OF_CREDITS),
    (openai_status(402, {"error": {"code": 402, "message": "x", "metadata": {"limit_source": "openrouter_in_flight_budget"}}}), RATE_LIMITED),
    (openai_status(403, {"error": {"code": 403, "message": "Budget limit exceeded (weekly limit).", "metadata": {"limit_source": "openrouter_key_limit"}}}), SPEND_LIMIT_REACHED),
    (openai_status(403, {"error": {"code": 403, "message": "Request blocked: prompt injection patterns detected", "metadata": {"patterns": ["x"]}}}), None),
    (openai_status(429, {"error": {"code": "insufficient_quota", "message": "x"}}), OUT_OF_CREDITS),
    (openai_status(429, {"error": {"code": "rate_limit_exceeded", "message": "x"}}), RATE_LIMITED),
    (openai_status(401, {"error": {"message": "bad key"}}), AUTH_FAILED),
    (openai_status(500, {"error": {"message": "x"}}), PROVIDER_UNAVAILABLE),
    (openai.APIConnectionError(request=REQ), PROVIDER_UNAVAILABLE),
    (anthropic_status(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "Your credit balance is too low to access the Anthropic API."}}), OUT_OF_CREDITS),
    (anthropic_status(400, {"type": "error", "error": {"type": "invalid_request_error", "message": "messages: field required"}}), None),
    (anthropic_status(402, {"type": "error", "error": {"type": "billing_error", "message": "x"}}), OUT_OF_CREDITS),
    (anthropic_status(429, {"type": "error", "error": {"type": "rate_limit_error", "message": "x", "details": {"error_code": "enforced_spend_limit_reached"}}}), SPEND_LIMIT_REACHED),
    (anthropic_status(529, {"type": "error", "error": {"type": "overloaded_error", "message": "x"}}), PROVIDER_UNAVAILABLE),
    (anthropic_status(401, {"type": "error", "error": {"type": "authentication_error", "message": "x"}}), AUTH_FAILED),
    (gemini(ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "x"}})), RATE_LIMITED),
    (gemini(ClientError(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid. Please pass a valid API key."}})), AUTH_FAILED),
    (gemini(ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "x"}})), PROVIDER_UNAVAILABLE),
    (ValueError("boom"), None),
])
def test_classify_provider_error(exc, expected): assert classify_provider_error(exc) == expected

def test_classify_finds_the_provider_error_behind_a_wrapper(): ...  # RuntimeError raised `from` a 402 → OUT_OF_CREDITS
def test_provider_failure_of_reads_the_tag_through_the_cause_chain(): ...
def test_tagging_an_exception_that_refuses_attributes_does_not_raise(): ...
```

The Anthropic `EnforcedSpendLimitReachedError` row: build it through `anthropic._error_classes.make_status_error`
so the test proves the SDK's own class maps. Adjust the body if the SDK reads the code from somewhere else.

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/unit_tests/automation/agent/test_provider_errors.py -v`

- [ ] **Step 3: Implement `classify_provider_error`**

Branch on SDK type:

- `openai.APIStatusError` / `anthropic.APIStatusError`: `status_code` plus the error object. That's
  `body["error"]` when the body has an `error` key, else `body`.
- `openai.APIConnectionError` / `anthropic.APIConnectionError` (timeouts included): provider unavailable.
- `google.genai.errors.APIError`: `.code` / `.status` / message.

Rules:

- 402 reads `metadata.limit_source`: `openrouter_in_flight_budget` → rate limited; anything else → out of credits.
- 403 is spending limit only when `limit_source` names a budget (`openrouter_key_limit` or any value ending in
  `_budget`); otherwise `None`.
- Message-text matching only for Anthropic's 400 (`"credit balance"`) and Gemini's invalid key
  (`"API key not valid"`).
- Walk the chain. The first match wins.

- [ ] **Step 4: Run, expect PASS** (same command)

- [ ] **Step 5: Commit** — `feat(agent): Classify provider errors into failure kinds`

---

### Task 3: Health listener on every model

**Files:**
- Create: `daiv/automation/agent/provider_health.py`
- Modify: `daiv/automation/agent/base.py:289-307` (`get_model`)
- Test: `tests/unit_tests/automation/agent/test_provider_health.py` (new), `tests/unit_tests/automation/agent/test_base.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces:
  - `class ProviderHealthCallback(BaseCallbackHandler)`: `__init__(self, provider_slug: str, model_name: str)`;
    `on_llm_error(error, **kwargs)`; `on_llm_end(response, **kwargs)`
  - `mark_provider_failure(provider_slug: str, kind: str, model_name: str, now: datetime | None = None) -> None`
  - `clear_provider_failure(provider_slug: str, model_name: str) -> None`

- [ ] **Step 1: Write the failing tests**

Test model: a `BaseChatModel` subclass whose `_generate` raises a given exception. For success use
`FakeListChatModel(responses=["ok"], callbacks=[cb])`. Sync tests with plain `django_db`; the async one patches
the DB functions.

```python
@pytest.mark.django_db
def test_a_credits_error_is_tagged_and_marks_the_provider(): ...
    # RaisingChatModel(error=openrouter_402, callbacks=[ProviderHealthCallback("openrouter", "m-big")]).invoke("hi")
    # raises the same object; provider_failure_of(err) == ProviderFailure("out_of_credits", "openrouter")
    # row: failure_kind "out_of_credits", failure_model "m-big", failing_since and last_failed_at set
@pytest.mark.django_db
def test_a_repeat_keeps_failing_since_and_bumps_last_failed_at(): ...
@pytest.mark.django_db
def test_a_rate_limit_is_tagged_but_marks_nothing(): ...
@pytest.mark.django_db
def test_success_on_another_model_keeps_the_credits_mark(): ...      # Review Focus 2
@pytest.mark.django_db
def test_success_on_the_failing_model_clears_the_credits_mark(): ...
@pytest.mark.django_db
def test_any_success_clears_an_auth_mark(): ...
@pytest.mark.django_db
def test_a_db_failure_while_marking_keeps_the_original_error(caplog): ...  # Review Focus 5: patch Provider.objects.filter → DatabaseError
async def test_an_async_call_is_tagged_before_it_raises(): ...       # patch mark_provider_failure; ainvoke; tag present
@pytest.mark.django_db
def test_get_model_attaches_the_health_callback(): ...               # in test_base.py: one ProviderHealthCallback with slug/model
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/unit_tests/automation/agent/test_provider_health.py tests/unit_tests/automation/agent/test_base.py -v`

- [ ] **Step 3: Implement**

The callback is a sync `BaseCallbackHandler` with `run_inline = False`. In async runs LangChain awaits it in an
executor thread, so the tag lands before the error propagates and no ORM call runs on the event loop.

- `on_llm_error`: classify, tag, and mark when the kind is in `ADMIN_KINDS`.
- `on_llm_end`: call `clear_provider_failure`.

`mark_provider_failure`:
1. `filter(slug, failure_kind=kind, failure_model=model).update(last_failed_at=now)`.
2. If nothing was updated: `filter(slug=slug).update(` all four fields `)`.
3. Then `Provider.invalidate_cache()`.

`clear_provider_failure`:
- Return early unless the cached row has an active kind, and that kind is `auth_failed` or
  `failure_model == model_name`.
- Otherwise `filter(slug=slug, failure_kind=<that kind>).update(<blanks>)`, then invalidate.

Both functions: wrap everything in `try/except Exception: logger.exception(...)`, and handle connections the way
`Provider._load_and_cache` does.

In `get_model`: append the callback to `model_kwargs["callbacks"]` (keep any the caller passed) before building
the model.

- [ ] **Step 4: Run, expect PASS** (same command)

- [ ] **Step 5: Commit** — `feat(agent): Track provider health from every model call`

---

### Task 4: Run fields; the executor records the failure

**Files:**
- Modify: `daiv/sessions/models.py:467` (next to `error_message`)
- Create: `daiv/sessions/migrations/0037_run_failure_kind.py` (generated)
- Modify: `daiv/sessions/executor/run.py` at the `_notify_failure` call sites :160 (`stream_run`) and :219 (`_run_in_slot`)
- Test: `tests/unit_tests/sessions/executor/test_run.py`

**Interfaces:**
- Consumes: `provider_failure_of` (Task 2), `failure_message` (Task 1).
- Produces:
  - `Run.failure_kind`: CharField 32, `ProviderFailureKind` choices, blank, default `""`
  - `Run.failure_provider`: CharField 32, blank, default `""`
  - `Run.provider_failure_message() -> str | None`
  - In the executor: `async def _arecord_provider_failure(run_id: str | None, exc: BaseException) -> None`.
    It does nothing without a run id or a tag; it never raises.

- [ ] **Step 1: Write the failing tests** (`django_db(transaction=True)`; reuse the failing-agent seam from
  `test_a_failing_relay_close_does_not_mask_the_agents_error`, :275)

```python
async def test_a_provider_failure_is_recorded_on_the_run(): ...
    # agent raises an exception tagged ProviderFailure("out_of_credits", "openrouter"); run row exists for spec.run_id
    # after execute_run raises: run.failure_kind == "out_of_credits", run.failure_provider == "openrouter"
async def test_an_untagged_failure_leaves_the_run_fields_blank(): ...
async def test_stream_run_records_a_provider_failure_found_through_the_cause_chain(): ...
    # stream raises RuntimeError from a tagged error (the chat path's _ReportedRunError shape)
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/unit_tests/sessions/executor/test_run.py -v -k provider`
- [ ] **Step 3: Implement.** Add the fields and the method. Run `uv run daiv/manage.py makemigrations sessions -n run_failure_kind`.
  Call `_arecord_provider_failure(spec.run_id, exc)` just before `_notify_failure` at both sites.
- [ ] **Step 4: Run, expect PASS** (same command)
- [ ] **Step 5: Commit** — `feat(sessions): Record a run's provider failure kind`

---

### Task 5: Live messages — chat stream and the session live view

**Files:**
- Modify: `daiv/sessions/live/publisher.py:57-62` (`sanitize_run_error`), :189 (`_RootOutcome` → `RootOutcome`), and the
  three `sanitize_run_error(...)` calls in `ainvoke_live`
- Modify: `daiv/chat/api/streaming.py:313-362` (`_agui_events`), :262-266
- Test: `tests/unit_tests/sessions/live/test_publisher.py`, `tests/unit_tests/chat/api/test_streaming.py`

**Interfaces:**
- Consumes: `provider_failure_of` (Task 2), `ProviderFailure.message()` (Task 1).
- Produces:
  - `sanitize_run_error(event: BaseEvent, error: BaseException | None = None) -> BaseEvent`. A tagged `error`
    gives `RunErrorEvent(message=failure.message(), code=f"provider_{failure.kind}")`. Otherwise it behaves as
    today.
  - `RootOutcome` (public, unchanged behaviour)

- [ ] **Step 1: Write the failing tests**

```python
# test_publisher.py
def test_sanitize_run_error_uses_the_provider_message_for_a_tagged_error(): ...  # code "provider_out_of_credits", "OpenRouter" in message
def test_sanitize_run_error_stays_generic_for_an_untagged_error(): ...           # RUN_FAILED_MESSAGE, "run_failed"
async def test_a_live_run_publishes_the_provider_message(): ...                   # graph raises tagged error → published RUN_ERROR carries it
# test_streaming.py, next to test_events_finalizes_failed_when_run_error_event_emitted (:439)
async def test_a_provider_failure_streams_and_persists_the_provider_message(): ...
    # graph raises tagged ProviderFailure("out_of_credits","openrouter"): streamed RUN_ERROR code "provider_out_of_credits";
    # Run.error_message == that message; the _ReportedRunError reaching the executor has the tagged error as __cause__
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/unit_tests/sessions/live/test_publisher.py tests/unit_tests/chat/api/test_streaming.py -v -k provider`
- [ ] **Step 3: Implement.**
  - Publisher: pass `outcome.error` to every `sanitize_run_error` call.
  - Chat `_agui_events`: build `RootOutcome()` and merge `{"callbacks": [outcome], "run_id": outcome.root_id}`
    into the agui `config`, as `ainvoke_live` does.
  - On RUN_ERROR, set `turn.error` to `failure.message()` when `provider_failure_of(outcome.error)` returns a
    failure, else `RUN_FAILED_MESSAGE`. Yield `sanitize_run_error(event, outcome.error)`.
  - End with `raise _ReportedRunError from outcome.error`.
  - `chat-stream.js` already renders `evt.message` for unknown failure codes: confirm, don't change.
- [ ] **Step 4: Run, expect PASS**; also `uv run pytest tests/unit_tests/chat/test_chat_stream_js.py -v`
- [ ] **Step 5: Commit** — `feat(chat): Say which provider stopped the run in the live stream`

---

### Task 6: Reload, notifications, jobs API, MCP

**Files:**
- Modify: `daiv/sessions/transcript.py:71-98` (`_marker`)
- Modify: `daiv/notifications/tasks.py:199-203`
- Modify: `daiv/jobs/api/views.py:95`, `daiv/mcp_api/server.py:347`
- Test: `tests/unit_tests/sessions/test_transcript.py` (add `failure_kind=""`, `failure_provider=""` to the `_run`
  fake), `tests/unit_tests/notifications/test_tasks.py`, `tests/unit_tests/jobs/api/test_views.py`,
  `tests/unit_tests/mcp_api/test_server_jobs.py`

**Interfaces:** Consumes `failure_message` (Task 1), `Run.provider_failure_message()` (Task 4).

- [ ] **Step 1: Write the failing tests**

```python
# test_transcript.py
@pytest.mark.parametrize("trigger", [SessionOrigin.CHAT, SessionOrigin.JOB, SessionOrigin.ISSUE_WEBHOOK])  # use the real enum members
def test_a_provider_failure_marker_names_the_provider(trigger): ...  # status "failed", message == failure_message("out_of_credits","openrouter")
def test_a_stopped_run_keeps_the_stopped_marker_despite_a_provider_failure(): ...  # Review Focus 4: is_cancelled → "Stopped by user."
# notifications/test_tasks.py
async def test_a_failed_run_with_a_provider_failure_summarises_it(): ...
# jobs/api/test_views.py, next to test_get_job_status_failed (:639)
async def test_get_job_status_failed_by_a_provider_says_why(): ...      # error == provider message
async def test_get_job_status_failed_otherwise_keeps_the_generic_error(): ...  # "Job execution failed"
# mcp_api/test_server_jobs.py
async def test_get_job_failed_by_a_provider_says_why(): ...
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/unit_tests/sessions/test_transcript.py tests/unit_tests/notifications/test_tasks.py tests/unit_tests/jobs/api/test_views.py tests/unit_tests/mcp_api/test_server_jobs.py -v -k "provider"`
- [ ] **Step 3: Implement.** In `_marker`, right after the chat-cancelled branch, add
  `elif run.failure_kind and not run.is_cancelled:` → `failure_message(run.failure_kind, run.failure_provider)`,
  not aborted. Notifications summary: `run.provider_failure_message() or first_line or gettext("Run failed.")`.
  Jobs/MCP: the provider message, else today's text.
- [ ] **Step 4: Run, expect PASS** (same command)
- [ ] **Step 5: Commit** — `feat(sessions): Show the provider failure on reload, in notifications and job status`

---

### Task 7: Issue and MR comments

**Files:**
- Modify: `daiv/webhooks/managers/issue_addressor.py:216-242`, `daiv/webhooks/managers/review_addressor.py:134-208`
- Modify: `daiv/webhooks/templates/webhooks/unable_address_issue.txt`, `daiv/core/templates/core/unable_address_review.txt`
- Test: `tests/unit_tests/webhooks/managers/test_issue_addressor.py`, `tests/unit_tests/webhooks/managers/test_review_addressor_footer.py`

**Interfaces:** Consumes `provider_failure_of` (Task 2). Both note helpers take
`failure_message: str | None = None`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_provider_failure_says_so_on_the_issue(self, captured_client): ...
    # on_failure with a tagged out_of_credits error → note contains "OpenRouter is out of credits, so DAIV couldn't"
    # (literal apostrophe — Review Focus 3) and still the "To have DAIV resume" line; no "unexpected error"
async def test_a_provider_failure_says_so_on_the_merge_request(...): ...
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/unit_tests/webhooks/managers -v -k provider`
- [ ] **Step 3: Implement.** `_on_failure` passes `failure.message()` when `provider_failure_of(exc)` returns a
  failure. In each template, the first line becomes `{% if failure_message %}{{ failure_message|safe }}{% else %}`
  followed by today's line and `{% endif %}`. The `.txt` templates autoescape, and the text is fixed copy plus an
  admin-set display name.
- [ ] **Step 4: Run, expect PASS** (same command)
- [ ] **Step 5: Commit** — `feat(webhooks): Name the provider that stopped the run in the issue or MR note`

---

### Task 8: Admin banner and changelog

**Files:**
- Modify: `daiv/core/context_processors.py`, `daiv/daiv/settings/components/common.py:82-91`
- Create: `daiv/core/templates/core/_provider_failure_banner.html` (style after `schedules/_dispatch_failure_banner.html`)
- Modify: `daiv/dashboard/templates/base_app.html` (include it just above `{% block app_body %}`, :63)
- Modify: `CHANGELOG.md` (Unreleased → Added)
- Test: `tests/unit_tests/core/test_context_processors.py`, `tests/unit_tests/dashboard/` view test rendering any signed-in page

**Interfaces:** Consumes `active_provider_failures` (Task 1). Produces the context processor
`core.context_processors.provider_health(request) -> dict`. It returns
`{"provider_failures": SimpleLazyObject(active_provider_failures)}` for authenticated admins, `{}` otherwise.

- [ ] **Step 1: Write the failing tests**

```python
def test_provider_health_is_empty_for_a_non_admin(rf, member_user): ...
def test_provider_health_lists_the_marked_provider_for_an_admin(rf, admin_user): ...
def test_an_admin_sees_the_banner_with_a_link_to_provider_settings(admin_client): ...
    # dashboard page contains "OpenRouter", "out of credits", the model name, and reverse("site_configuration", kwargs={"group_key": "providers"})
def test_a_member_never_sees_the_banner(member_client): ...
def test_the_banner_is_gone_after_the_ttl(admin_client): ...
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/unit_tests/core/test_context_processors.py tests/unit_tests/dashboard -v -k "provider"`
- [ ] **Step 3: Implement.** Banner copy, one line per kind, through `blocktranslate`:
  - out_of_credits: "%(provider)s can't cover requests to %(model)s: out of credits since %(since)s. Runs that use it are failing, or falling back to the fallback model if one is set."
  - spend_limit_reached: "DAIV's %(provider)s key has hit its spending limit since %(since)s. Runs that use it are failing, or falling back to the fallback model if one is set."
  - auth_failed: "%(provider)s has been rejecting DAIV's API key since %(since)s. Runs that use it are failing, or falling back to the fallback model if one is set."

  Link text: "Open provider settings". `role="alert"`. Run `uv run --only-group=dev djade --target-version 6.0`
  on the new template. Changelog line:
  "Say which provider stopped a run — out of credits, spending limit, rejected key, rate limit or outage — on every failure surface, and warn admins with a banner while a provider needs them".
- [ ] **Step 4: Run, expect PASS** (same command)
- [ ] **Step 5: Commit** — `feat(dashboard): Warn admins while a provider is out of credits or rejecting its key`

---

### Final verification

- [ ] `make test`: all pass
- [ ] `make lint`: passes. It includes the import-layering contracts: core imports no other app, and the
  automation, sessions, chat and webhooks imports above are all downward.
- [ ] `make lint-typing`: no new diagnostics in touched files
- [ ] Manual, optional: point OpenRouter at a key with a $0 limit in `make up`, start a chat. The chat shows the
  credits or spending-limit message, and the admin banner appears.
