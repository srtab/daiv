"""The sandbox a run asks for, resolved from its environment and the GLOBAL default.

A :class:`SandboxSpec` holds only what the environments configure. ``set_runtime_ctx`` adds the
git-platform rule and credential per run (``RuntimeCtx.sandbox_egress``) and never stores them here,
so :attr:`SandboxSpec.fingerprint` changes only when the environment does.
"""

import json
from dataclasses import dataclass

from django.utils.crypto import salted_hmac

from core.sandbox.command_policy import SandboxCommandPolicy
from core.sandbox.schemas import EgressConfigRequest  # noqa: TC001


@dataclass(frozen=True)
class SandboxEnvOverride:
    """A resolved sandbox-env view with secrets decrypted: the input of :func:`merge_sandbox_spec`,
    built by :func:`sandbox_envs.services.row_to_override`."""

    base_image: str | None
    memory_bytes: int | None
    cpus: float | None
    env_vars: dict[str, str]
    egress: EgressConfigRequest | None = None


@dataclass(frozen=True)
class SandboxSpec:
    """Effective sandbox configuration for a run: the per-run env merged over the GLOBAL default.

    ``command_policy`` is currently always the empty default; per-env policies are a future iteration.
    """

    base_image: str | None
    memory_bytes: int | None
    cpus: float | None
    env_vars: dict[str, str]
    command_policy: SandboxCommandPolicy
    egress: EgressConfigRequest | None = None

    @property
    def enabled(self) -> bool:
        return self.base_image is not None

    @property
    def fingerprint(self) -> str:
        """A stable digest of what a sandbox container is built from.

        Covers the base image, memory, CPUs, env vars and the egress policy. Egress secrets are left
        out: a warm session gets their current values through ``update_egress``. Keyed on
        ``SECRET_KEY`` so a checkpoint never holds a plain hash of env var values, which may be secrets.
        """
        payload = json.dumps(
            {
                "base_image": self.base_image,
                "memory_bytes": self.memory_bytes,
                "cpus": self.cpus,
                "env_vars": self.env_vars,
                "egress": self.egress.policy.model_dump(mode="json") if self.egress is not None else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return salted_hmac("sandbox_envs.spec.SandboxSpec.fingerprint", payload, algorithm="sha256").hexdigest()


def merge_sandbox_spec(*, per_run: SandboxEnvOverride | None, global_default: SandboxEnvOverride | None) -> SandboxSpec:
    """Resolve the effective sandbox spec from a per-run env + GLOBAL default.

    For each resource field (``base_image``, ``memory_bytes``, ``cpus``): the
    per-run env wins when its value is non-None; otherwise the GLOBAL default
    wins; otherwise the field's runtime default applies.

    ``env_vars`` are unioned with per-run keys shadowing GLOBAL keys.
    ``egress`` is taken from the effective env as-is (see inline comment).
    ``command_policy`` defaults to an empty policy; built-in safety rules in
    :mod:`core.sandbox.command_policy` still apply.
    """

    def pick(field: str, runtime_default):
        if per_run is not None:
            v = getattr(per_run, field)
            if v is not None:
                return v
        if global_default is not None:
            v = getattr(global_default, field)
            if v is not None:
                return v
        return runtime_default

    return SandboxSpec(
        base_image=pick("base_image", None),
        memory_bytes=pick("memory_bytes", None),
        cpus=pick("cpus", None),
        # Network is explicit per env (no inherit): take the effective env's egress as-is. A per-run env
        # that is Off (egress=None) must NOT inherit the global default's policy, so this is not pick().
        egress=(
            per_run.egress if per_run is not None else (global_default.egress if global_default is not None else None)
        ),
        env_vars={**(global_default.env_vars if global_default else {}), **(per_run.env_vars if per_run else {})},
        command_policy=SandboxCommandPolicy(),
    )
