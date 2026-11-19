from decimal import Decimal, InvalidOperation

from django.db import migrations, models

BATCH_SIZE = 1000
UNKNOWN_MODEL = "unknown"
_MIN_COLLAPSIBLE_UNIT = 4


def _collapse_repeated_model_name(model_name):
    period = (model_name * 2).find(model_name, 1)
    return model_name[:period] if _MIN_COLLAPSIBLE_UNIT <= period < len(model_name) else model_name


def _tokens(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _cost(value):
    if value is None:
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() and cost >= 0 else None


def _figures(usage):
    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}
    total = _tokens(usage.get("total_tokens"))
    cost = _cost(usage.get("cost_usd"))
    return {
        "input_tokens": _tokens(usage.get("input_tokens")),
        "cache_read_tokens": _tokens(input_details.get("cache_read")),
        "cache_write_tokens": sum(
            _tokens(input_details.get(key))
            for key in ("cache_creation", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        ),
        "output_tokens": _tokens(usage.get("output_tokens")),
        "reasoning_tokens": _tokens(output_details.get("reasoning")),
        "total_tokens": total,
        "cost_usd": cost or Decimal(0),
        "unpriced_tokens": total if cost is None else 0,
        "cache_reported": "cache_read" in input_details,
        "cache_unpriced_tokens": _tokens(input_details.get("cache_read")),
    }


def _merge(into, figures):
    for key, value in figures.items():
        into[key] = (into[key] or value) if isinstance(value, bool) else into[key] + value


def records_for_run(run, provider_names):
    """The run-spend rows of one ``Run``, one per repaired model name, from its stored usage."""
    spec_slug, _, spec_model = run.agent_model.partition(":") if ":" in run.agent_model else ("", "", "")
    by_model = {}
    usage_by_model = run.usage_by_model if isinstance(run.usage_by_model, dict) else {}
    for raw_name, usage in usage_by_model.items():
        if not isinstance(usage, dict):
            continue
        name = _collapse_repeated_model_name(raw_name) if raw_name else UNKNOWN_MODEL
        figures = _figures(usage)
        if name in by_model:
            _merge(by_model[name], figures)
        else:
            by_model[name] = figures
    if not by_model and run.input_tokens is not None:
        by_model[UNKNOWN_MODEL] = _figures({
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "total_tokens": run.total_tokens,
            "cost_usd": run.cost_usd,
        })

    shared = {
        "occurred_at": run.created_at,
        "run_id": run.pk,
        "session_id": run.session_id,
        "repo_id": run.repo_id,
        "trigger_type": run.trigger_type,
        "user_id": run.user_id,
        "external_provider": run.external_provider,
        "external_uid": run.external_uid,
        "external_username": run.external_username,
    }
    rows = []
    for name, figures in by_model.items():
        slug = spec_slug if spec_slug and name == spec_model else ""
        rows.append({
            **shared,
            **figures,
            "model": name[:255],
            "provider_slug": slug,
            "provider_name": provider_names.get(slug, slug),
        })
    return rows


def backfill(apps, schema_editor):
    Run = apps.get_model("agent_sessions", "Run")
    SpendRecord = apps.get_model("agent_sessions", "SpendRecord")
    Provider = apps.get_model("core", "Provider")

    provider_names = dict(Provider.objects.values_list("slug", "display_name"))
    runs = (
        Run.objects
        .filter(models.Q(usage_by_model__isnull=False) | models.Q(input_tokens__isnull=False))
        .exclude(spend_records__task="")
        .only(
            "agent_model",
            "usage_by_model",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
            "created_at",
            "session_id",
            "repo_id",
            "trigger_type",
            "user_id",
            "external_provider",
            "external_uid",
            "external_username",
        )
        .order_by("pk")
    )
    batch = []
    for run in runs.iterator(chunk_size=BATCH_SIZE):
        batch.extend(SpendRecord(**row) for row in records_for_run(run, provider_names))
        if len(batch) >= BATCH_SIZE:
            SpendRecord.objects.bulk_create(batch)
            batch = []
    if batch:
        SpendRecord.objects.bulk_create(batch)


def unbackfill(apps, schema_editor):
    SpendRecord = apps.get_model("agent_sessions", "SpendRecord")
    SpendRecord.objects.filter(task="", reported_cost_usd__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("agent_sessions", "0038_spendrecord"), ("core", "0026_sandbox_browser_settings")]

    operations = [migrations.RunPython(backfill, unbackfill)]
