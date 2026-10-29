# Tests

## Layout

- Unit tests live in `tests/unit_tests/`, mirroring `daiv/` by app, one test file per
  application module: `daiv/<app>/foo.py` is tested in `tests/unit_tests/<app>/test_foo.py`.
  There are no feature-named catch-all files.
- Shared fixtures live in `tests/unit_tests/conftest.py`.
- Tests import without the `daiv.` prefix (`from automation.agent.graph import ...`), because
  `pythonpath = ["daiv", "."]`.
- `asyncio_mode = "auto"`: async tests need no marker.
- Tests that call real models live in `tests/integration_tests/`, which `make test` never runs.

## Rules

1. **A test fails without the change.** A behaviour change ships with tests that fail when
   the change is reverted. _Prevents_: green builds that prove nothing.
2. **Test DAIV, not the framework.** Cover DAIV's own logic: custom model methods, services,
   view branching, gating, and call-site wiring that threads new parameters through. A test
   that would still pass with DAIV's code deleted and the framework's defaults in its place is
   not written. _Prevents_: a slow suite that breaks on framework upgrades without finding a
   DAIV bug.
3. **No real LLM or network.** Unit tests stub models, git-platform clients and the sandbox.
   _Prevents_: flaky, paid, credential-dependent CI.
4. **No state leaks between tests.** An async test that writes to the database uses
   `django_db(transaction=True)`, because async ORM writes run on another connection and commit
   past the test's rollback. A test that depends on a shared cache bucket, such as a throttle,
   clears the cache first. Patch a module constant, never the global `time` module.
   _Prevents_: failures that depend on test order under `pytest -n auto`.
5. **Evidence is what ran.** A change reports the commands it ran and their results; a check
   that did not run is reported as not run. _Prevents_: a passing claim no run supports.
