from django.db import IntegrityError, transaction

import pytest
from sandbox_envs.models import SandboxEnvironment, Scope

from accounts.models import User


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(username="alice", email="alice@example.com", password="x")  # noqa: S106


@pytest.fixture
def user2(db) -> User:
    return User.objects.create_user(username="bob", email="bob@example.com", password="x")  # noqa: S106


def test_user_env_requires_user(db):
    with transaction.atomic(), pytest.raises(IntegrityError):
        SandboxEnvironment.objects.create(scope=Scope.USER, user=None, name="x", base_image="python:3.12")


def test_global_env_forbids_user(db, user):
    with transaction.atomic(), pytest.raises(IntegrityError):
        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, user=user, name="x", base_image="python:3.12")


def test_user_env_name_unique_per_user(db, user, user2):
    SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="a")
    # Same user, same name → conflict.
    with transaction.atomic(), pytest.raises(IntegrityError):
        SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="b")
    # Different user, same name → allowed.
    SandboxEnvironment.objects.create(scope=Scope.USER, user=user2, name="dev", base_image="c")


def test_global_env_name_unique(db):
    # Clear the seeded GLOBAL "Default" from the data migration before asserting uniqueness.
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="a")
    with transaction.atomic(), pytest.raises(IntegrityError):
        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="b")


def test_only_one_global_default(db):
    # Clear the seeded GLOBAL default from the data migration so this test owns the row.
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="A", base_image="a", is_default=True)
    with transaction.atomic(), pytest.raises(IntegrityError):
        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="B", base_image="b", is_default=True)


def test_is_default_only_on_global(db, user):
    with transaction.atomic(), pytest.raises(IntegrityError):
        SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="x", base_image="a", is_default=True)


def test_env_vars_encryption_round_trip(db, user):
    env = SandboxEnvironment.objects.create(
        scope=Scope.USER,
        user=user,
        name="dev",
        base_image="python:3.12",
        env_vars=[{"name": "TOKEN", "value": "s3cret", "is_secret": True}],
    )
    env.refresh_from_db()
    assert env.env_vars == [{"name": "TOKEN", "value": "s3cret", "is_secret": True}]
    # Raw column is ciphertext, not plaintext.
    assert "s3cret" not in (env._env_vars_encrypted or "")


def test_promote_as_default_atomically_swaps_existing_default(db):
    """Calling promote_as_default on a new GLOBAL env must demote the prior
    default so the partial unique index ``env_one_global_default`` is satisfied."""
    SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()
    old = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Old", base_image="a", is_default=True)
    new = SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="New", base_image="b", is_default=False)
    new.promote_as_default()
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_default is False
    assert new.is_default is True


def test_promote_as_default_rejects_user_scope(db, user):
    from django.core.exceptions import ValidationError

    env = SandboxEnvironment.objects.create(scope=Scope.USER, user=user, name="dev", base_image="a")
    with pytest.raises(ValidationError):
        env.promote_as_default()
    env.refresh_from_db()
    assert env.is_default is False
