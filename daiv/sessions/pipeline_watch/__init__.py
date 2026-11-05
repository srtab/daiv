"""CI watch on a merge request DAIV published: judge each head pipeline and post the result to the inbox of the session
that owns the merge request, which decides from facts whether the agent wakes to look at it.

The layering is the design. ``judgment`` is pure; ``policy`` clamps a repository's ``.daiv.yml`` against the site
switch; ``platform`` holds the package's only ``RepoClient`` and the only classification of a platform error; ``store``
is the only module that touches the watch columns of ``MergeRequestOwnership``; ``service`` sequences them, applies
the wake rule and owns no I/O or SQL of its own. Running the agent is the inbox's job (``sessions.inbox``), not the
watch's.

Callers outside the package import the submodule they need rather than a façade, so ``sessions/tasks.py`` and
``policy``'s two webhook callers do not drag ``service``'s import graph in. Each module's own docstring carries the
rest.
"""
