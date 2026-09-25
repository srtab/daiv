import pytest
from pydantic import SecretStr

from core.sandbox.egress import PLATFORM_EGRESS_SECRET_NAME, with_platform_credential
from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

HOST = "github.com"
HEADER = "Authorization"


def _token(value: str) -> SecretStr:
    return SecretStr(f"Basic {value}")


def _env_egress() -> EgressConfigRequest:
    """An environment's own policy: one credentialed rule for another host."""
    return EgressConfigRequest(
        policy=EgressPolicy(
            default="deny",
            intercept="credentialed",
            rules=[EgressRule(host="api.openai.com", methods=["GET"], inject="s1")],
        ),
        secrets={"s1": EgressSecret(header="Authorization", value=SecretStr("sk"))},
    )


def test_it_opens_a_deny_all_base_for_the_git_host():
    result = with_platform_credential(None, HOST, HEADER, _token("abc"))

    assert (result.policy.default, result.policy.intercept) == ("deny", "all")
    assert result.policy.rules == [EgressRule(host=HOST, methods=["*"], inject=PLATFORM_EGRESS_SECRET_NAME)]
    assert result.secrets == {PLATFORM_EGRESS_SECRET_NAME: EgressSecret(header=HEADER, value=_token("abc"))}


def test_it_puts_the_platform_rule_first_and_keeps_the_environment_policy():
    result = with_platform_credential(_env_egress(), HOST, HEADER, _token("abc"))

    assert result.policy.intercept == "credentialed"
    assert [rule.host for rule in result.policy.rules] == [HOST, "api.openai.com"]
    assert set(result.secrets) == {"s1", PLATFORM_EGRESS_SECRET_NAME}


def test_without_a_token_the_rule_only_makes_the_host_reachable():
    result = with_platform_credential(None, HOST, HEADER, None)

    assert result.policy.rules == [EgressRule(host=HOST)]
    assert result.secrets == {}


@pytest.mark.parametrize("token", [pytest.param("abc", id="token"), pytest.param(None, id="token-less")])
def test_applying_the_same_credential_twice_yields_one_rule(token):
    value = _token(token) if token else None
    once = with_platform_credential(_env_egress(), HOST, HEADER, value)

    assert with_platform_credential(once, HOST, HEADER, value) == once


def test_a_fresh_token_swaps_the_secret_and_leaves_the_rest():
    env = _env_egress()
    stale = with_platform_credential(env, HOST, HEADER, _token("STALE"))

    fresh = with_platform_credential(stale, HOST, HEADER, _token("FRESH"))

    assert fresh == with_platform_credential(env, HOST, HEADER, _token("FRESH"))
    assert fresh.secrets["s1"] == env.secrets["s1"]
    assert stale.secrets[PLATFORM_EGRESS_SECRET_NAME].value == _token("STALE")


def test_a_token_less_credential_drops_an_earlier_platform_token():
    credentialed = with_platform_credential(_env_egress(), HOST, HEADER, _token("abc"))

    assert with_platform_credential(credentialed, HOST, HEADER, None) == with_platform_credential(
        _env_egress(), HOST, HEADER, None
    )


def test_an_environment_rule_for_the_git_host_stays_behind_the_platform_rule():
    env = EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host=HOST, methods=["GET"])]))

    result = with_platform_credential(env, HOST, HEADER, _token("abc"))

    assert result.policy.rules == [
        EgressRule(host=HOST, methods=["*"], inject=PLATFORM_EGRESS_SECRET_NAME),
        EgressRule(host=HOST, methods=["GET"]),
    ]
