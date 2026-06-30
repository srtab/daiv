"""Migration test for 0006_drop_network_enabled.

Uses Django's built-in ``MigrationExecutor`` (no ``django-test-migrations``
dependency required). The test migrates to 0005, seeds two rows via the
historical model, then migrates to 0006 and asserts the data op ran correctly:

- An env that was ``network_enabled=False`` with a non-null egress_policy has
  its egress_policy NULLed (the "off" intent is preserved).
- An env that was ``network_enabled=True`` with a non-null egress_policy keeps
  its egress_policy intact.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

import pytest


@pytest.mark.django_db(transaction=True)
def test_off_env_with_policy_loses_policy():
    """null_egress_for_off_envs: network-off env's policy is nulled; network-on env's policy is kept."""
    executor = MigrationExecutor(connection)

    # Migrate down to 0005 so we have the historical model with network_enabled.
    executor.migrate([("sandbox_envs", "0005_sandboxenvironment__egress_secrets_encrypted_and_more")])
    executor.loader.build_graph()

    old_state = executor.loader.project_state((
        "sandbox_envs",
        "0005_sandboxenvironment__egress_secrets_encrypted_and_more",
    ))
    env_model_old = old_state.apps.get_model("sandbox_envs", "SandboxEnvironment")

    policy = {"default": "allow", "rules": []}

    off = env_model_old.objects.create(
        scope="global", name="off-with-policy", network_enabled=False, egress_policy=policy
    )
    on = env_model_old.objects.create(scope="global", name="on-with-policy", network_enabled=True, egress_policy=policy)

    # Migrate to 0006 (applies data op then drops network_enabled).
    executor2 = MigrationExecutor(connection)
    executor2.migrate([("sandbox_envs", "0006_drop_network_enabled")])
    executor2.loader.build_graph()

    new_state = executor2.loader.project_state(("sandbox_envs", "0006_drop_network_enabled"))
    env_model_new = new_state.apps.get_model("sandbox_envs", "SandboxEnvironment")

    assert env_model_new.objects.get(pk=off.pk).egress_policy is None, "off env: egress_policy should be nulled"
    assert env_model_new.objects.get(pk=on.pk).egress_policy is not None, "on env: egress_policy should be preserved"
