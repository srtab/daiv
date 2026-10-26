import pytest
from pydantic import SecretStr

from core.sandbox.egress import (
    PLATFORM_EGRESS_SECRET_NAME,
    PlatformCredential,
    is_platform_secret_name,
    with_platform_credentials,
)
from core.sandbox.schemas import EgressConfigRequest, EgressPolicy, EgressRule, EgressSecret

HOST = "github.com"
HEADER = "Authorization"


def _token(value: str) -> SecretStr:
    return SecretStr(f"Basic {value}")


def _apply(egress: EgressConfigRequest | None, token: SecretStr | None) -> EgressConfigRequest:
    return with_platform_credentials(egress, [PlatformCredential(host=HOST, header=HEADER, value=token)])


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


MAIN = PlatformCredential(host="gitlab.com", header="Authorization", value=SecretStr("M"), path="/g/app.git")
MOD = PlatformCredential(host="gitlab.com", header="Authorization", value=SecretStr("S"), path="/g/mod.git")


def test_main_credential_is_host_wide_and_siblings_are_path_scoped():
    cfg = with_platform_credentials(None, [MAIN, MOD])

    assert [(r.host, r.path_prefix, r.inject) for r in cfg.policy.rules] == [
        ("gitlab.com", "/g/mod.git", f"{PLATFORM_EGRESS_SECRET_NAME}1"),
        ("gitlab.com", None, PLATFORM_EGRESS_SECRET_NAME),
    ]
    assert cfg.secrets[f"{PLATFORM_EGRESS_SECRET_NAME}1"].value.get_secret_value() == "S"


def test_reapplying_replaces_every_platform_rule_and_secret():
    first = with_platform_credentials(None, [MAIN, MOD])

    again = with_platform_credentials(first, [MAIN])

    assert len(again.policy.rules) == 1
    assert set(again.secrets) == {PLATFORM_EGRESS_SECRET_NAME}


def test_environment_rules_are_kept_after_platform_rules():
    env = EgressConfigRequest(policy=EgressPolicy(rules=[EgressRule(host="pypi.org")]))

    result = with_platform_credentials(env, [MAIN, MOD])

    assert [r.host for r in result.policy.rules] == ["gitlab.com", "gitlab.com", "pypi.org"]


def test_tokenless_sibling_adds_no_rule():
    bare = PlatformCredential(host="gitlab.com", header="Authorization", value=None, path="/g/mod.git")

    assert len(with_platform_credentials(None, [MAIN, bare]).policy.rules) == 1


def test_a_missing_main_credential_keeps_siblings_path_scoped():
    cfg = with_platform_credentials(None, [None, MOD])

    assert [(r.path_prefix, r.inject) for r in cfg.policy.rules] == [("/g/mod.git", f"{PLATFORM_EGRESS_SECRET_NAME}1")]
    assert set(cfg.secrets) == {f"{PLATFORM_EGRESS_SECRET_NAME}1"}


def test_is_platform_secret_name():
    assert is_platform_secret_name(PLATFORM_EGRESS_SECRET_NAME)
    assert is_platform_secret_name(f"{PLATFORM_EGRESS_SECRET_NAME}3")
    assert not is_platform_secret_name("my_token")
