#!/bin/bash
#
# Makes `make test`, `make lint` and `make lint-typing` runnable in Claude Code on
# the web. Without it the container starts on a Python the project cannot import:
# the image ships a uv whose bundled index tops out at cpython-3.14.0rc2, and on
# rc2 pydantic cannot construct a single model (`typing._eval_type()` lost the
# `prefer_fwd_module` argument between rc2 and 3.14.0). Every model in the project
# fails at import, so pytest dies during plugin load and the entire suite is
# unrunnable — which reads like "the tests are broken" rather than "the
# interpreter is wrong".
#
# Idempotent: re-running is a no-op once the interpreter and venv are in place.
set -euo pipefail

# Local checkouts bring their own toolchain; only the remote image needs repairing.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

# Installs over the uv already on PATH (~/.local/bin), so the Makefile's own
# `uv run` picks it up with no PATH juggling. A current uv is the only thing that
# can resolve a real 3.14.x.
python3 -m pip install --user --quiet --upgrade uv >&2

# A PEP 440 specifier, not the bare `3.14` family: a family request happily matches
# 3.14.0rc2, so it would "succeed" against the very interpreter this hook exists to
# get rid of. A specifier excludes pre-releases, and mirroring `requires-python`
# keeps it floating across patch releases.
PY_REQUEST='>=3.14,<3.15'
uv python install "$PY_REQUEST" >&2

# `--python` alone cannot dislodge an existing venv: uv keeps one whose interpreter
# loosely satisfies the request, and rc2 does, so a sync would report success and
# change nothing. Discard any venv not built on a final release and let uv rebuild.
if [ -x .venv/bin/python ] \
  && ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info.releaselevel != "final")' 2>/dev/null; then
  echo "session-start: discarding .venv on $(.venv/bin/python -V 2>&1)" >&2
  rm -rf .venv
fi

# `--frozen` installs exactly what uv.lock pins and never rewrites it.
uv sync --frozen --python "$PY_REQUEST" >&2

# The session clone arrives without tags, and `test_docs_links` resolves the docs
# site's `latest` alias through them — so the suite ships one failure that has
# nothing to do with the working tree. Non-fatal: a fetch failure costs one test,
# not the session.
git fetch --tags --quiet origin >&2 2>/dev/null \
  || echo "session-start: could not fetch tags; test_docs_links will fail" >&2

# `make makemessages` shells out to xgettext, which the image does not carry.
# Non-fatal: translations are secondary to getting tests and linters running.
if ! command -v xgettext >/dev/null 2>&1; then
  apt-get install -y -qq gettext >&2 || echo "session-start: gettext unavailable; make makemessages will not run" >&2
fi

echo "Python toolchain ready: $(uv run python -V 2>/dev/null). Use make test / make lint / make lint-typing."
