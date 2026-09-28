import pytest
from asgiref.sync import async_to_sync
from sandbox_envs.models import SandboxEnvironment, Scope
from sandbox_envs.selection import aresolve_repo_envs, resolve_env_for_run
from sessions.services import RepoTarget

from accounts.models import User


@pytest.mark.django_db
class TestResolveEnvForRun:
    @pytest.fixture(autouse=True)
    def _clear_global(self):
        """Remove migration-seeded global envs so each test starts from scratch."""
        SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()

    def _user(self, name="u"):
        return User.objects.create(username=name, email=f"{name}@x.test")

    def test_returns_none_when_no_repo_and_no_global_default(self):
        user = self._user()

        result = async_to_sync(resolve_env_for_run)(user=user, repo_id=None)
        assert result is None

    def test_returns_global_default_when_no_repo(self):
        user = self._user()

        default_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id=None)
        assert result == default_env

    def test_returns_user_env_matching_repo(self):
        user = self._user()

        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        user_env = SandboxEnvironment.objects.create(
            scope=Scope.USER, user=user, name="me", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id="acme/foo")
        assert result == user_env

    def test_user_env_beats_global_env_for_same_repo(self):
        user = self._user()

        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="org-env", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        user_env = SandboxEnvironment.objects.create(
            scope=Scope.USER, user=user, name="my-env", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id="acme/foo")
        assert result == user_env

    def test_global_env_matches_when_no_user_env(self):
        user = self._user()

        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        global_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="org-env", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id="acme/foo")
        assert result == global_env

    def test_falls_back_to_global_default_when_no_match(self):
        user = self._user()

        default_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
        )
        SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="org-env", base_image="python:3.14", repo_ids=["other/repo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=user, repo_id="acme/foo")
        assert result == default_env

    def test_does_not_return_other_users_user_env(self):
        u1 = self._user("u1")
        u2 = self._user("u2")

        default_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True
        )
        SandboxEnvironment.objects.create(
            scope=Scope.USER, user=u2, name="theirs", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=u1, repo_id="acme/foo")
        assert result == default_env

    def test_anonymous_user_uses_global_only(self):
        SandboxEnvironment.objects.create(scope=Scope.GLOBAL, name="Default", base_image="python:3.14", is_default=True)
        global_env = SandboxEnvironment.objects.create(
            scope=Scope.GLOBAL, name="org-env", base_image="python:3.14", repo_ids=["acme/foo"]
        )
        result = async_to_sync(resolve_env_for_run)(user=None, repo_id="acme/foo")
        assert result == global_env


@pytest.mark.django_db(transaction=True)
class TestAresolveRepoEnvs:
    """Direct coverage for aresolve_repo_envs precedence + edge cases.

    View-level tests exercise this indirectly through single-repo cases. These
    pin the in-memory precedence ladder and protect contracts shared by all
    batch call sites.
    """

    @pytest.fixture(autouse=True)
    def _clear_global(self):
        SandboxEnvironment.objects.filter(scope=Scope.GLOBAL).delete()

    async def test_explicit_env_id_stamps_all_targets(self):
        resolved = await aresolve_repo_envs(
            user=None,
            repos=[RepoTarget(repo_id="a/b"), RepoTarget(repo_id="c/d")],
            explicit_env_id="00000000-0000-0000-0000-000000000099",
        )
        assert [t.sandbox_environment_id for t in resolved] == [
            "00000000-0000-0000-0000-000000000099",
            "00000000-0000-0000-0000-000000000099",
        ]

    async def test_empty_repos_returns_empty_list(self):
        assert await aresolve_repo_envs(user=None, repos=[], explicit_env_id=None) == []
        assert await aresolve_repo_envs(user=None, repos=[], explicit_env_id="x") == []

    async def test_user_env_wins_over_global_when_both_match_repo(self):
        user = await User.objects.acreate(username="u", email="u@x.test")
        user_env = await SandboxEnvironment.objects.acreate(
            scope=Scope.USER, user=user, name="mine", base_image="x", repo_ids=["a/b"]
        )
        await SandboxEnvironment.objects.acreate(scope=Scope.GLOBAL, name="theirs", base_image="x", repo_ids=["a/b"])
        resolved = await aresolve_repo_envs(user=user, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id == str(user_env.id)

    async def test_global_repo_match_wins_over_default(self):
        await SandboxEnvironment.objects.acreate(scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True)
        repo_env = await SandboxEnvironment.objects.acreate(
            scope=Scope.GLOBAL, name="match", base_image="x", repo_ids=["a/b"]
        )
        resolved = await aresolve_repo_envs(user=None, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id == str(repo_env.id)

    async def test_no_envs_at_all_yields_none(self):
        resolved = await aresolve_repo_envs(user=None, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id is None

    async def test_envs_with_empty_repo_ids_do_not_match(self):
        """An env with an empty ``repo_ids`` list must not match any repo and must fall
        through to the GLOBAL default."""

        default = await SandboxEnvironment.objects.acreate(
            scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True, repo_ids=[]
        )
        await SandboxEnvironment.objects.acreate(
            scope=Scope.GLOBAL, name="empty", base_image="x", is_default=False, repo_ids=[]
        )
        resolved = await aresolve_repo_envs(user=None, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id == str(default.id)

    async def test_user_scope_skipped_for_anonymous_or_none(self):
        other = await User.objects.acreate(username="o", email="o@x.test")
        await SandboxEnvironment.objects.acreate(
            scope=Scope.USER, user=other, name="other-env", base_image="x", repo_ids=["a/b"]
        )
        default = await SandboxEnvironment.objects.acreate(
            scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True
        )
        # user=None must not leak the other user's USER env.
        resolved = await aresolve_repo_envs(user=None, repos=[RepoTarget(repo_id="a/b")], explicit_env_id=None)
        assert resolved[0].sandbox_environment_id == str(default.id)

    async def test_input_targets_not_mutated(self):
        await SandboxEnvironment.objects.acreate(scope=Scope.GLOBAL, name="Default", base_image="x", is_default=True)
        original = [RepoTarget(repo_id="a/b"), RepoTarget(repo_id="c/d", ref="dev")]
        await aresolve_repo_envs(user=None, repos=original, explicit_env_id=None)
        assert all(t.sandbox_environment_id is None for t in original)
        assert original[1].ref == "dev"
