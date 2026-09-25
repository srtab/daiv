"""The git platform's part of a sandbox's egress config.

DAIV runs git, including the publish push, from inside the sandbox. So each run's egress config gets an
allow-rule for the repository's git host, placed first so it wins the sidecar's first-match, plus the
push token as an injected header when there is one. Both exist only at runtime: an environment never
stores them, and its validation rejects the reserved secret name.
"""

from pydantic import SecretStr  # noqa: TC002

from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

PLATFORM_EGRESS_SECRET_NAME = "__daiv_git_platform__"  # noqa: S105


def with_platform_credential(
    egress: EgressConfigRequest | None, host: str, header: str, token: SecretStr | None
) -> EgressConfigRequest:
    """Return ``egress`` with exactly one git-platform rule for ``host``, first, carrying ``token``.

    Idempotent: a platform rule and secret already present are replaced, not duplicated, so applying
    a fresh token to a credentialed config swaps its secret. With no ``egress`` the base is a
    deny-all policy; an existing policy keeps its ``default``, ``intercept`` and non-platform rules. Without
    a ``token`` the rule only makes the host reachable. ``egress`` is never mutated.
    """
    base_policy = egress.policy if egress is not None else EgressPolicy()
    secrets = dict(egress.secrets) if egress is not None else {}
    secrets.pop(PLATFORM_EGRESS_SECRET_NAME, None)

    inject = None
    if token is not None:
        inject = PLATFORM_EGRESS_SECRET_NAME
        secrets[PLATFORM_EGRESS_SECRET_NAME] = EgressSecret(header=header, value=token)

    platform_rule = EgressRule(host=host, methods=["*"], inject=inject)
    other_rules = [rule for rule in base_policy.rules if not _is_platform_rule(rule, host)]
    policy = base_policy.model_copy(update={"rules": [platform_rule, *other_rules]})
    return EgressConfigRequest(policy=policy, secrets=secrets)


def _is_platform_rule(rule: EgressRule, host: str) -> bool:
    # A token-less platform rule has no marker; an environment rule identical to it is already
    # shadowed by the platform rule in front of it, so dropping it changes nothing.
    return rule.inject == PLATFORM_EGRESS_SECRET_NAME or rule == EgressRule(host=host)
