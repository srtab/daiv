import logging
import os

from django.core.management.base import BaseCommand, CommandError

from langsmith import Client

logger = logging.getLogger("daiv.langsmith")

DASHBOARD_TITLE = "DAIV Monitoring"
DASHBOARD_DESCRIPTION = "Monitoring dashboard for DAIV agent traces, performance, cost, and tool usage"

# Charts are organized by logical group. Each group prefix is prepended to chart titles
# so they sort together visually in the single dashboard.
CHART_GROUPS = [
    {
        "prefix": "Overview",
        "charts": [
            {
                "title": "Trace Volume by Scope",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Traces",
                        "metric": "run_count",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "scope", "max_groups": 5},
                    }
                ],
            },
            {
                "title": "Error Rate by Scope",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Error Rate",
                        "metric": "error_rate",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "scope", "max_groups": 5},
                    }
                ],
            },
            {
                "title": "Trigger Breakdown",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "Traces",
                        "metric": "run_count",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "trigger", "max_groups": 5},
                    }
                ],
            },
        ],
    },
    {
        "prefix": "Latency",
        "charts": [
            {
                "title": "Median Latency by Scope",
                "chart_type": "line",
                "series": [
                    {
                        "name": "P50 Latency",
                        "metric": "latency_p50",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "scope", "max_groups": 5},
                    }
                ],
            },
            {
                "title": "P99 Latency by Scope",
                "chart_type": "line",
                "series": [
                    {
                        "name": "P99 Latency",
                        "metric": "latency_p99",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "scope", "max_groups": 5},
                    }
                ],
            },
            {
                "title": "Latency by Repository",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "P50 Latency",
                        "metric": "latency_p50",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "repository", "max_groups": 10},
                    }
                ],
            },
        ],
    },
    {
        "prefix": "Cost",
        "charts": [
            {
                "title": "Total Cost by Scope",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Total Cost",
                        "metric": "total_cost",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "scope", "max_groups": 5},
                    }
                ],
            },
            {
                "title": "Prompt vs Completion Cost",
                "chart_type": "line",
                "series": [
                    {"name": "Prompt Cost", "metric": "prompt_cost", "filters": {"filter": "eq(is_root, true)"}},
                    {
                        "name": "Completion Cost",
                        "metric": "completion_cost",
                        "filters": {"filter": "eq(is_root, true)"},
                    },
                ],
            },
            {
                "title": "Token Usage by Repository",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "Total Tokens",
                        "metric": "total_tokens",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "repository", "max_groups": 10},
                    }
                ],
            },
            {
                "title": "P99 Cost per Run by Trigger",
                "chart_type": "line",
                "series": [
                    {
                        "name": "P99 Cost",
                        "metric": "cost_p99",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "trigger", "max_groups": 5},
                    }
                ],
            },
        ],
    },
    {
        "prefix": "Platform",
        "charts": [
            {
                "title": "Volume by Git Platform",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "Traces",
                        "metric": "run_count",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "git_platform", "max_groups": 5},
                    }
                ],
            },
            {
                "title": "Error Rate by Platform",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Error Rate",
                        "metric": "error_rate",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "git_platform", "max_groups": 5},
                    }
                ],
            },
            {
                "title": "Top Repositories by Volume",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "Traces",
                        "metric": "run_count",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "repository", "max_groups": 10},
                    }
                ],
            },
        ],
    },
    {
        "prefix": "Tools",
        "charts": [
            {
                "title": "Subagent Usage",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "Runs",
                        "metric": "run_count",
                        "filters": {"filter": 'eq(run_type, "chain")'},
                        "group_by": {"attribute": "name", "max_groups": 10},
                    }
                ],
            },
            {
                "title": "Tool Call Volume",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "Calls",
                        "metric": "run_count",
                        "filters": {"filter": 'eq(run_type, "tool")'},
                        "group_by": {"attribute": "name", "max_groups": 10},
                    }
                ],
            },
            {
                "title": "Tool Error Rate",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Error Rate",
                        "metric": "error_rate",
                        "filters": {"filter": 'eq(run_type, "tool")'},
                        "group_by": {"attribute": "name", "max_groups": 10},
                    }
                ],
            },
            {
                "title": "MCP Server Tool Usage",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "Calls",
                        "metric": "run_count",
                        "filters": {"filter": 'and(eq(run_type, "tool"), has(tags, "mcp_server"))'},
                        "group_by": {"attribute": "name", "max_groups": 10},
                    }
                ],
            },
        ],
    },
    {
        "prefix": "LLM",
        "charts": [
            {
                "title": "Call Count",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Calls",
                        "metric": "run_count",
                        "filters": {"filter": 'eq(run_type, "llm")'},
                        "group_by": {"attribute": "name", "max_groups": 5},
                    }
                ],
            },
            {
                "title": "Latency",
                "chart_type": "line",
                "series": [
                    {"name": "P50 Latency", "metric": "latency_p50", "filters": {"filter": 'eq(run_type, "llm")'}},
                    {"name": "P99 Latency", "metric": "latency_p99", "filters": {"filter": 'eq(run_type, "llm")'}},
                ],
            },
            {
                "title": "Prompt vs Completion Tokens",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Prompt Tokens P50",
                        "metric": "prompt_tokens_p50",
                        "filters": {"filter": 'eq(run_type, "llm")'},
                    },
                    {
                        "name": "Completion Tokens P50",
                        "metric": "completion_tokens_p50",
                        "filters": {"filter": 'eq(run_type, "llm")'},
                    },
                ],
            },
        ],
    },
    {
        "prefix": "Model",
        "charts": [
            {
                "title": "Trace Volume by Model",
                "chart_type": "bar",
                "series": [
                    {
                        "name": "Traces",
                        "metric": "run_count",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "model", "max_groups": 10},
                    }
                ],
            },
            {
                "title": "Latency by Model",
                "chart_type": "line",
                "series": [
                    {
                        "name": "P50 Latency",
                        "metric": "latency_p50",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "model", "max_groups": 10},
                    }
                ],
            },
            {
                "title": "Cost by Model",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Total Cost",
                        "metric": "total_cost",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "model", "max_groups": 10},
                    }
                ],
            },
            {
                "title": "Error Rate by Model",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Error Rate",
                        "metric": "error_rate",
                        "filters": {"filter": "eq(is_root, true)"},
                        "group_by": {"attribute": "metadata", "path": "model", "max_groups": 10},
                    }
                ],
            },
        ],
    },
    {
        "prefix": "DiffToMetadata",
        "charts": [
            {
                "title": "Volume",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Runs",
                        "metric": "run_count",
                        "filters": {"filter": 'and(eq(is_root, true), has(tags, "DiffToMetadata"))'},
                    }
                ],
            },
            {
                "title": "Latency",
                "chart_type": "line",
                "series": [
                    {
                        "name": "P50 Latency",
                        "metric": "latency_p50",
                        "filters": {"filter": 'and(eq(is_root, true), has(tags, "DiffToMetadata"))'},
                    }
                ],
            },
            {
                "title": "Error Rate",
                "chart_type": "line",
                "series": [
                    {
                        "name": "Error Rate",
                        "metric": "error_rate",
                        "filters": {"filter": 'and(eq(is_root, true), has(tags, "DiffToMetadata"))'},
                    }
                ],
            },
        ],
    },
]


class Command(BaseCommand):
    help = "Create LangSmith custom dashboard with monitoring charts for DAIV"

    def add_arguments(self, parser):
        parser.add_argument(
            "--project",
            default=os.environ.get("LANGCHAIN_PROJECT", os.environ.get("LANGSMITH_PROJECT", "default")),
            help="LangSmith project name (default: from LANGCHAIN_PROJECT or LANGSMITH_PROJECT env var)",
        )
        parser.add_argument(
            "--recreate", action="store_true", help="Delete existing dashboard and recreate from scratch"
        )

    def handle(self, *args, **options):
        project_name = options["project"]
        recreate = options["recreate"]

        client = Client()

        # Resolve project session ID
        try:
            project = client.read_project(project_name=project_name)
        except Exception as exc:
            raise CommandError(f"Could not find LangSmith project '{project_name}': {exc}") from exc

        session_id = str(project.id)
        self.stdout.write(f"Using project '{project_name}' (session_id={session_id})")

        # Check for existing dashboard
        existing_sections = self._list_sections(client)
        existing = [s for s in existing_sections if s["title"] == DASHBOARD_TITLE]

        if existing and not recreate:
            raise CommandError(
                f"Dashboard '{DASHBOARD_TITLE}' already exists. Use --recreate to delete and recreate it."
            )

        if existing and recreate:
            for section in existing:
                self._delete_section(client, section["id"])
                self.stdout.write(f"Deleted existing dashboard: {section['id']}")

        # Create a single dashboard (section)
        dashboard = self._create_section(client, DASHBOARD_TITLE, DASHBOARD_DESCRIPTION)
        section_id = dashboard["id"]
        self.stdout.write(f"Created dashboard: {DASHBOARD_TITLE}")

        # Create all charts in the single dashboard, using group prefix in titles
        chart_index = 0
        for group in CHART_GROUPS:
            prefix = group["prefix"]
            for chart_def in group["charts"]:
                title = f"{prefix}: {chart_def['title']}"
                series = _inject_session_id(chart_def["series"], session_id)
                self._create_chart(
                    client,
                    title=title,
                    chart_type=chart_def["chart_type"],
                    section_id=section_id,
                    session_id=session_id,
                    series=series,
                    index=chart_index,
                )
                self.stdout.write(f"  Created chart: {title}")
                chart_index += 1

        self.stdout.write(self.style.SUCCESS(f"Dashboard '{DASHBOARD_TITLE}' created with {chart_index} charts"))

    def _list_sections(self, client: Client) -> list[dict]:
        resp = client.request_with_retries("GET", "/api/v1/charts/section", params={"limit": 100})
        resp.raise_for_status()
        return resp.json()

    def _delete_section(self, client: Client, section_id: str):
        resp = client.request_with_retries("DELETE", f"/api/v1/charts/section/{section_id}")
        resp.raise_for_status()

    def _create_section(self, client: Client, title: str, description: str | None) -> dict:
        resp = client.request_with_retries(
            "POST",
            "/api/v1/charts/section",
            request_kwargs={"json": {"title": title, "description": description, "index": 0}},
        )
        resp.raise_for_status()
        return resp.json()

    def _create_chart(
        self,
        client: Client,
        *,
        title: str,
        chart_type: str,
        section_id: str,
        session_id: str,
        series: list[dict],
        index: int,
    ) -> dict:
        resp = client.request_with_retries(
            "POST",
            "/api/v1/charts/create",
            request_kwargs={
                "json": {
                    "title": title,
                    "chart_type": chart_type,
                    "section_id": section_id,
                    "series": series,
                    "index": index,
                    "common_filters": {"session": [session_id]},
                }
            },
        )
        resp.raise_for_status()
        return resp.json()


def _inject_session_id(series: list[dict], session_id: str) -> list[dict]:
    """Add session ID to each series' filters so charts are scoped to the project."""
    result = []
    for s in series:
        s = {**s}
        s["filters"] = {**s.get("filters", {}), "session": [session_id]}
        result.append(s)
    return result
