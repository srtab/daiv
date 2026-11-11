"""Content of the public homepage that the template shows and its FAQPage JSON-LD repeats."""

from dataclasses import dataclass

from django.utils.safestring import SafeString, mark_safe

QUICKSTART_URL = "https://srtab.github.io/daiv/dev/getting-started/quickstart/"


@dataclass(frozen=True)
class Faq:
    question: str
    answer: str

    @property
    def answer_html(self) -> SafeString:
        return mark_safe(self.answer)  # noqa: S308 — authored in FAQS below, never user input.


FAQS = (
    Faq(
        question="What do I need to self-host DAIV?",
        answer=(
            "A Linux host with Docker. DAIV runs with Docker Compose or Docker Swarm as a few containers: "
            "the app, background workers, a scheduler, PostgreSQL, Redis 8 or later, and the "
            '<a href="https://github.com/srtab/daiv-sandbox">daiv-sandbox</a> service that runs the agent\'s '
            'commands. The <a href="https://srtab.github.io/daiv/dev/getting-started/quickstart/">Quickstart</a> '
            "runs the smallest working setup on one machine and turns a GitLab issue into a merge request in "
            'about 15 minutes. <a href="https://srtab.github.io/daiv/dev/getting-started/deployment/">Deployment</a> '
            "covers a production install."
        ),
    ),
    Faq(
        question="Which LLM providers and models can I use?",
        answer=(
            "OpenRouter (the default), Anthropic, OpenAI and Google Gemini, with your own API keys. An admin can "
            "also register any OpenAI-compatible server, such as vLLM, llama.cpp or LiteLLM, as a "
            '<a href="https://srtab.github.io/daiv/dev/getting-started/llm-providers/#custom-providers">custom '
            "provider</a>. The agent runs on Claude Sonnet 5.5 by default; you can change the model per "
            '<a href="https://srtab.github.io/daiv/dev/customization/repository-config/#model-overrides">repository'
            "</a> or per run. DAIV is tested with Claude Sonnet 5.5, Opus 5.5 and Haiku 5.5, GPT-6.1 Sol and "
            "GPT-6 Luna, GLM-5.2 through OpenRouter, and Qwen3.8 on a self-hosted vLLM server."
        ),
    ),
    Faq(
        question="Does DAIV work with both GitLab and GitHub?",
        answer=(
            "Yes. On GitLab it works with GitLab.com and self-managed instances, as a GitLab user of its own. On "
            "GitHub it works with GitHub.com and GitHub Enterprise Server, as a GitHub App you create and install. "
            "Issue addressing, answers to review comments, slash commands and CI fixes work on both. The "
            "Quickstart uses GitLab; "
            '<a href="https://srtab.github.io/daiv/dev/getting-started/platform-setup/">Platform Setup</a> '
            "covers both."
        ),
    ),
    Faq(
        question="What does DAIV cost?",
        answer=(
            "DAIV itself costs nothing: there is no per-seat pricing and no paid tier today. You pay for the "
            "servers it runs on and for your LLM provider's usage, billed to your own API keys, or nothing for "
            "the model if you run your own. DAIV records the token usage and "
            '<a href="https://srtab.github.io/daiv/dev/reference/monitoring/#internal-usage-and-cost-tracking">'
            "estimated cost</a> of every run."
        ),
    ),
    Faq(
        question="What is DAIV's license?",
        answer=(
            '<a href="https://github.com/srtab/daiv/blob/main/LICENSE">Apache-2.0</a>, and it will stay that '
            "way. If paid support or hosting is ever offered, it will run the same open-source code, not a "
            "separately licensed edition."
        ),
    ),
    Faq(
        question="What data leaves my infrastructure?",
        answer=(
            "The agent's requests to your LLM provider, which carry issue and comment text and the code it "
            "reads; with a model on your own server, none of that leaves. DAIV also calls your GitLab or GitHub. "
            "Its web search (DuckDuckGo by default) and web fetch tools, and the built-in Context7 MCP server "
            "for library documentation, reach the internet; you can turn each off, or point search at your own "
            '<a href="https://srtab.github.io/daiv/dev/reference/env-variables/#web-search">SearXNG</a>. The '
            "sandbox has no network unless a "
            '<a href="https://srtab.github.io/daiv/dev/features/sandbox-environments/">sandbox environment</a> '
            "allows specific hosts. LangSmith tracing and Sentry error reporting are off unless you configure them."
        ),
    ),
)
