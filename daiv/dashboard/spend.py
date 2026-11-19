"""The numbers the Insights Spend page shows, read from the ``SpendRecord`` rows the viewer may see.

Every figure is a sum over the same records, so each breakdown adds up to the total. A record counts in the
period of its ``occurred_at``: its run's start for run spend, the call for background spend. The report makes
eleven queries whatever the data: two totals, the chart and its hover cards, five breakdowns, the costliest
sessions and the coverage note.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db.models import Case, CharField, Count, Exists, F, Max, OuterRef, Q, StringAgg, Sum, Value, When, Window
from django.utils.translation import gettext_lazy as _

from sessions.filters import SCOPE_MINE
from sessions.managers import RUN_SPEND
from sessions.models import Run, RunStatus, SessionOrigin, SpendRecord, SpendTask
from sessions.spend_ledger import DAIV_PERSON, SpendLedger
from sessions.templatetags.session_tags import format_cost, format_tokens

from dashboard.insights import _rate_change, _relative_change, format_pct, pct, session_list_url, stacked_chart

if TYPE_CHECKING:
    from django.db.models import Aggregate

    from sessions.managers import SpendRecordQuerySet

    from accounts.models import User
    from dashboard.insights import Window as Period

CACHE_GOOD = 85
CACHE_FAIR = 70
FOLDED_ROWS = 5
SHOWN_ROWS = 100
CHART_MODELS = 5
TOP_SESSIONS = 10

ZERO = Decimal(0)
REPORTED_CACHE = Q(cache_reported=True)
RATE_SUMS: dict[str, Aggregate] = {
    "runs": Count("run", distinct=True, filter=RUN_SPEND),
    "reported_read": Sum("cache_read_tokens", filter=REPORTED_CACHE, default=0),
    "reported_input": Sum("input_tokens", filter=REPORTED_CACHE, default=0),
    "cache_records": Count("pk", filter=REPORTED_CACHE),
}

MODEL_SWATCHES = ("bg-sky-400/65", "bg-amber-500/60", "bg-emerald-500/60", "bg-fuchsia-500/50", "bg-indigo-500/70")
OTHER_SWATCH = "bg-text-muted/40"

TOKEN_KINDS = (
    ("cache_read", _("Cache read"), "bg-sky-400/35"),
    ("uncached_input", _("Uncached input"), "bg-sky-400/90"),
    ("cache_write", _("Cache write"), "bg-sky-400/60"),
    ("output", _("Output"), "bg-emerald-500/75"),
)
COST_KINDS = (
    ("estimated", _("Estimated from list prices"), "bg-sky-400/60"),
    ("reported", _("Reported by the provider"), "bg-emerald-500/75"),
    ("unknown", _("Unknown source"), "bg-text-muted/40"),
)


def usd(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return format_cost(value) if value else "$0.00"


def cache_tag(rate: int | None) -> str:
    """The tone of a prompt cache hit rate: ``good``, ``fair``, ``attention``, or ``unreported`` with no rate."""
    if rate is None:
        return "unreported"
    if rate >= CACHE_GOOD:
        return "good"
    return "fair" if rate >= CACHE_FAIR else "attention"


def hit_rate(sums: dict[str, Any]) -> int | None:
    """Cache-read tokens over input tokens, both over records that reported cache use (``RATE_SUMS``); None when
    none did."""
    return pct(sums["reported_read"], sums["reported_input"]) if sums["cache_records"] else None


def _share(cost: Decimal, total: Decimal) -> dict[str, str]:
    return {"share": f"{cost / total * 100:.1f}" if total else "0", "share_text": format_pct(pct(cost, total))}


def _cost_text(cost: Decimal, unpriced_tokens: int) -> str:
    """A cost, marked as a floor when some of its tokens have no price, and never "$0" when none of them do."""
    if unpriced_tokens and not cost:
        return str(_("no price"))
    return usd(cost) + ("+" if unpriced_tokens else "")


def _per(cost: Decimal, count: int) -> Decimal | None:
    return cost / count if count else None


def _period_params(window: Period) -> dict[str, str]:
    params = {"spend_to": window.end.isoformat()}
    if window.start is not None:
        params["spend_from"] = window.start.isoformat()
    return params


def _split(kinds, values: dict[str, Any], meanings: dict[str, Any], *, text) -> dict[str, Any]:
    total = sum(values[key] for key, _label, _css in kinds)
    parts = []
    for key, label, css in kinds:
        share = values[key] / total * 100 if total else 0
        parts.append({
            "key": key,
            "label": label,
            "css": css,
            "value": text(values[key]),
            "share": f"{round(share)}%",
            "width": f"{share:.1f}",
            "meaning": meanings[key],
        })
    return {"total": text(total), "parts": parts}


class SpendReport:
    """The Spend page for one viewer, period and scope; ``mine`` keeps the records the viewer started."""

    def __init__(self, user: User, window: Period, previous: Period | None, scope: str) -> None:
        self.user = user
        self.window = window
        self.previous = previous
        self.scope = scope
        self.records = SpendRecord.objects.for_scope(user, scope)
        self._current: dict[str, Any] | None = None
        self._models: list[dict] | None = None

    # Totals ---------------------------------------------------------------------------------------

    def totals(self) -> dict[str, Any]:
        """The six tiles, their changes against the previous period, and the two "Behind the totals" panels."""
        now = self.sums()
        before = self._aggregate(self.previous) if self.previous is not None else None
        figures = self._figures(now)
        old = self._figures(before) if before is not None else None

        def change(key, how=_relative_change, **kwargs):
            return how(figures[key], old[key], **kwargs) if old is not None else None

        return {
            **figures,
            "empty": not now["records"],
            "cost_text": usd(now["cost"]),
            "cost_change": change("cost", better="down", text=usd),
            "tokens_change": change("tokens", better="down", text=format_tokens),
            "hit_rate_change": change("hit_rate", _rate_change, better="up"),
            "runs_change": change("runs"),
            "avg_per_run_change": change("avg_per_run", better="down", text=usd),
            "avg_per_changed_run_change": change("avg_per_changed_run", better="down", text=usd),
            "cache_saving": now["cache_saving"],
            "cache_saving_floor": now["cache_unpriced"] > 0,
            "unpriced_tokens": now["unpriced"],
            "token_split": self._token_split(now),
            "cost_split": self._cost_split(now),
        }

    @staticmethod
    def _figures(sums: dict[str, Any]) -> dict[str, Any]:
        return {
            "cost": sums["cost"],
            "tokens": sums["tokens"],
            "hit_rate": hit_rate(sums),
            "runs": sums["runs"],
            "avg_per_run": _per(sums["run_cost"], sums["runs"]),
            "avg_per_changed_run": _per(sums["changed_cost"], sums["changed_runs"]),
        }

    def sums(self) -> dict[str, Any]:
        if self._current is None:
            self._current = self._aggregate(self.window)
        return self._current

    def _aggregate(self, window: Period) -> dict[str, Any]:
        changed = RUN_SPEND & Q(run__status=RunStatus.SUCCESSFUL, run__code_changes=True)
        return self.records.in_window(window).aggregate(
            records=Count("pk"),
            cost=Sum("cost_usd", default=ZERO),
            reported=Sum("reported_cost_usd", default=ZERO),
            unknown=Sum("cost_usd", filter=Q(reported_cost_usd__isnull=True), default=ZERO),
            unpriced=Sum("unpriced_tokens", default=0),
            input=Sum("input_tokens", default=0),
            cache_read=Sum("cache_read_tokens", default=0),
            cache_write=Sum("cache_write_tokens", default=0),
            output=Sum("output_tokens", default=0),
            reasoning=Sum("reasoning_tokens", default=0),
            tokens=Sum("total_tokens", default=0),
            cache_saving=Sum("cache_saving_usd", default=ZERO),
            cache_unpriced=Sum("cache_unpriced_tokens", default=0),
            **RATE_SUMS,
            run_cost=Sum("cost_usd", filter=RUN_SPEND, default=ZERO),
            changed_runs=Count("run", distinct=True, filter=changed),
            changed_cost=Sum("cost_usd", filter=changed, default=ZERO),
        )

    @staticmethod
    def _token_split(sums: dict[str, Any]) -> dict[str, Any]:
        values = {
            "cache_read": sums["cache_read"],
            "uncached_input": max(sums["input"] - sums["cache_read"] - sums["cache_write"], 0),
            "cache_write": sums["cache_write"],
            "output": sums["output"],
        }
        meanings = {
            "cache_read": _("Input served from the prompt cache, the cheapest tokens."),
            "uncached_input": _("Input the model read at the full input price."),
            "cache_write": _("Input stored in the cache for later calls; priced above plain input."),
            "output": _("What the model wrote, %(reasoning)s of it reasoning; the dearest tokens.")
            % {"reasoning": format_tokens(sums["reasoning"])},
        }
        return _split(TOKEN_KINDS, values, meanings, text=format_tokens)

    @staticmethod
    def _cost_split(sums: dict[str, Any]) -> dict[str, Any]:
        values = {
            "estimated": sums["cost"] - sums["reported"] - sums["unknown"],
            "reported": sums["reported"],
            "unknown": sums["unknown"],
        }
        meanings = {
            "estimated": _("Tokens times the model's public price; your bill can differ."),
            "reported": _("The charge the provider returned for each call."),
            "unknown": _("Spend recorded before DAIV kept the two apart."),
        }
        kinds = COST_KINDS if values["unknown"] else COST_KINDS[:2]
        return _split(kinds, values, meanings, text=usd)

    # Chart ----------------------------------------------------------------------------------------

    def chart(self) -> dict | None:
        """Spend per bucket stacked by the five costliest models and **Other**, each column with a hover card."""
        models = [row["model"] for row in self.model_rows()]
        top = models[:CHART_MODELS]
        kinds = [(f"m{i}", model, MODEL_SWATCHES[i], Q(model=model)) for i, model in enumerate(top)]
        if len(models) > CHART_MODELS:
            kinds.append(("other", str(_("Other")), OTHER_SWATCH, ~Q(model__in=top)))
        chart = stacked_chart(
            self.records,
            "occurred_at",
            self.window,
            kinds,
            lambda bucket: session_list_url(_period_params(bucket), self.scope),
            measure=lambda q: Sum("cost_usd", filter=q, default=ZERO),
            text=usd,
            extra=RATE_SUMS,
        )
        if chart is None:
            return None
        half = len(chart["columns"]) / 2
        for index, column in enumerate(chart["columns"]):
            if not column["total"]:
                continue
            column["card"] = {
                "id": f"spend-card-{index}",
                "lines": [
                    {
                        "name": segment["label"],
                        "css": segment["css"],
                        "cost": usd(segment["count"]),
                        "share": f"{pct(segment['count'], column['total'])}%",
                    }
                    for segment in reversed(column["segments"])
                ],
                "runs": column["extra"]["runs"],
                "hit_rate": hit_rate(column["extra"]),
                "side": "left" if index < half else "right",
            }
        return chart

    # Breakdowns -----------------------------------------------------------------------------------

    def breakdowns(self) -> list[dict[str, Any]]:
        """The five ranked tables: person, repository, what started the spend, model and provider."""
        swatches = {row["model"]: MODEL_SWATCHES[i] for i, row in enumerate(self.model_rows()[:CHART_MODELS])}
        return [
            self._folded("person", _("By person"), _("people"), self._person_rows()),
            self._folded("repository", _("By repository"), _("repositories"), self._repository_rows()),
            self._started_by(),
            self._folded("model", _("By model"), _("models"), self._model_table_rows(swatches)),
            self._folded("provider", _("By provider"), _("providers"), self._provider_rows()),
        ]

    def model_rows(self) -> list[dict]:
        if self._models is None:
            self._models = self._ranked(self.records, "model")
        return self._models

    def _ranked(self, records: SpendRecordQuerySet, *group_by: str, **extra: Any) -> list[dict]:
        """Up to ``SHOWN_ROWS + 1`` groups of the period's ``records``, costliest first; ``groups`` counts them all."""
        rows = (
            records
            .in_window(self.window)
            .order_by()
            .values(*group_by)
            .annotate(
                cost=Sum("cost_usd", default=ZERO),
                tokens=Sum("total_tokens", default=0),
                unpriced=Sum("unpriced_tokens", default=0),
                reported=Sum("reported_cost_usd", default=ZERO),
                **RATE_SUMS,
                groups=Window(Count(Value(1))),
                **extra,
            )
            .order_by("-cost", "-tokens", *group_by)
        )
        return list(rows[: SHOWN_ROWS + 1])

    def _row(self, values: dict, name: str, *, note: Any = "", param: tuple[str, str] | None, why: Any = "") -> dict:
        total = self.sums()["cost"]
        rate = hit_rate(values)
        cost = values["cost"]
        url = session_list_url({**_period_params(self.window), param[0]: param[1]}, self.scope) if param else ""
        return {
            "name": name,
            "note": note,
            "cost": cost,
            "cost_text": _cost_text(cost, values["unpriced"]),
            "unpriced": values["unpriced"],
            **_share(cost, total),
            "tokens": format_tokens(values["tokens"]),
            "runs": values["runs"],
            "hit_rate": rate,
            "cache": cache_tag(rate),
            "url": url,
            "why": why,
            "swatch": "",
            "subs": [],
            "groups": values.get("groups", 0),
        }

    def _folded(self, key: str, title: Any, plural: Any, rows: list[dict]) -> dict[str, Any]:
        """A table showing its top rows and a **Rest** row, with up to ``SHOWN_ROWS`` rows behind **Show all**."""
        groups = rows[0]["groups"] if rows else 0
        shown = rows[:SHOWN_ROWS]
        for index, row in enumerate(shown):
            row["hidden"] = index >= FOLDED_ROWS
        rest = None
        if groups > FOLDED_ROWS:
            sums = self.sums()
            top = shown[:FOLDED_ROWS]
            cost = sums["cost"] - sum(row["cost"] for row in top)
            unpriced = sums["unpriced"] - sum(row["unpriced"] for row in top)
            rest = {
                "name": _("Rest (%(count)s %(plural)s)") % {"count": groups - FOLDED_ROWS, "plural": plural},
                "cost_text": _cost_text(cost, unpriced),
                **_share(cost, sums["cost"]),
                "why": _("The rows past the top %(count)s are summed here") % {"count": FOLDED_ROWS},
            }
        return {
            "key": key,
            "title": title,
            "plural": plural,
            "rows": shown,
            "rest": rest,
            "groups": groups,
            "not_shown": max(groups - SHOWN_ROWS, 0),
            "foldable": groups > FOLDED_ROWS,
        }

    def _person_rows(self) -> list[dict]:
        no_user = Q(user__isnull=True)
        external = no_user & ~Q(external_provider="") & ~Q(external_uid="")
        text = CharField()
        records = self.records.annotate(
            ext_provider=Case(When(external, then=F("external_provider")), default=Value(""), output_field=text),
            ext_uid=Case(When(external, then=F("external_uid")), default=Value(""), output_field=text),
            deleted_name=Case(When(no_user & ~external, then=F("person_name")), default=Value(""), output_field=text),
        )
        rows = self._ranked(
            records,
            "user_id",
            "user__username",
            "user__name",
            "ext_provider",
            "ext_uid",
            "deleted_name",
            external_label=Max("external_username"),
        )
        out = []
        for values in rows:
            person = SpendLedger.person({
                "user_id": values["user_id"],
                "user__username": values["user__username"],
                "user__name": values["user__name"],
                "external_provider": values["ext_provider"],
                "external_uid": values["ext_uid"],
                "external_username": values["external_label"],
                "person_name": values["deleted_name"],
            })
            notes = {
                "external": _("%(platform)s, external") % {"platform": values["ext_provider"].title()},
                "deleted": _("deleted user"),
                "daiv": _("no person started it"),
            }
            daiv = person.key == DAIV_PERSON
            out.append(
                self._row(
                    values,
                    person.name,
                    note=notes.get(person.kind, ""),
                    param=None if daiv else ("spend_person", person.key),
                    why=_("Work no person started has no single list of sessions") if daiv else "",
                )
            )
        return out

    def _repository_rows(self) -> list[dict]:
        return [
            self._row(
                values,
                values["repo_id"] or str(_("No repository")),
                param=("spend_repo", values["repo_id"]) if values["repo_id"] else None,
                why="" if values["repo_id"] else _("Batch titles have no repository or session to list"),
            )
            for values in self._ranked(self.records, "repo_id")
        ]

    def _model_table_rows(self, swatches: dict[str, str]) -> list[dict]:
        out = []
        for values in self.model_rows():
            row = self._row(values, values["model"], param=("spend_model", values["model"]))
            row["swatch"] = swatches.get(values["model"], OTHER_SWATCH)
            out.append(row)
        return out

    def _provider_rows(self) -> list[dict]:
        out = []
        rows = self._ranked(
            self.records, "provider_slug", provider_label=Max("provider_name"), models=Count("model", distinct=True)
        )
        for values in rows:
            slug = values["provider_slug"]
            name = values["provider_label"] or slug or str(_("Unknown provider"))
            if values["reported"]:
                note = _("cost reported by %(provider)s") % {"provider": name}
            elif values["cost"]:
                note = _("list prices")
            else:
                note = ""
            row = self._row(
                values,
                name,
                note=note,
                param=("spend_provider", slug) if slug else None,
                why="" if slug else _("Spend recorded before DAIV kept providers has none to filter by"),
            )
            row["models"] = values["models"]
            out.append(row)
        return out

    def _started_by(self) -> dict[str, Any]:
        """Trigger types, then **Background** with a row per task; short and fixed, so never folded."""
        rows = self._ranked(self.records, "trigger_type", "task")
        triggers, tasks = [], []
        for values in rows:
            if values["task"]:
                task = SpendTask(values["task"])
                consolidation = task == SpendTask.MEMORY_CONSOLIDATION
                tasks.append(
                    self._row(
                        values,
                        str(task.label),
                        param=None if consolidation else ("spend_task", task.value),
                        why=_("Repository-wide work has no session to list") if consolidation else "",
                    )
                )
            else:
                triggers.append(
                    self._row(
                        values,
                        str(SessionOrigin(values["trigger_type"]).label),
                        param=("spend_trigger", values["trigger_type"]),
                    )
                )
        if tasks:
            background = [values for values in rows if values["task"]]
            summed = {
                key: sum(values[key] for values in background)
                for key in ("cost", "tokens", "unpriced", "reported_read", "reported_input", "cache_records")
            }
            row = self._row(
                {**summed, "runs": 0},
                str(_("Background")),
                param=None,
                why=_("DAIV work outside runs, split by task below"),
            )
            row["subs"] = tasks
            row["background"] = True
            triggers.append(row)
        return {
            "key": "started_by",
            "title": _("Started by"),
            "rows": triggers,
            "rest": None,
            "foldable": False,
            "not_shown": 0,
        }

    # Sessions and coverage ------------------------------------------------------------------------

    def top_sessions(self) -> list[dict[str, Any]]:
        """The ``TOP_SESSIONS`` sessions with the most run spend in the period, as the sessions list's cost sort ranks
        them."""
        rows = (
            self.records
            .in_window(self.window)
            .filter(RUN_SPEND, session__isnull=False)
            .order_by()
            .values(
                "session_id",
                "session__title",
                "session__origin",
                "session__repo_id",
                "session__user__username",
                "session__user__name",
                "session__external_username",
            )
            .annotate(
                cost=Sum("cost_usd", default=ZERO),
                runs=Count("run", distinct=True),
                models=StringAgg("model", delimiter=Value("\n")),
                last=Max("occurred_at"),
            )
            .order_by("-cost", "-last")[:TOP_SESSIONS]
        )
        return [
            {
                "thread_id": row["session_id"],
                "title": row["session__title"] or str(_("Untitled session")),
                "origin": SessionOrigin(row["session__origin"]).label,
                "origin_value": row["session__origin"],
                "repo_id": row["session__repo_id"],
                "owner": row["session__user__name"]
                or row["session__user__username"]
                or row["session__external_username"]
                or str(_("DAIV")),
                "models": ", ".join(sorted(set(row["models"].split("\n")))),
                "runs": row["runs"],
                "cost_text": usd(row["cost"]),
            }
            for row in rows
        ]

    def sessions_by_cost_url(self) -> str:
        return session_list_url({**_period_params(self.window), "sort": "cost"}, self.scope)

    def coverage(self) -> dict[str, Any]:
        """What the figures leave out: unpriced models, runs that recorded nothing, and runs still running."""
        runs = Run.objects.started_by(self.user) if self.scope == SCOPE_MINE else Run.objects.visible_to(self.user)
        recorded = SpendRecord.objects.filter(run=OuterRef("pk"))
        counts = runs.filter(self.window.date_q("created_at")).aggregate(
            unrecorded=Count("pk", filter=Q(Q(status__in=RunStatus.terminal()), ~Exists(recorded))),
            running=Count("pk", filter=Q(status=RunStatus.RUNNING)),
        )
        return {
            "unpriced_models": [row["model"] for row in self.model_rows() if row["unpriced"]],
            "unrecorded_runs": counts["unrecorded"],
            "running_runs": counts["running"],
        }
