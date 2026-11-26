"""Content of the public homepage that the template shows and its JSON-LD repeats: the FAQ and the social accounts."""

import re
from dataclasses import dataclass

from daiv import DOCS_URL

DOCS_ROOT = DOCS_URL.rstrip("/")
QUICKSTART_URL = f"{DOCS_ROOT}/getting-started/quickstart/"
DEPLOYMENT_URL = f"{DOCS_ROOT}/getting-started/deployment/"

DEFINITION = (
    "DAIV is an open-source, self-hosted AI coding agent for GitLab and GitHub that turns issues into merge "
    "requests, answers review comments and fixes failing CI."
)

_LINK = re.compile(r'<a href="([^"]+)">(.*?)</a>')


@dataclass(frozen=True)
class SocialLink:
    name: str
    url: str


SOCIAL_LINKS = (
    SocialLink("X", "https://x.com/daivagent"),
    SocialLink("Bluesky", "https://bsky.app/profile/daivagent.com"),
    SocialLink("LinkedIn", "https://www.linkedin.com/company/daivagent"),
    SocialLink("Mastodon", "https://mastodon.social/@daivagent"),
    SocialLink("YouTube", "https://www.youtube.com/@daivagent"),
)


@dataclass(frozen=True)
class Faq:
    question: str
    answer: str

    @property
    def markdown_answer(self) -> str:
        return _LINK.sub(r"[\2](\1)", self.answer)


FAQS = (
    Faq(
        question="What do I need to self-host DAIV?",
        answer=(
            "A Linux host with Docker. DAIV runs with Docker Compose or Docker Swarm as a few containers: "
            "the app, background workers, a scheduler, PostgreSQL, Redis 8 or later, and the "
            '<a href="https://github.com/srtab/daiv-sandbox">daiv-sandbox</a> service that runs the agent\'s '
            f'commands. The <a href="{QUICKSTART_URL}">Quickstart</a> '
            "runs the smallest working setup on one machine and turns a GitLab issue into a merge request in "
            f'about 15 minutes. <a href="{DEPLOYMENT_URL}">Deployment</a> '
            "covers a production install."
        ),
    ),
    Faq(
        question="Which LLM providers and models can I use? Can I run local models?",
        answer=(
            "OpenRouter (the default), Anthropic, OpenAI and Google Gemini, with your own API keys. For local "
            "models, an admin can register any OpenAI-compatible server, such as vLLM, llama.cpp, Ollama or "
            f'LiteLLM, as a <a href="{DOCS_ROOT}/getting-started/llm-providers/#custom-providers">custom '
            "provider</a>. The agent runs on Claude Sonnet 5.5 by default; you can change the model per "
            f'<a href="{DOCS_ROOT}/customization/repository-config/#model-overrides">repository'
            "</a> or per run. DAIV is tested with Claude Sonnet 5.5, Opus 5.5 and Haiku 5.5, GPT-6.1 Sol and "
            "GPT-6 Luna, GLM-5.2 through OpenRouter, and Qwen3.8 on a self-hosted vLLM server."
        ),
    ),
    Faq(
        question="Which Git platforms does DAIV support?",
        answer=(
            "GitLab and GitHub. On GitLab it works with GitLab.com and self-managed instances, as a GitLab user of "
            "its own. On GitHub it works with GitHub.com and GitHub Enterprise Server, as a GitHub App you create "
            "and install. Issue addressing, answers to review comments, slash commands and CI fixes work on both. "
            "The Quickstart uses GitLab; "
            f'<a href="{DOCS_ROOT}/getting-started/platform-setup/">Platform setup</a> '
            "covers both. One DAIV install connects to one platform. DAIV does not support Bitbucket, Azure "
            "DevOps, Gitea or Forgejo."
        ),
    ),
    Faq(
        question="What does DAIV cost?",
        answer=(
            "DAIV itself costs nothing: there is no per-seat pricing and no paid tier today. You pay for the "
            "servers it runs on and for your LLM provider's usage, billed to your own API keys, or nothing for "
            "the model if you run your own. DAIV records the token usage and "
            f'<a href="{DOCS_ROOT}/reference/monitoring/#usage-and-cost">'
            "estimated cost</a> of every run."
        ),
    ),
    Faq(
        question="Is there a hosted version of DAIV?",
        answer=(
            "No. DAIV runs only on servers you operate, with your own Git platform and LLM provider accounts. "
            f'The <a href="{QUICKSTART_URL}">Quickstart</a> sets up a single machine in about 15 minutes.'
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
            f'<a href="{DOCS_ROOT}/reference/site-configuration/#set-up-searxng">SearXNG</a>. The '
            "sandbox has no network unless a "
            f'<a href="{DOCS_ROOT}/features/sandbox-environments/">sandbox environment</a> '
            "allows specific hosts. LangSmith tracing and Sentry error reporting are off unless you configure them."
        ),
    ),
)
