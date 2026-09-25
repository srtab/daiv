import pytest
from pydantic import SecretStr
from sandbox_envs.spec import SandboxEnvOverride, SandboxSpec, merge_sandbox_spec

from core.sandbox.command_policy import SandboxCommandPolicy
from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret


def _egress(host: str = "api.example.com", *, secret: str = "sk-1", methods: tuple[str, ...] = ("*",)):  # noqa: S107
    return EgressConfigRequest(
        policy=EgressPolicy(rules=[EgressRule(host=host, methods=list(methods), inject="s")]),
        secrets={"s": EgressSecret(header="Authorization", value=SecretStr(secret))},
    )


def _spec(**changes) -> SandboxSpec:
    fields = {
        "base_image": "python:3.14",
        "memory_bytes": 2 * 2**30,
        "cpus": 2.0,
        "env_vars": {"A": "1", "B": "2"},
        "command_policy": SandboxCommandPolicy(),
        "egress": _egress(),
    }
    return SandboxSpec(**(fields | changes))


class TestFingerprint:
    def test_it_is_the_same_for_an_equal_spec(self):
        assert _spec().fingerprint == _spec(env_vars={"B": "2", "A": "1"}).fingerprint

    @pytest.mark.parametrize(
        "change",
        [
            pytest.param({"base_image": "python:3.12"}, id="base-image"),
            pytest.param({"memory_bytes": 4 * 2**30}, id="memory"),
            pytest.param({"cpus": 1.0}, id="cpus"),
            pytest.param({"env_vars": {"A": "1", "B": "3"}}, id="env-var-value"),
            pytest.param({"egress": None}, id="network-off"),
            pytest.param({"egress": _egress("other.example.com")}, id="egress-host"),
            pytest.param({"egress": _egress(methods=("GET",))}, id="egress-methods"),
        ],
    )
    def test_it_changes_with_what_the_container_is_built_from(self, change):
        assert _spec(**change).fingerprint != _spec().fingerprint

    def test_an_egress_secret_value_does_not_change_it(self):
        """A warm session gets current secret values through ``update_egress``, so a rotation is no rebuild."""
        assert _spec(egress=_egress(secret="sk-2")).fingerprint == _spec().fingerprint  # noqa: S106

    def test_it_is_keyed_on_the_secret_key(self, settings):
        """So a checkpoint never holds a plain hash of env var values, which may be secrets."""
        before = _spec().fingerprint
        settings.SECRET_KEY = "another-secret-key"  # noqa: S105

        assert _spec().fingerprint != before


@pytest.mark.django_db
class TestMergeSandboxSpec:
    def test_per_run_env_supplies_fields_when_set(self):
        egress_on = EgressConfigRequest(
            policy=EgressPolicy(rules=[EgressRule(host="per-run.example", inject="t")]),
            secrets={"t": EgressSecret(header="Authorization", value=SecretStr("Bearer x"))},
        )
        per_run = SandboxEnvOverride(
            base_image="python:3.14", memory_bytes=2 * 2**30, cpus=2.0, env_vars={"K": "v"}, egress=egress_on
        )
        global_default = SandboxEnvOverride(
            base_image="python:3.12", memory_bytes=1 * 2**30, cpus=1.0, env_vars={"G": "g"}, egress=None
        )
        runtime = merge_sandbox_spec(per_run=per_run, global_default=global_default)
        assert runtime.base_image == "python:3.14"
        # per-run egress takes precedence; network is on (egress is not None)
        assert runtime.egress is not None
        assert runtime.egress.policy.rules[0].host == "per-run.example"
        assert runtime.memory_bytes == 2 * 2**30
        assert runtime.cpus == 2.0
        assert runtime.env_vars == {"G": "g", "K": "v"}

    def test_per_run_off_blocks_global_egress_inheritance(self):
        egress_global = EgressConfigRequest(
            policy=EgressPolicy(rules=[EgressRule(host="global.example", inject="t")]),
            secrets={"t": EgressSecret(header="Authorization", value=SecretStr("Bearer x"))},
        )
        per_run = SandboxEnvOverride(base_image=None, memory_bytes=None, cpus=None, env_vars={"K": "v"}, egress=None)
        global_default = SandboxEnvOverride(
            base_image="python:3.12", memory_bytes=1 * 2**30, cpus=1.0, env_vars={"G": "g"}, egress=egress_global
        )
        runtime = merge_sandbox_spec(per_run=per_run, global_default=global_default)
        assert runtime.base_image == "python:3.12"
        # per-run has no egress (off); global_default's egress is NOT inherited
        # (network is per-env, not fallthrough — per-run off beats global on)
        assert runtime.egress is None
        assert runtime.memory_bytes == 1 * 2**30
        assert runtime.cpus == 1.0
        assert runtime.env_vars == {"G": "g", "K": "v"}

    def test_per_run_env_vars_shadow_global_on_key_collision(self):
        per_run = SandboxEnvOverride(
            base_image=None, memory_bytes=None, cpus=None, env_vars={"SHARED": "from-per-run", "PER_RUN_ONLY": "x"}
        )
        global_default = SandboxEnvOverride(
            base_image="python:3.12",
            memory_bytes=None,
            cpus=None,
            env_vars={"SHARED": "from-global", "GLOBAL_ONLY": "g"},
        )
        runtime = merge_sandbox_spec(per_run=per_run, global_default=global_default)
        assert runtime.env_vars == {"SHARED": "from-per-run", "PER_RUN_ONLY": "x", "GLOBAL_ONLY": "g"}

    def test_command_policy_defaults_empty(self):
        per_run = SandboxEnvOverride(base_image="python:3.14", memory_bytes=None, cpus=None, env_vars={})
        runtime = merge_sandbox_spec(per_run=per_run, global_default=None)
        assert runtime.command_policy == SandboxCommandPolicy()


def _egress_request(host: str):
    return EgressConfigRequest(
        policy=EgressPolicy(rules=[EgressRule(host=host, inject="t")]),
        secrets={"t": EgressSecret(header="Authorization", value=SecretStr("Bearer x"))},
    )


def test_merge_prefers_per_run_egress():
    rt = merge_sandbox_spec(
        per_run=_ov(egress=_egress_request("per-run.example")),
        global_default=_ov(egress=_egress_request("global.example")),
    )
    assert rt.egress.policy.rules[0].host == "per-run.example"


def test_merge_egress_is_none_when_neither_side_has_it():
    # Egress is opt-in: no policy on either side must never materialize one (it would otherwise
    # silently apply an unintended network posture).
    rt = merge_sandbox_spec(per_run=_ov(egress=None), global_default=_ov(egress=None))
    assert rt.egress is None


@pytest.fixture
def make_egress():
    """Return a factory that builds an EgressConfigRequest for a single host."""

    def _factory(hosts: list[str]):
        rules = [EgressRule(host=h, inject="t") for h in hosts]
        return EgressConfigRequest(
            policy=EgressPolicy(rules=rules),
            secrets={"t": EgressSecret(header="Authorization", value=SecretStr("Bearer x"))},
        )

    return _factory


def _ov(**kw):
    base = {"base_image": None, "memory_bytes": None, "cpus": None, "env_vars": {}, "egress": None}
    base.update(kw)
    return SandboxEnvOverride(**base)


def test_sandbox_env_override_has_no_network_enabled():
    assert not hasattr(_ov(), "network_enabled")


def test_merge_takes_per_run_egress_off_even_when_global_has_policy(make_egress):
    # per-run env explicitly Off (egress=None) must NOT inherit the global default's policy.
    per_run = _ov(egress=None)
    global_default = _ov(egress=make_egress(["github.com"]))
    rt = merge_sandbox_spec(per_run=per_run, global_default=global_default)
    assert rt.egress is None
    assert not hasattr(rt, "network_enabled")


def test_merge_falls_back_to_global_egress_when_no_per_run():
    eg = object()
    rt = merge_sandbox_spec(per_run=None, global_default=_ov(egress=eg))
    assert rt.egress is eg
