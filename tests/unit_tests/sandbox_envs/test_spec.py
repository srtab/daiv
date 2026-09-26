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


def _ov(**fields) -> SandboxEnvOverride:
    return SandboxEnvOverride(**({"base_image": None, "memory_bytes": None, "cpus": None, "env_vars": {}} | fields))


class TestMergeSandboxSpec:
    def test_per_run_env_supplies_fields_when_set(self):
        per_run = _ov(
            base_image="python:3.14",
            memory_bytes=2 * 2**30,
            cpus=2.0,
            env_vars={"K": "v"},
            egress=_egress("per-run.example"),
        )
        global_default = _ov(base_image="python:3.12", memory_bytes=1 * 2**30, cpus=1.0, env_vars={"G": "g"})
        spec = merge_sandbox_spec(per_run=per_run, global_default=global_default)
        assert spec.base_image == "python:3.14"
        assert spec.egress is not None
        assert spec.egress.policy.rules[0].host == "per-run.example"
        assert spec.memory_bytes == 2 * 2**30
        assert spec.cpus == 2.0
        assert spec.env_vars == {"G": "g", "K": "v"}

    def test_per_run_off_blocks_global_egress_inheritance(self):
        per_run = _ov(env_vars={"K": "v"})
        global_default = _ov(
            base_image="python:3.12",
            memory_bytes=1 * 2**30,
            cpus=1.0,
            env_vars={"G": "g"},
            egress=_egress("global.example"),
        )
        spec = merge_sandbox_spec(per_run=per_run, global_default=global_default)
        assert spec.base_image == "python:3.12"
        assert spec.egress is None
        assert spec.memory_bytes == 1 * 2**30
        assert spec.cpus == 1.0
        assert spec.env_vars == {"G": "g", "K": "v"}

    def test_per_run_env_vars_shadow_global_on_key_collision(self):
        per_run = _ov(env_vars={"SHARED": "from-per-run", "PER_RUN_ONLY": "x"})
        global_default = _ov(base_image="python:3.12", env_vars={"SHARED": "from-global", "GLOBAL_ONLY": "g"})
        spec = merge_sandbox_spec(per_run=per_run, global_default=global_default)
        assert spec.env_vars == {"SHARED": "from-per-run", "PER_RUN_ONLY": "x", "GLOBAL_ONLY": "g"}

    def test_command_policy_defaults_empty(self):
        spec = merge_sandbox_spec(per_run=_ov(base_image="python:3.14"), global_default=None)
        assert spec.command_policy == SandboxCommandPolicy()

    def test_per_run_egress_wins_over_global_egress(self):
        spec = merge_sandbox_spec(
            per_run=_ov(egress=_egress("per-run.example")), global_default=_ov(egress=_egress("global.example"))
        )
        assert spec.egress.policy.rules[0].host == "per-run.example"

    def test_egress_is_none_when_neither_side_has_it(self):
        spec = merge_sandbox_spec(per_run=_ov(), global_default=_ov())
        assert spec.egress is None

    def test_falls_back_to_global_egress_when_no_per_run(self):
        egress = _egress("global.example")
        spec = merge_sandbox_spec(per_run=None, global_default=_ov(egress=egress))
        assert spec.egress is egress
