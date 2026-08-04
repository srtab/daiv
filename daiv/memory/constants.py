# Documented defaults for the memory knobs. The live values are served by ``site_settings``
# (env var > site-configuration UI > default); these constants mirror the defaults declared
# in ``core.site_settings._build_field_defaults`` (parity-tested in test_constants).
# ``MEMORY_MAX_LINES``/``MEMORY_MAX_BYTES`` are the render budget enforced by ``prune_to_budget``;
# ``CONSOLIDATION_MIN_PENDING`` is also the threshold the ``consolidate_memory`` command enforces.
CONSOLIDATION_MIN_PENDING = 10
CONSOLIDATION_MAX_PENDING_AGE_DAYS = 7
MEMORY_MAX_LINES = 200
MEMORY_MAX_BYTES = 10_240
