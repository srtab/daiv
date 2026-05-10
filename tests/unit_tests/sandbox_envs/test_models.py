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
    SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="a")
    with transaction.atomic(), pytest.raises(IntegrityError):
        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="b")


def test_only_one_global_default(db):
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
