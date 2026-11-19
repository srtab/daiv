# Provider failure messages — design

Status: draft for approval · 2026-11-19

## Intent

When a run stops because the LLM provider refused it — the OpenRouter account ran out of credits, the API key
was revoked — every surface shows the same generic line ("Run failed. Check server logs for details."). The person
whose run stopped can't tell what happened, and the admin who can fix it only hears about it second-hand.

Two audiences, both in scope:

- **The person whose run stopped** sees, on the surface they're already looking at, a plain sentence naming the
  provider and the cause, and who can fix it.
- **Admins** see a banner on every signed-in page while a provider is out of credits or rejecting DAIV's key, with
  a link to the provider settings, until the provider works again.

## Goals

- A run that fails because of a recognised provider error shows a fixed, provider-named message on every failure
  surface: chat (live and on reload), the session page for non-chat runs (live and on reload), issue/MR comments,
  notifications, and the jobs API `error` field.
- Admins learn about a provider that needs their action (credits, spending limit, key) without anyone relaying it — including
  when the fallback model hid the failure from users, and when the failing call was a background one (titling,
  notification classification, memory extraction, web_fetch summaries).
- Covers all four provider types: OpenRouter, Anthropic, OpenAI, Google Gemini.

## Non-goals

- Proactive balance warnings (polling OpenRouter's key endpoint before credits run out). Possible later add-on;
  only OpenRouter exposes a balance.
- Email/Telegram alerts to admins. The banner is the admin channel for now.
- Refusing new runs early while a provider is marked. A stale mark would block runs after a top-up.
- Changing retry or fallback behaviour (e.g. not retrying OpenAI's `insufficient_quota`, which is a 429 and is
  retried by the SDK today).

## Anti-goals

- **No raw provider or exception text ever reaches users.** Provider error bodies can carry account ids, URLs and
  internal names. Users only ever see one of the fixed messages below, with the provider's display name filled in.
  This keeps today's rule (`core/constants.py:33`, `sessions/transcript.py:75`).
- **A wrong label is worse than the generic message.** Telling users "out of credits" when it isn't sends them
  and the admin down the wrong path. When the classifier isn't sure, it returns nothing and today's generic message
  stands.
- **Health tracking never fails or slows a model call.** Marking and clearing are best-effort and logged.
- **A banner never outlives the problem.** It clears on the provider's next successful call.

## What exists today (constraints)

Failure surfaces, all generic:

| Surface | Where | Today |
|---|---|---|
| Chat, live | `chat/api/streaming.py:262-266`, `:345-346` | RUN_ERROR `run_failed` + `RUN_FAILED_MESSAGE` |
| Chat and non-chat, reload | `sessions/transcript.py:_marker` | allow-listed constants for chat; always generic for others |
| Non-chat, live | `sessions/live/publisher.py:58-61` (`sanitize_run_error`) | generic RUN_ERROR |
| Issue / MR comment | `webhooks/managers/issue_addressor.py:142`, `review_addressor.py:88`, `webhooks/templates/webhooks/unable_address_issue.txt` | "An unexpected error occurred…" |
| Notifications | `notifications/tasks.py:199-203` | first line of `Run.error_message` — for task runs that is the exception class path (`sessions/models.py:695`) |
| Jobs API | `jobs/api/views.py:95` | "Job execution failed" |

Facts the design depends on:

- **Every model DAIV builds comes from `get_model`** (`automation/agent/base.py:289`): agent, fallbacks, subagents,
  diff_to_metadata, web_fetch, memory, notification classification, titling. It knows the provider row.
- **Chat runs never see the exception.** ag-ui catches the graph's exception and emits a RUN_ERROR carrying
  `str(exc)` (`ag_ui_langgraph/agent.py:1595-1604`); `streaming.py` only sees that event. Non-chat runs recover the
  real exception with the `_RootOutcome` callback in `sessions/live/publisher.py:189` and re-raise it, so it
  reaches `execute_run`'s `on_failure` hook (`sessions/executor/run.py:88`).
- **The fallback re-raises only the last model's exception** (`langchain/agents/middleware/model_fallback.py`),
  with no link back to the primary's.
- **Gemini wraps the SDK error** (`raise ChatGoogleGenerativeAIError(msg) from e`); the `google.genai` error with
  `.code` / `.status` is the `__cause__`.
- **OpenRouter's 402 and 403 each mean several things**, told apart by `error.metadata.limit_source` in the body
  ([errors](https://openrouter.ai/docs/api-reference/errors), [limits](https://openrouter.ai/docs/api-reference/limits),
  read 2026-11-19):
  - 402 `openrouter_credits`: the balance can't cover the request. This includes "this one request is too
    expensive" (`weight_exceeds_budget`), so a low balance fails big requests while small ones still pass.
  - 402 `openrouter_in_flight_budget`: transient. It comes with `Retry-After` and clears once in-flight requests
    settle.
  - 403 budget blocks: `openrouter_key_limit` (the key's own cap), workspace budgets and guardrail budgets, e.g.
    "Budget limit exceeded (weekly limit)…". There is no `Retry-After`; they last until the window resets.
  - Any other 403 is a guardrail, moderation or permission block (e.g. the Sensitive Info presets). It is not a
    billing or key problem and must stay unclassified.
  - The docs once list `openrouter_key_limit` under 402 too. Match on `limit_source`, not the status code.
- `Provider.Cached` is a cached snapshot of provider rows, invalidated on save (`core/models.py:1135`, `:1219`).
- Admins are `user.is_admin`. Provider rows are edited on a site-configuration group page
  (`configuration/views.py`, URL name `site_configuration`).
- `Run.error_message` stays operator text (class path, traceback, enqueue reasons) and is still never rendered.

## Approaches considered

1. **Classify only where the run fails.** Smallest change. Rejected: behind a fallback it often can't say which
   provider ran dry, it never sees a failure the fallback hid, and background calls fail unnoticed.
2. **Per-provider health from a listener on every model, plus per-run classification.** Chosen by the user. Names
   the exact provider, catches hidden and background failures. Costs a field pair on `Provider` and a cheap
   clear-on-success path.
3. **Proactive balance polling (OpenRouter only).** Deferred to a later add-on; doesn't cover the other providers.

## Design

### Failure kinds — a closed set (my call: credits was the ask; the other kinds ride the same mechanism)

| Kind | Recognised from | Message to the user | Admin banner |
|---|---|---|---|
| `out_of_credits` | OpenRouter 402 with `limit_source` `openrouter_credits`, or with no `limit_source`; Anthropic `billing_error` and the 400 `invalid_request_error` whose message says the credit balance is too low; OpenAI 429 with code `insufficient_quota` | "{Provider} is out of credits, so DAIV couldn't finish this run. An admin needs to top up the {Provider} account." | yes |
| `spend_limit_reached` | OpenRouter 403 budget block (`limit_source` `openrouter_key_limit`, workspace or guardrail budget); Anthropic `EnforcedSpendLimitReachedError` (429 `enforced_spend_limit_reached`) | "DAIV has reached the spending limit set on its {Provider} key, so it couldn't finish this run. An admin needs to raise the limit or wait for it to reset." | yes |
| `auth_failed` | 401 from any provider; Gemini's invalid-API-key error | "{Provider} rejected DAIV's API key, so DAIV couldn't finish this run. An admin needs to update the key in the provider settings." | yes |
| `rate_limited` | any other 429 (incl. Gemini `RESOURCE_EXHAUSTED`); OpenRouter 402 `openrouter_in_flight_budget` | "{Provider} is rate-limiting DAIV right now. Try again in a few minutes." | no |
| `provider_unavailable` | 5xx, Anthropic 529 overloaded, connection errors and timeouts after the SDK's retries | "{Provider} is having problems right now. Try again in a few minutes." | no |
| — | anything else | today's generic message | no |

Wording is a starting point; the shape (who/what/what next) is the requirement. Messages go through gettext and
are rendered at display time from the stored kind, so a reload shows them translated.

### Classifier

One pure function, e.g. `classify_provider_error(exc) -> ProviderFailureKind | None`, in `automation/agent/`. It
walks `__cause__` / `__context__`, recognises errors by SDK exception type, HTTP status and structured error code
(OpenRouter's `error.metadata.limit_source` included), and falls back to message text only where a provider gives
no structured code (Anthropic's credit-balance 400). Unknown → `None`.

### Model-level listener (the health record)

- `get_model` attaches a callback handler bound to the provider's slug to every model it builds.
- **On error:** classify. If it matches, tag the exception with `(kind, provider_slug)` so run-level code reads
  the provider without re-deriving it. For admin kinds (`out_of_credits`, `spend_limit_reached`, `auth_failed`),
  mark the provider with new fields on `Provider`:
  - `failure_kind` (blank = healthy)
  - `failure_model`: the model whose call failed
  - `failing_since`: set on the first failure of that kind, not bumped on repeats
  - `last_failed_at`: bumped on every repeat
- **Clearing (my call).** A success on just any model must not clear the mark. On a low OpenRouter balance the
  expensive primary gets a 402 while the cheap fallback, titling or classifier calls on the same provider row still
  pass. Clearing on those would make the banner flicker or never show, which is exactly the hidden-by-fallback case
  the banner exists for. The mark clears when:
  - `out_of_credits` / `spend_limit_reached`: a call to `failure_model` succeeds.
  - `auth_failed`: any call to the provider succeeds, since the key is per provider.
  - The provider is saved in settings. The admin who just topped up or changed the key shouldn't wait for a run;
    the next failing call re-marks it.
  - 24 h pass with no repeat (`last_failed_at`). This covers an admin who switched the failing model out of the
    configuration instead of fixing the account.
- The success-path check reads `Provider.Cached`, so a healthy provider costs no DB hit.
- Marking and clearing invalidate the provider cache, are idempotent across workers, never block the event loop,
  and never raise into the model call.

### Run-level classification

- New `Run.failure_kind` (choices, blank) and `Run.failure_provider` (slug, blank). Set when a run ends FAILED and
  its final exception carries the tag (or classifies).
- Primary and fallback failing with different kinds (my call): the run reports the final exception, i.e. the last
  model tried. Example: primary on provider A is out of credits, fallback on provider B is rate-limited. The user
  then reads "B is rate-limiting, try again", while the admin banner still shows A. Reporting the most serious kind
  seen during the run would need a per-run record of every tagged failure in every failure path. That isn't worth
  it while the default setup puts primary and fallback on the same OpenRouter row.
- Every way a run can fail must set them: chat, jobs/schedules/MCP/dashboard, issue/MR webhooks, delegated legs,
  inbox runs.
- Chat needs the real exception: recover it the way `publisher.py` does (`_RootOutcome`-style root-error capture)
  rather than parsing the RUN_ERROR text.
- The message names the provider by its current `display_name`, falling back to the slug if the row is gone.

### Surfaces

1. **Chat, live** — RUN_ERROR with a kind-specific `code` (e.g. `provider_out_of_credits`) and the fixed message.
   `chat-stream.js` already renders `evt.message` for failures; check that only the code list needs touching.
2. **Session page, reload** — `_marker` uses `Run.failure_kind` when set, for every trigger type, before the
   generic fallback. Still the red "failed" marker.
3. **Session page, live (non-chat)** — the publisher sends the kind message instead of the generic one when the
   recovered exception classifies.
4. **Issue / MR comment** — the "unable to address" notes say the kind message in place of "An unexpected error
   occurred". The resume line stays.
5. **Notifications** — the FAILED summary is the kind message when set, instead of `error_message`'s first line.
6. **Jobs API and MCP `get_job`** — `error` is the kind message when set, else today's text.

### Admin banner

- On every page that extends `base_app.html`, admins only, while any enabled provider has `failure_kind` set.
- Reads `Provider.Cached`, so it adds no query per page.
- Names the model, because a low balance can fail only the expensive one. Example: "OpenRouter can't cover
  requests to anthropic/claude-opus-5.5: out of credits (since 10:42). Runs that use it are failing, or falling
  back to the fallback model if one is set. **Open provider settings**"
- Follows `DESIGN.md` and the `input.css` class conventions.

## Testing

- Classifier: table-driven, using real SDK exception objects for each provider and kind, plus the near-misses
  that must stay unclassified (OpenRouter 403 guardrail or moderation, a 400 that isn't about credits), and
  OpenRouter's transient 402 `openrouter_in_flight_budget` landing on `rate_limited`.
- Listener: marks on an admin kind, ignores the others. A success on another model leaves an `out_of_credits` mark
  in place, and a success on `failure_model` clears it. Saving the provider clears it, and so does 24 h without a
  repeat. It does nothing when healthy, and swallows and logs its own DB/cache failures.
- Surfaces: each surface shows the kind message when `failure_kind` is set, and today's text when it isn't.
- Banner: visible to admins only, gone once the provider is cleared.

## Left to the builder

- Exact write point(s) for `Run.failure_kind` in each failure path, and the migration.
- Confirming each SDK's exact error shapes against the installed versions (openai 3.49, anthropic 2.7,
  google-genai 3.0), especially Anthropic's credit-balance 400 and Gemini's invalid-key error.
- RUN_ERROR code names, banner markup and placement in `base_app.html`.
- Final message wording within the who/what/what-next shape.

## Follow-ups (not this work)

- OpenRouter balance warning before credits run out.
- Out-of-band admin alert (email/Telegram) when a provider is first marked.
- Skipping SDK retries on `out_of_credits` (OpenAI's `insufficient_quota` is retried today as a 429).
