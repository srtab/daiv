"""The /compare/ pages: DAIV beside each tool the README's "How it compares" names.

Every cell about another tool cites the vendor's own public docs it was taken from.
"""

from dataclasses import dataclass, field

CHECKED = "November 2026"

ASPECTS = (
    ("hosting", "Where it runs"),
    ("platforms", "Git platforms"),
    ("trigger", "How work starts"),
    ("models", "Models"),
    ("execution", "Where the agent's commands run"),
    ("license", "License"),
    ("pricing", "Pricing"),
)


@dataclass(frozen=True)
class Cell:
    text: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class Row:
    aspect: str
    daiv: Cell
    theirs: Cell


DAIV_CELLS = {
    "hosting": Cell(
        "On your servers, with Docker Compose or Docker Swarm.",
        ("https://srtab.github.io/daiv/dev/getting-started/deployment/",),
    ),
    "platforms": Cell(
        "GitLab.com, self-managed GitLab, GitHub.com and GitHub Enterprise Server.",
        ("https://srtab.github.io/daiv/dev/getting-started/platform-setup/",),
    ),
    "trigger": Cell(
        "Label an issue daiv or mention @daiv on an issue or merge request; or start a run from the dashboard, "
        "the Jobs API, the MCP endpoint or a schedule.",
        ("https://srtab.github.io/daiv/dev/features/issue-addressing/",),
    ),
    "models": Cell(
        "OpenRouter, Anthropic, OpenAI or Google Gemini with your own keys, or your own OpenAI-compatible "
        "server; chosen per repository and per run.",
        ("https://srtab.github.io/daiv/dev/getting-started/llm-providers/",),
    ),
    "execution": Cell(
        "A Docker container per session, managed by daiv-sandbox, with no network unless an egress allow-list "
        "opens it.",
        ("https://srtab.github.io/daiv/dev/features/sandbox/",),
    ),
    "license": Cell("Apache-2.0.", ("https://github.com/srtab/daiv/blob/main/LICENSE",)),
    "pricing": Cell(
        "No license fee and no per-seat pricing. You pay for your servers and your LLM provider's usage.",
        ("https://github.com/srtab/daiv#license",),
    ),
}


@dataclass(frozen=True)
class Comparison:
    slug: str
    name: str
    short_name: str
    docs_url: str
    summary: str
    choose_them: str
    choose_daiv: str
    cells: dict[str, Cell] = field(hash=False)

    @property
    def rows(self) -> list[Row]:
        return [Row(label, DAIV_CELLS[key], self.cells[key]) for key, label in ASPECTS]


COMPARISONS = (
    Comparison(
        slug="github-copilot",
        name="GitHub Copilot cloud agent",
        short_name="Copilot",
        docs_url="https://docs.github.com/en/copilot/concepts/copilot-surfaces/copilot-on-github",
        summary=(
            "GitHub's agent researches a repository, plans and makes changes in a cloud environment, and opens a "
            "pull request for you to review. GitHub's docs used to call it Copilot coding agent."
        ),
        choose_them=(
            "Copilot's cloud agent is built into GitHub and needs no infrastructure of your own. If your code is on "
            "GitHub and you already pay for Copilot, start there."
        ),
        choose_daiv=(
            "Your code is on GitLab or GitHub Enterprise Server, or you want the agent, its sandbox and the choice "
            "of model provider on your own servers."
        ),
        cells={
            "hosting": Cell(
                "Hosted by GitHub. Sessions run in a cloud sandbox GitHub manages, or in a GitHub Actions "
                "environment, which can use your self-hosted runners.",
                (
                    "https://docs.github.com/en/copilot/concepts/copilot-surfaces/copilot-on-github",
                    "https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/customize-the-agent-environment",
                ),
            ),
            "platforms": Cell(
                "Repositories on GitHub. Copilot is not available for GitHub Enterprise Server.",
                ("https://docs.github.com/en/copilot/get-started/plans",),
            ),
            "trigger": Cell(
                "Assign an issue to Copilot, mention @copilot on a pull request, or start a session from the "
                "Agents tab, an IDE, the GitHub CLI, the API, Slack, Jira and other integrations.",
                ("https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/start-copilot-sessions",),
            ),
            "models": Cell(
                "A list of Anthropic, Google, OpenAI, xAI and Microsoft models that GitHub provides, or Auto; "
                "which ones you get depends on your plan and policies.",
                ("https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/changing-the-ai-model",),
            ),
            "execution": Cell(
                "An ephemeral Linux or Windows environment. A firewall limits internet access to GitHub and a "
                "recommended allowlist, which organization and repository owners can customize.",
                (
                    "https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-the-firewall",
                ),
            ),
            "license": Cell(
                "A commercial GitHub service, sold through Copilot plans.",
                ("https://docs.github.com/en/copilot/get-started/plans",),
            ),
            "pricing": Cell(
                "A Copilot plan (per user for Pro, Pro+ and Max; per seat for Business and Enterprise), with AI "
                "credits for model usage and compute billed separately.",
                (
                    "https://docs.github.com/en/copilot/get-started/plans",
                    "https://docs.github.com/en/billing/concepts/product-billing/cloud-and-local-sandboxes",
                ),
            ),
        },
    ),
    Comparison(
        slug="gitlab-duo",
        name="GitLab Duo Agent Platform",
        short_name="GitLab Duo",
        docs_url="https://docs.gitlab.com/user/duo_agent_platform/",
        summary=(
            "GitLab's agents work across the software development lifecycle in GitLab. Its Developer Flow creates a "
            "draft merge request from an issue and iterates on it from review feedback."
        ),
        choose_them=(
            "GitLab Duo builds agents into GitLab itself. If your GitLab subscription already includes it, try it "
            "first."
        ),
        choose_daiv=(
            "Your team also works on GitHub, or you want an Apache-2.0 agent you run yourself, with your own model "
            "keys, on any GitLab tier."
        ),
        cells={
            "hosting": Cell(
                "Part of GitLab.com, GitLab Self-Managed and GitLab Dedicated. Flows started in GitLab run as CI/CD "
                "jobs on GitLab-hosted runners or your own.",
                (
                    "https://docs.gitlab.com/user/duo_agent_platform/",
                    "https://docs.gitlab.com/user/duo_agent_platform/flows/execution/",
                ),
            ),
            "platforms": Cell(
                "GitLab: GitLab.com, Self-Managed and Dedicated.", ("https://docs.gitlab.com/user/duo_agent_platform/",)
            ),
            "trigger": Cell(
                "Assign the Developer Flow to an issue, mention it, select Implement or change a work item's "
                "status; or start it from Agentic Chat or the GitLab Duo CLI.",
                ("https://docs.gitlab.com/user/duo_agent_platform/flows/foundational_flows/",),
            ),
            "models": Cell(
                "GitLab-managed models from Anthropic, Google, OpenAI and others, set per feature; or self-hosted "
                "models behind a self-hosted AI Gateway.",
                (
                    "https://docs.gitlab.com/user/duo_agent_platform/model_selection/",
                    "https://docs.gitlab.com/administration/gitlab_duo_self_hosted/",
                ),
            ),
            "execution": Cell(
                "A CI/CD job on a runner, sandboxed with Anthropic's Sandbox Runtime: the network reaches the "
                "GitLab instance and the Workflow Service unless a network policy allows more.",
                ("https://docs.gitlab.com/user/duo_agent_platform/environment_sandbox/",),
            ),
            "license": Cell(
                "Open core: GitLab is MIT outside its ee/ directory, and the Agent Platform's code is under the "
                "GitLab Enterprise Edition license.",
                ("https://gitlab.com/gitlab-org/gitlab/-/blob/master/LICENSE",),
            ),
            "pricing": Cell(
                "Usage-based, in GitLab Credits, on Premium and Ultimate; some features are available on Free "
                "with credits.",
                ("https://docs.gitlab.com/subscriptions/gitlab_credits/", "https://about.gitlab.com/pricing/"),
            ),
        },
    ),
    Comparison(
        slug="claude-code",
        name="Claude Code in CI",
        short_name="Claude Code",
        docs_url="https://code.claude.com/docs/en/github-actions",
        summary=(
            "Anthropic's Claude Code runs inside your own pipelines: the claude-code-action GitHub Action, or a "
            "GitLab CI/CD job. Mention @claude, or run it on any workflow event with a prompt."
        ),
        choose_them=(
            "Claude Code in CI runs Anthropic's agent inside your own pipelines, if you want Claude in CI rather "
            "than a long-running service."
        ),
        choose_daiv=(
            "You want a long-running service with a dashboard, an MCP endpoint and scheduled jobs, or models from "
            "providers other than Anthropic."
        ),
        cells={
            "hosting": Cell(
                "No service to run: each run is a job on your GitHub Actions or GitLab runner, and model calls go "
                "to the provider you choose.",
                ("https://github.com/anthropics/claude-code-action", "https://code.claude.com/docs/en/gitlab-ci-cd"),
            ),
            "platforms": Cell(
                "GitHub.com; GitHub Enterprise Server on Team and Enterprise plans; GitLab through GitLab CI/CD, a "
                "beta that GitLab maintains.",
                (
                    "https://code.claude.com/docs/en/github-enterprise-server",
                    "https://code.claude.com/docs/en/gitlab-ci-cd",
                ),
            ),
            "trigger": Cell(
                "On GitHub, @claude in issue and pull request comments, or any workflow event with a prompt. On "
                "GitLab, the pipeline rules you define.",
                ("https://code.claude.com/docs/en/github-actions", "https://code.claude.com/docs/en/gitlab-ci-cd"),
            ),
            "models": Cell(
                "Claude models, through the Claude API, Amazon Bedrock, Google Cloud, or Microsoft Foundry on "
                "GitHub Actions.",
                ("https://code.claude.com/docs/en/github-actions",),
            ),
            "execution": Cell(
                "The CI runner that runs the job, with tools granted through allowedTools. On GitHub, the token is "
                "short-lived and scoped to the repository.",
                ("https://github.com/anthropics/claude-code-action/blob/main/docs/security.md",),
            ),
            "license": Cell(
                "The GitHub Action is MIT. Claude Code itself is not open source: Anthropic reserves all rights.",
                (
                    "https://github.com/anthropics/claude-code-action/blob/main/LICENSE",
                    "https://github.com/anthropics/claude-code/blob/main/LICENSE.md",
                ),
            ),
            "pricing": Cell(
                "Runner minutes plus Claude usage, billed as API tokens or against a Claude subscription.",
                ("https://code.claude.com/docs/en/github-actions",),
            ),
        },
    ),
    Comparison(
        slug="openhands",
        name="OpenHands",
        short_name="OpenHands",
        docs_url="https://docs.openhands.dev/overview/introduction",
        summary=(
            "OpenHands is an open-source community and platform for AI-driven development: the Agent Canvas "
            "client, a Software Agent SDK, the hosted OpenHands Cloud, and OpenHands Enterprise for self-hosting."
        ),
        choose_them=(
            "OpenHands is also open source and self-hostable, with a larger community and a general-purpose agent "
            "that goes beyond Git workflows."
        ),
        choose_daiv=(
            "You want one deployment on your own servers, for a team, under an open-source license, that works "
            "inside GitLab and GitHub threads."
        ),
        cells={
            "hosting": Cell(
                "Agent Canvas runs on your machine with npm or Docker, or on a backend you choose. OpenHands Cloud "
                "is hosted; OpenHands Enterprise runs on your servers, private cloud or VPC.",
                (
                    "https://docs.openhands.dev/openhands/usage/agent-canvas/overview",
                    "https://docs.openhands.dev/enterprise",
                ),
            ),
            "platforms": Cell(
                "GitHub, GitLab and Bitbucket on Cloud. Enterprise adds GitLab Self-Hosted, Bitbucket Data Center "
                "and Azure Repos.",
                ("https://docs.openhands.dev/enterprise",),
            ),
            "trigger": Cell(
                "On Cloud, label an issue openhands or comment @openhands. Agent Canvas adds a chat UI and "
                "issue-to-pull-request automations.",
                (
                    "https://docs.openhands.dev/openhands/usage/cloud/github-installation",
                    "https://docs.openhands.dev/openhands/usage/agent-canvas/prebuilt/issue-to-pr",
                ),
            ),
            "models": Cell(
                "Any LLM that LiteLLM supports, including local servers such as Ollama or vLLM, with your own keys. "
                "Cloud also offers its own LLM provider.",
                ("https://docs.openhands.dev/openhands/usage/llms/llms",),
            ),
            "execution": Cell(
                "Depends on the edition: your machine or a Docker container for Agent Canvas, managed sandboxes on "
                "Cloud, and one sandbox per conversation on Enterprise.",
                (
                    "https://docs.openhands.dev/openhands/usage/agent-canvas/overview",
                    "https://docs.openhands.dev/enterprise/architecture",
                ),
            ),
            "license": Cell(
                "Agent Canvas and the Software Agent SDK are MIT; Cloud and Enterprise are commercial.",
                ("https://docs.openhands.dev/enterprise/enterprise-vs-oss",),
            ),
            "pricing": Cell(
                "Agent Canvas is free to self-host. Teams is a flat monthly fee with per-seat add-ons, and "
                "Enterprise is priced on request.",
                ("https://openhands.dev/pricing",),
            ),
        },
    ),
)

COMPARISONS_BY_SLUG = {comparison.slug: comparison for comparison in COMPARISONS}
