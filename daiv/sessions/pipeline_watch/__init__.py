"""CI watch on a merge request DAIV published: judge the pipeline, dispatch a bounded number of
fix runs, and give up loudly when it cannot make progress.

The layering is the design. ``judgment`` is pure; ``policy`` clamps a repository's ``.daiv.yml``
against the site switch; ``platform`` holds the package's only ``RepoClient`` and the only
classification of a platform error; ``store`` is the only module that touches ``Session.watch_*``,
reads and writes alike, which is why an attempt claim is ``WatchStore.aclaim_attempt`` rather than
an ``atransition`` of ``F`` expressions driven from ``service``; ``service`` sequences them and
owns no I/O or SQL of its own.

Callers outside the package import the submodule they need rather than a façade, so
``sessions/tasks.py`` and ``policy``'s two webhook callers do not drag ``service``'s import graph
in. Each module's own docstring carries the rest.
"""
