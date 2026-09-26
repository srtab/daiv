import pytest
from pydantic import SecretStr

from core.sandbox.egress import PLATFORM_EGRESS_SECRET_NAME, with_platform_credential
from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

HOST = "github.com"
HEADER = "Authorization"


def _token(value: str) -> SecretStr:
    return SecretStr(f"Basic {value}")


def _apply(egress: EgressConfigRequest | None, token: SecretStr | None) -> EgressConfigRequest:
    return with_platform_credential(egress, host=HOST, header=HEADER, token=token)


def _env_egress() -> EgressConfigRequest:
    """An environment's own policy, both modes off their defaults: one credentialed rule for another host."""
    return EgressConfigRequest(
        policy=EgressPolicy(
            default="allow",
            intercept="credentialed",
            rules=[EgressRule(host="api.openai.com", methods=["GET"], inject="s1")],
        ),
        secrets={"s1": EgressSecret(header="Authorization", value=SecretStr("sk"))},
    )


def test_it_opens_a_deny_all_base_for_the_git_host():
    result = _apply(None, _token("abc"))

    assert (result.policy.default, result.policy.intercept) == ("deny", "all")
    assert result.policy.rules == [EgressRule(host=HOST, methods=["*"], inject=PLATFORM_EGRESS_SECRET_NAME)]
    assert result.secrets == {PLATFORM_EGRESS_SECRET_NAME: EgressSecret(header=HEADER, value=_token("abc"))}


def test_it_puts_the_platform_rule_first_and_keeps_the_environment_policy():
    result = _apply(_env_egress(), _token("abc"))

    assert (result.policy.default, result.policy.intercept) == ("allow", "credentialed")
    assert [rule.host for rule in result.policy.rules] == [HOST, "api.openai.com"]
    assert set(result.secrets) == {"s1", PLATFORM_EGRESS_SECRET_NAME}


def test_without_a_token_the_rule_only_makes_the_host_reachable():
    result = _apply(None, None)

    assert result.policy.rules == [EgressRule(host=HOST)]
    assert result.secrets == {}


@pytest.mark.parametrize("token", [pytest.param("abc", id="token"), pytest.param(None, id="token-less")])
def test_applying_the_same_credential_twice_yields_one_rule(token):
    value = _token(token) if token else None
    once = _apply(_env_egress(), value)

    assert _apply(once, value) == once


def test_a_fresh_token_swaps_the_secret_and_leaves_the_rest():
    env = _env_egress()
    stale = _apply(env, _token("STALE"))

    fresh = _apply(stale, _token("FRESH"))

    assert fresh == _apply(env, _token("FRESH"))
    assert fresh.secrets["s1"] == env.secrets["s1"]
    assert stale.secrets[PLATFORM_EGRESS_SECRET_NAME].value == _token("STALE")


def test_a_token_less_credential_drops_an_earlier_platform_token():
    credentialed = _apply(_env_egress(), _token("abc"))

    assert _apply(credentialed, None) == _apply(_env_egress(), None)


def test_an_environment_rule_for_the_git_host_stays_behind_the_platform_rule():
    env = EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host=HOST, methods=["GET"])]))

    result = _apply(env, _token("abc"))

    assert result.policy.rules == [
        EgressRule(host=HOST, methods=["*"], inject=PLATFORM_EGRESS_SECRET_NAME),
        EgressRule(host=HOST, methods=["GET"]),
    ]
