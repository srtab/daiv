from typing import TYPE_CHECKING
from unittest.mock import patch

import openai
import pytest

from automation.agent.base import CLAUDE_THINKING_MODELS, OPENAI_THINKING_MODELS, get_model, get_model_kwargs
from automation.agent.chat_models import ChatOpenRouter
from automation.agent.model_spec import parse_model_spec
from automation.agent.provider_clients import OPENROUTER_BASE_URL
from core.constants import ModelName
from core.models import Provider, ProviderType, ThinkingLevelChoices

if TYPE_CHECKING:
    import httpx


@pytest.mark.django_db
class TestGetModelKwargs:
    def _enable_seed(self, slug: str, api_key: str = "k") -> None:
        p = Provider.objects.get(slug=slug)
        p.api_key = api_key
        p.is_enabled = True
        p.save()

    def test_disabled_provider_raises(self):
        Provider.objects.create(
            slug="off", display_name="off", provider_type=ProviderType.OPENAI, api_key="k", is_enabled=False
        )
        with pytest.raises(RuntimeError, match="disabled"):
            get_model_kwargs(resolved=parse_model_spec("off:gpt-5.4"))

    def test_keyless_provider_raises(self):
        Provider.objects.create(slug="nokey", display_name="No key", provider_type=ProviderType.OPENAI)
        with pytest.raises(RuntimeError, match="no API key"):
            get_model_kwargs(resolved=parse_model_spec("nokey:gpt-5.4"))

    def test_anthropic_row(self):
        Provider.objects.create(
            slug="anth2",
            display_name="A2",
            provider_type=ProviderType.ANTHROPIC,
            api_key="sk-a",
            base_url="https://proxy.example.com",
        )
        kw = get_model_kwargs(resolved=parse_model_spec("anth2:claude-haiku-4-5"))
        assert kw["model_provider"] == ProviderType.ANTHROPIC.value
        assert kw["api_key"] == "sk-a"
        assert kw["base_url"] == "https://proxy.example.com"
        assert kw["model"] == "claude-haiku-4-5"
        # Models that still accept ``temperature`` keep the seeded default.
        assert kw["temperature"] == 0

    @pytest.mark.parametrize(
        ("slug", "provider_type", "model_name"),
        [
            ("anth5", ProviderType.ANTHROPIC, "claude-sonnet-5"),
            ("or5", ProviderType.OPENROUTER, "anthropic/claude-sonnet-5"),
        ],
    )
    def test_claude5_omits_temperature(self, slug, provider_type, model_name):
        """Claude 5-generation models reject ``temperature`` (400 invalid_request_error), so it
        must be stripped — covers direct-Anthropic and OpenRouter slug forms. See Sentry DAIV-9P."""
        Provider.objects.create(slug=slug, display_name=slug, provider_type=provider_type, api_key="sk")
        kw = get_model_kwargs(resolved=parse_model_spec(f"{slug}:{model_name}"))
        assert kw["model"] == model_name
        assert "temperature" not in kw

    @pytest.mark.parametrize(
        ("slug", "model_name", "thinking_level"),
        [
            ("openai", "gpt-6-luna", None),
            ("openai", "gpt-6-sol", ThinkingLevelChoices.MEDIUM),
            ("openrouter", "openai/gpt-6-luna", None),
            ("openrouter", "openai/gpt-6-sol", ThinkingLevelChoices.MEDIUM),
        ],
    )
    def test_gpt6_omits_temperature(self, slug, model_name, thinking_level):
        """GPT-6 models reject ``temperature`` outright, and langchain-openai only strips it
        for ``gpt-5*``. See Sentry DAIV-CK."""
        self._enable_seed(slug, "sk")
        kw = get_model_kwargs(resolved=parse_model_spec(f"{slug}:{model_name}"), thinking_level=thinking_level)
        assert kw["model"] == model_name
        assert "temperature" not in kw

    def test_openai_compatible_with_custom_base_url_defaults_to_chat_completions(self):
        """Custom OpenAI-compatible rows default to ``use_responses_api=False`` because
        most ``/v1/chat/completions``-only servers reject ``/v1/responses``."""
        Provider.objects.create(
            slug="vllm",
            display_name="vLLM",
            provider_type=ProviderType.OPENAI,
            api_key="sk-v",
            base_url="http://localhost:8000/v1",
            extra_headers={"X-Trace": "yes"},
        )
        kw = get_model_kwargs(resolved=parse_model_spec("vllm:llama-3.3-70b"))
        assert kw["model_provider"] == ProviderType.OPENAI.value
        assert kw["api_key"] == "sk-v"
        assert kw["openai_api_base"] == "http://localhost:8000/v1"
        assert kw["model_kwargs"]["extra_headers"] == {"X-Trace": "yes"}
        assert "use_responses_api" not in kw

    def test_openai_compatible_opted_into_responses_api(self):
        Provider.objects.create(
            slug="bedrock",
            display_name="Bedrock",
            provider_type=ProviderType.OPENAI,
            api_key="sk-b",
            base_url="https://bedrock.example.com/v1",
            use_responses_api=True,
        )
        kw = get_model_kwargs(resolved=parse_model_spec("bedrock:gpt-5.4"))
        assert kw["use_responses_api"] is True

    def test_seed_openai_row_keeps_responses_api(self):
        """Migration enables Responses API for the locked ``openai`` seed row."""
        self._enable_seed("openai", "sk-o")
        kw = get_model_kwargs(resolved=parse_model_spec("openai:gpt-5.4"))
        assert kw["use_responses_api"] is True

    def test_verify_ssl_disabled_injects_insecure_http_clients(self):
        import httpx

        Provider.objects.create(
            slug="selfsigned",
            display_name="Self-signed",
            provider_type=ProviderType.OPENAI,
            api_key="sk-s",
            base_url="https://internal.example.test/v1",
            verify_ssl=False,
        )
        kw = get_model_kwargs(resolved=parse_model_spec("selfsigned:gpt-5.4"))
        assert isinstance(kw["http_client"], httpx.Client)
        assert isinstance(kw["http_async_client"], httpx.AsyncClient)

    def test_verify_ssl_enabled_omits_http_clients(self):
        Provider.objects.create(
            slug="secure",
            display_name="Secure",
            provider_type=ProviderType.OPENAI,
            api_key="sk-x",
            base_url="https://api.example.com/v1",
        )
        kw = get_model_kwargs(resolved=parse_model_spec("secure:gpt-5.4"))
        assert "http_client" not in kw
        assert "http_async_client" not in kw

    def test_verify_ssl_disabled_on_openrouter_injects_insecure_http_clients(self):
        """OpenRouter routes through ChatOpenAI, which accepts ``http_client``."""
        import httpx

        Provider.objects.create(
            slug="or_insec", display_name="OR", provider_type=ProviderType.OPENROUTER, api_key="sk-or", verify_ssl=False
        )
        kw = get_model_kwargs(resolved=parse_model_spec("or_insec:z-ai/glm-5"))
        assert isinstance(kw["http_client"], httpx.Client)
        assert isinstance(kw["http_async_client"], httpx.AsyncClient)

    @pytest.mark.parametrize("ptype", [ProviderType.GOOGLE_GENAI, ProviderType.ANTHROPIC])
    def test_verify_ssl_disabled_on_unsupported_type_logs_and_skips(self, caplog, ptype):
        """langchain-anthropic and langchain-google-genai don't expose ``http_client`` —
        toggling verify_ssl=False there is a silent no-op without the warn path."""
        Provider.objects.create(
            slug=f"skip_{ptype.value}", display_name="S", provider_type=ptype, api_key="sk-x", verify_ssl=False
        )
        model_name = "claude-haiku-4-5" if ptype == ProviderType.ANTHROPIC else "gemini-2.5-pro"
        with caplog.at_level("WARNING", logger="daiv.automation"):
            kw = get_model_kwargs(resolved=parse_model_spec(f"skip_{ptype.value}:{model_name}"))
        assert "http_client" not in kw
        assert any("SDK has no http_client hook" in rec.message for rec in caplog.records)

    def test_insecure_http_clients_closed_when_init_chat_model_fails(self):
        """Construction failures must not leak httpx pools."""

        Provider.objects.create(
            slug="badmodel",
            display_name="Bad",
            provider_type=ProviderType.OPENAI,
            api_key="sk-x",
            base_url="https://x.example/v1",
            verify_ssl=False,
        )
        sync_holder: list[httpx.Client] = []
        orig_init = sync_holder.append  # placeholder; reassigned via patch below

        with patch("automation.agent.base.init_chat_model") as mock_init:
            # Capture the clients before raising so we can assert they were closed.
            def capture_and_raise(**kwargs):
                sync_holder.append(kwargs["http_client"])
                sync_holder.append(kwargs["http_async_client"])
                raise RuntimeError("simulated wrapper construction failure")

            mock_init.side_effect = capture_and_raise

            with pytest.raises(RuntimeError, match="simulated"):
                get_model(model="badmodel:gpt-5.4")

        assert len(sync_holder) == 2
        sync_client, _async_client = sync_holder
        assert sync_client.is_closed
        del orig_init

    def test_get_model_openrouter_returns_chat_openrouter(self):
        self._enable_seed("openrouter", "sk-or")
        model = get_model(model="openrouter:anthropic/claude-sonnet-4.6")
        assert isinstance(model, ChatOpenRouter)
        assert model.openai_api_base == OPENROUTER_BASE_URL

    def test_get_model_non_openrouter_is_not_chat_openrouter(self):
        self._enable_seed("anthropic", "sk-a")
        model = get_model(model="anthropic:claude-sonnet-4-6")
        assert not isinstance(model, ChatOpenRouter)

    @pytest.mark.parametrize("thinking_level", [None, ThinkingLevelChoices.HIGH])
    @pytest.mark.parametrize("model", ["openrouter:z-ai/glm-5", "openrouter:anthropic/claude-sonnet-4.6"])
    def test_openrouter_routes_to_the_cheapest_provider(self, model, thinking_level):
        self._enable_seed("openrouter", "sk-or")
        kw = get_model_kwargs(resolved=parse_model_spec(model), thinking_level=thinking_level)
        assert kw["extra_body"]["provider"] == {"sort": "price"}

    def test_openrouter_anthropic_thinking(self):
        self._enable_seed("openrouter", "sk-or")
        kw = get_model_kwargs(
            resolved=parse_model_spec("openrouter:anthropic/claude-sonnet-4.6"),
            thinking_level=ThinkingLevelChoices.MEDIUM,
        )
        assert kw["openai_api_base"] == "https://openrouter.ai/api/v1"
        assert kw["api_key"] == "sk-or"
        assert "openai_api_key" not in kw
        assert kw["model_kwargs"]["extra_headers"]["HTTP-Referer"]
        assert kw["temperature"] == 1
        assert kw["max_tokens"] == 51_200
        assert kw["extra_body"]["reasoning"]["enabled"] is True
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.MEDIUM
        assert "max_tokens" not in kw["extra_body"]["reasoning"]

    def test_openrouter_anthropic_xhigh(self):
        """xhigh on Anthropic-via-OpenRouter passes the effort string straight through, even
        on 4.6: OpenRouter turns it into a ``budget_tokens`` ratio rather than forwarding it as
        ``output_config.effort``. ``max_tokens`` stays inside Anthropic's per-model cap (64K)."""
        self._enable_seed("openrouter", "sk-or")
        kw = get_model_kwargs(
            resolved=parse_model_spec("openrouter:anthropic/claude-sonnet-4.6"),
            thinking_level=ThinkingLevelChoices.XHIGH,
        )
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.XHIGH
        assert kw["max_tokens"] == 64_000

    def test_openrouter_generic_thinking(self):
        self._enable_seed("openrouter", "sk-or")
        kw = get_model_kwargs(
            resolved=parse_model_spec("openrouter:z-ai/glm-5"), thinking_level=ThinkingLevelChoices.HIGH
        )
        assert kw["extra_body"]["reasoning"]["enabled"] is True
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.HIGH

    def test_openrouter_generic_xhigh_passthrough(self):
        self._enable_seed("openrouter", "sk-or")
        kw = get_model_kwargs(
            resolved=parse_model_spec("openrouter:z-ai/glm-5"), thinking_level=ThinkingLevelChoices.XHIGH
        )
        assert kw["extra_body"]["reasoning"]["enabled"] is True
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.XHIGH
        # Generic models bypass the per-level max_tokens table; the Anthropic-only
        # path is what sets max_tokens, so confirm it doesn't leak here.
        assert "max_tokens" not in kw

    def test_google_genai(self):
        self._enable_seed("google_genai", "sk-g")
        kw = get_model_kwargs(resolved=parse_model_spec("google_genai:gemini-2.5-pro"))
        assert kw["model_provider"] == ProviderType.GOOGLE_GENAI.value
        assert kw["api_key"] == "sk-g"
        assert kw["include_thoughts"] is True

    def test_anthropic_no_thinking_sets_default_max_tokens(self):
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(resolved=parse_model_spec("anthropic:claude-sonnet-4-6"))
        assert kw["max_tokens"] == 16_384

    def test_openai_with_thinking_model(self):
        self._enable_seed("openai", "sk-o")
        kw = get_model_kwargs(
            resolved=parse_model_spec("openai:gpt-5.3-codex"), thinking_level=ThinkingLevelChoices.LOW
        )
        assert kw["temperature"] == 1
        assert kw["reasoning_effort"] == ThinkingLevelChoices.LOW

    @pytest.mark.parametrize("model_name", ["gpt-5.2", "gpt-5.3-codex", "gpt-5.4", "gpt-5.5", "gpt-5.6", "gpt-6-sol"])
    @pytest.mark.parametrize(
        ("thinking_level", "expected_effort"),
        [
            # No thinking-enabled OpenAI model accepts ``minimal``; all of them accept ``xhigh``.
            (ThinkingLevelChoices.MINIMAL, "low"),
            (ThinkingLevelChoices.LOW, "low"),
            (ThinkingLevelChoices.MEDIUM, "medium"),
            (ThinkingLevelChoices.HIGH, "high"),
            (ThinkingLevelChoices.XHIGH, "xhigh"),
        ],
    )
    def test_openai_effort_mapping(self, model_name, thinking_level, expected_effort):
        self._enable_seed("openai", "sk-o")
        kw = get_model_kwargs(resolved=parse_model_spec(f"openai:{model_name}"), thinking_level=thinking_level)
        assert kw["reasoning_effort"] == expected_effort

    def test_anthropic_xhigh_budget(self):
        """xhigh on a manual-thinking model stays at the Sonnet/Haiku 64K cap — the level
        differentiates from HIGH only on OpenRouter, where the ratio formula applies."""
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(
            resolved=parse_model_spec("anthropic:claude-haiku-4-5"), thinking_level=ThinkingLevelChoices.XHIGH
        )
        assert kw["temperature"] == 1
        assert kw["max_tokens"] == 64_000
        assert kw["thinking"]["type"] == "enabled"
        assert kw["thinking"]["budget_tokens"] == 64_000 - 16_384

    @pytest.mark.parametrize(
        ("model_name", "expected_temperature"),
        [
            ("claude-sonnet-4-6", 1),
            ("claude-opus-4-6", 1),
            ("claude-opus-4-7", None),
            ("claude-opus-4-8", None),
            ("claude-opus-5", None),
            ("claude-sonnet-5", None),
            ("claude-fable-5", None),
            ("claude-haiku-5-5", None),
        ],
    )
    def test_adaptive_thinking_models_use_effort_not_budget(self, model_name, expected_temperature):
        """4.6 deprecates ``thinking.type=enabled``/``budget_tokens`` and 4.7+ rejects it with a
        400; both drive depth through ``output_config.effort`` instead. The per-level output
        ceiling carries over and ``display`` is requested explicitly (4.7+ defaults to omitted).
        4.6 still needs temperature=1 while thinking; 4.7+ rejects sampling params outright."""
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(
            resolved=parse_model_spec(f"anthropic:{model_name}"), thinking_level=ThinkingLevelChoices.MEDIUM
        )
        assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
        assert kw["effort"] == "medium"
        assert kw["max_tokens"] == 16_384 + 25_600
        assert kw.get("temperature") == expected_temperature

    def test_adaptive_thinking_model_dated_variant(self):
        """Model names are matched as prefixes, so dated snapshots take the adaptive path."""
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(
            resolved=parse_model_spec("anthropic:claude-opus-5-20260101"), thinking_level=ThinkingLevelChoices.HIGH
        )
        assert kw["thinking"]["type"] == "adaptive"
        assert kw["effort"] == "high"

    @pytest.mark.parametrize(
        ("thinking_level", "expected_effort"),
        [
            # Anthropic's effort scale has no ``minimal`` — sending it verbatim would 400.
            (ThinkingLevelChoices.MINIMAL, "low"),
            (ThinkingLevelChoices.LOW, "low"),
            (ThinkingLevelChoices.MEDIUM, "medium"),
            (ThinkingLevelChoices.HIGH, "high"),
            (ThinkingLevelChoices.XHIGH, "xhigh"),
        ],
    )
    def test_adaptive_thinking_effort_mapping(self, thinking_level, expected_effort):
        """Pins every row of the ThinkingLevel -> ``output_config.effort`` mapping."""
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(resolved=parse_model_spec("anthropic:claude-opus-4-8"), thinking_level=thinking_level)
        assert kw["effort"] == expected_effort

    @pytest.mark.parametrize("model_name", ["claude-opus-4-8", "claude-fable-5", "claude-haiku-5-5"])
    def test_adaptive_thinking_model_without_thinking_level_drops_temperature(self, model_name):
        """No thinking level means no ``thinking`` field at all (valid on these models),
        but the seeded ``temperature=0`` would still 400 — it has to go."""
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(resolved=parse_model_spec(f"anthropic:{model_name}"))
        assert "thinking" not in kw
        assert "effort" not in kw
        assert "temperature" not in kw
        assert kw["max_tokens"] == 16_384

    @pytest.mark.parametrize("model_name", ["claude-sonnet-4-6", "claude-opus-4-6"])
    def test_claude_4_6_sends_xhigh_as_max(self, model_name):
        """4.6 has no ``xhigh`` effort (it would 400); ``max`` is its only level above ``high``."""
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(
            resolved=parse_model_spec(f"anthropic:{model_name}"), thinking_level=ThinkingLevelChoices.XHIGH
        )
        assert kw["effort"] == "max"

    @pytest.mark.parametrize("model_name", ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"])
    def test_pre_adaptive_models_keep_budget_tokens(self, model_name):
        """Models older than 4.6 reject adaptive thinking, so they keep the manual
        extended-thinking budget (paired with temperature=1) and no ``output_config.effort``."""
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(
            resolved=parse_model_spec(f"anthropic:{model_name}"), thinking_level=ThinkingLevelChoices.MEDIUM
        )
        assert kw["thinking"] == {"type": "enabled", "budget_tokens": 25_600}
        assert kw["temperature"] == 1
        assert "effort" not in kw

    @pytest.mark.parametrize(
        "model_name",
        [
            "anthropic/claude-opus-4.7",
            "anthropic/claude-opus-5",
            "anthropic/claude-sonnet-5",
            "anthropic/claude-haiku-5.5",
        ],
    )
    def test_adaptive_generation_openrouter_thinking_without_temperature(self, model_name):
        """The OpenRouter Anthropic path also seeds temperature=1 for Claude thinking
        models; the adaptive generation must still end up with it stripped."""
        self._enable_seed("openrouter", "sk-or")
        kw = get_model_kwargs(
            resolved=parse_model_spec(f"openrouter:{model_name}"), thinking_level=ThinkingLevelChoices.MEDIUM
        )
        assert kw["extra_body"]["reasoning"]["enabled"] is True
        assert kw["extra_body"]["reasoning"]["effort"] == ThinkingLevelChoices.MEDIUM
        assert "temperature" not in kw

    @pytest.mark.parametrize(
        ("slug", "model_spec", "expects_sdk_timeout"),
        [
            ("openai", "openai:gpt-5.4", True),
            ("openrouter", "openrouter:z-ai/glm-5", True),
            ("anthropic", "anthropic:claude-sonnet-4-6", False),
            ("google_genai", "google_genai:gemini-2.5-pro", False),
        ],
    )
    def test_timeout_and_retries_applied_per_provider(self, slug, model_spec, expects_sdk_timeout):
        """Every provider gets the bounded default, in the timeout shape its langchain integration accepts."""
        self._enable_seed(slug, "sk")
        kw = get_model_kwargs(resolved=parse_model_spec(model_spec))
        assert kw["max_retries"] == 2
        if expects_sdk_timeout:
            assert isinstance(kw["timeout"], openai.Timeout)
            assert (kw["timeout"].read, kw["timeout"].connect) == (120.0, 10.0)
        else:
            assert kw["timeout"] == 120.0

    def test_caller_timeout_and_retries_take_precedence(self):
        self._enable_seed("openai", "sk-o")
        kw = get_model_kwargs(resolved=parse_model_spec("openai:gpt-5.4"), timeout=45, max_retries=5)
        assert (kw["timeout"].read, kw["timeout"].connect) == (45.0, 10.0)
        assert kw["max_retries"] == 5

    def test_short_timeout_caps_connect_fuse(self):
        self._enable_seed("openai", "sk-o")
        kw = get_model_kwargs(resolved=parse_model_spec("openai:gpt-5.4"), timeout=5)
        assert (kw["timeout"].read, kw["timeout"].connect) == (5.0, 5.0)

    def test_explicit_none_override_does_not_unbind(self):
        self._enable_seed("anthropic", "sk-a")
        kw = get_model_kwargs(resolved=parse_model_spec("anthropic:claude-sonnet-4-6"), timeout=None, max_retries=None)
        assert kw["timeout"] == 120.0
        assert kw["max_retries"] == 2

    @pytest.mark.parametrize("model_spec", ["openai:gpt-5.4", "openrouter:z-ai/glm-5"])
    def test_openai_family_models_do_not_share_an_async_client(self, model_spec):
        """A process-wide client reuses connections opened in a previous task's event loop, which
        fails with ``Event loop is closed`` once the worker runs the next task in a fresh loop."""
        self._enable_seed(model_spec.split(":")[0], "sk")
        first, second = get_model(model=model_spec), get_model(model=model_spec)
        assert first.root_async_client._client is not second.root_async_client._client


@pytest.mark.parametrize("model_spec", [name for name in ModelName if "/claude-" in name])
def test_every_listed_claude_model_gets_a_thinking_config(model_spec: str):
    assert model_spec.split(":", 1)[1].startswith(CLAUDE_THINKING_MODELS)


@pytest.mark.parametrize("model_spec", [name for name in ModelName if "/gpt-" in name])
def test_every_listed_gpt_model_gets_a_thinking_config(model_spec: str):
    assert model_spec.split(":", 1)[1].startswith(OPENAI_THINKING_MODELS)
