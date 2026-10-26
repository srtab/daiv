"""The git platform's part of a sandbox's egress config.

DAIV runs git, including the publish push, from inside the sandbox. So each run's egress config gets an
allow-rule for each repository's git host, placed first so it wins the sidecar's first-match, plus each
push token as an injected header when there is one. The first credential is host-wide; the others are
scoped to their repository's path so each repository's git traffic carries its own token.
Both exist only at runtime: an environment never stores them, and its validation rejects the reserved secret name.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import SecretStr  # noqa: TC002

from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

if TYPE_CHECKING:
    from collections.abc import Sequence

PLATFORM_EGRESS_SECRET_NAME = "__daiv_git_platform__"  # noqa: S105


@dataclass(frozen=True)
class PlatformCredential:
    host: str
    header: str
    token: SecretStr | None
    path: str = ""


def is_platform_secret_name(name: str) -> bool:
    suffix = name.removeprefix(PLATFORM_EGRESS_SECRET_NAME)
    return name.startswith(PLATFORM_EGRESS_SECRET_NAME) and (suffix == "" or suffix.isdecimal())


def with_platform_credentials(
    egress: EgressConfigRequest | None, credentials: Sequence[PlatformCredential]
) -> EgressConfigRequest:
    """Return ``egress`` with the git-platform rules for ``credentials``, first, carrying their tokens.

    The first credential is the main repository's: a host-wide rule injecting ``PLATFORM_EGRESS_SECRET_NAME``.
    Each later one is limited to its ``path`` and injects ``PLATFORM_EGRESS_SECRET_NAME<i>``, placed before
    the host-wide rule so the sidecar's first match gives each repository its own token. A later credential
    without a token adds no rule: the main rule already makes the host reachable.

    Idempotent: platform rules and secrets already present are replaced, not duplicated. With no ``egress``
    the base is a deny-all policy; an existing policy keeps its ``default``, ``intercept`` and non-platform
    rules. ``egress`` is never mutated.
    """
    base_policy = egress.policy if egress is not None else EgressPolicy()
    secrets = {
        name: s
        for name, s in (egress.secrets if egress is not None else {}).items()
        if not is_platform_secret_name(name)
    }

    scoped: list[EgressRule] = []
    main_rule: EgressRule | None = None
    for index, credential in enumerate(credentials):
        inject = None
        if credential.token is not None:
            inject = PLATFORM_EGRESS_SECRET_NAME if index == 0 else f"{PLATFORM_EGRESS_SECRET_NAME}{index}"
            secrets[inject] = EgressSecret(header=credential.header, value=credential.token)
        if index == 0:
            main_rule = EgressRule(host=credential.host, methods=["*"], inject=inject)
        elif inject is not None:
            scoped.append(
                EgressRule(host=credential.host, methods=["*"], inject=inject, path_prefix=credential.path or None)
            )

    hosts = {credential.host for credential in credentials}
    platform_rules = [*reversed(scoped), *([main_rule] if main_rule is not None else [])]
    other_rules = [rule for rule in base_policy.rules if not _is_platform_rule(rule, hosts)]
    policy = base_policy.model_copy(update={"rules": [*platform_rules, *other_rules]})
    return EgressConfigRequest(policy=policy, secrets=secrets)


def _is_platform_rule(rule: EgressRule, hosts: set[str]) -> bool:
    # A token-less platform rule has no marker; an environment rule identical to it is already
    # shadowed by the platform rule in front of it, so dropping it changes nothing.
    return (rule.inject is not None and is_platform_secret_name(rule.inject)) or any(
        rule == EgressRule(host=host) for host in hosts
    )
