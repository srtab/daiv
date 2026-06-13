"""Guards for DAIV's harness-profile customizations.

The main agent's filesystem grep override cannot use the usual exclude+re-add pattern:
``FilesystemMiddleware`` is required scaffolding in ``deepagents.graph._REQUIRED_MIDDLEWARE``,
so excluding it raises. Instead ``profile.register()`` rebinds the ``FilesystemMiddleware`` name
in ``deepagents.graph`` to ``DAIVFilesystemMiddleware`` so ``create_deep_agent`` constructs the
subclass for the main agent. These tests pin that mechanism so a refactor (or a deepagents bump
that relocates the auto-add) fails loudly.
"""

import inspect

import deepagents.graph as deepagents_graph

from automation.agent import subagents as subagents_module
from automation.agent.middlewares.file_system import DAIVFilesystemMiddleware
from automation.agent.profile import _install_daiv_filesystem_middleware


def test_register_rebinds_deepagents_graph_filesystem_middleware():
    """The lever the main-agent override depends on: ``create_deep_agent`` builds whatever
    ``deepagents.graph.FilesystemMiddleware`` resolves to. After install it must be DAIV's subclass."""
    original = deepagents_graph.FilesystemMiddleware
    try:
        _install_daiv_filesystem_middleware()
        assert deepagents_graph.FilesystemMiddleware is DAIVFilesystemMiddleware
    finally:
        deepagents_graph.FilesystemMiddleware = original


def test_filesystem_middleware_is_required_scaffolding_upstream():
    """Documents *why* the rebind exists: the exclude+re-add path is structurally unavailable
    because upstream lists FilesystemMiddleware as required scaffolding. If a deepagents bump drops
    this guard, reconsider switching to the simpler exclude+re-add mechanism.

    Checks the upstream class object directly: ``register()`` may already have rebound the
    ``deepagents.graph.FilesystemMiddleware`` *name* to DAIV's subclass, but the required-classes
    set is computed at import from the original class."""
    from deepagents.middleware.filesystem import FilesystemMiddleware as UpstreamFilesystemMiddleware

    assert UpstreamFilesystemMiddleware in deepagents_graph._REQUIRED_MIDDLEWARE_CLASSES


def test_subagents_use_daiv_filesystem_middleware():
    """Subagents build their middleware directly (not via create_deep_agent), so they must name
    the subclass explicitly rather than rely on the rebind."""
    src = inspect.getsource(subagents_module)
    assert "DAIVFilesystemMiddleware(" in src
    assert "        FilesystemMiddleware(" not in src, "subagents must not instantiate the upstream class"


def test_create_deep_agent_builds_main_agent_with_extended_grep():
    """End-to-end proof of the rebind's EFFECT, not just the mechanism: after the install,
    ``create_deep_agent`` must instantiate the filesystem middleware *via the rebound name* and the
    resulting main-agent grep tool must carry DAIV's extended ripgrep options.

    The other tests assert the name is rebound and that the subclass exposes the extended schema;
    this one closes the gap they leave open — that a deepagents bump which instantiates from a
    reference captured at import (e.g. ``_REQUIRED_MIDDLEWARE``) rather than the module-global name
    would silently revert the main agent to upstream's literal grep with every other test still green.
    A recording subclass captures the instance built by ``create_deep_agent``."""
    from deepagents import create_deep_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    original = deepagents_graph.FilesystemMiddleware
    built: list = []
    try:
        _install_daiv_filesystem_middleware()
        assert deepagents_graph.FilesystemMiddleware is DAIVFilesystemMiddleware

        class _Recording(DAIVFilesystemMiddleware):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                built.append(self)

        deepagents_graph.FilesystemMiddleware = _Recording
        create_deep_agent(model=GenericFakeChatModel(messages=iter([])))
    finally:
        deepagents_graph.FilesystemMiddleware = original

    assert built, "create_deep_agent did not instantiate via deepagents.graph.FilesystemMiddleware — rebind is a no-op"
    grep = next(t for t in built[0].tools if t.name == "grep")
    assert {"head_limit", "case_insensitive", "multiline"} <= set(grep.args.keys())
