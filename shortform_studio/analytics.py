"""Local analytics aggregation for evidence-informed future content planning."""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Any

ENGAGEMENT_FIELDS = ("likes", "comments", "shares", "saves")


def summarize_analytics(items: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """@brief Summarize latest manually recorded platform metrics without causal claims.

    @param items Durable content items with publication and analytics snapshots.
    @param now Optional aware time for deterministic tests.
    @returns Per-platform medians and a small set of recent topic examples.
    """
    current_time = now or datetime.now(timezone.utc)
    grouped: dict[str, list[dict[str, Any]]] = {"youtube": [], "instagram": []}
    for item in items:
        analytics = item.get("analytics") if isinstance(item.get("analytics"), dict) else {}
        publications = item.get("publications") if isinstance(item.get("publications"), dict) else {}
        for platform in grouped:
            records = analytics.get(platform, [])
            publication = publications.get(platform, {})
            if publication.get("status") != "published" or not isinstance(records, list) or not records:
                continue
            dated_records = [(parsed, entry) for entry in records if isinstance(entry, dict) and (parsed := _parse_datetime(entry.get("measured_at"))) is not None]
            if not dated_records:
                continue
            measured_at, latest = max(dated_records, key=lambda record: record[0])
            published_at = _parse_datetime(publication.get("updated_at"))
            if published_at is None or measured_at < published_at or measured_at > current_time:
                continue
            elapsed_days = max(1.0, (measured_at - published_at).total_seconds() / 86_400)
            sample: dict[str, Any] = {"topic": str(item.get("topic") or "")[:100], "measured_at": latest["measured_at"]}
            views = latest.get("views")
            if isinstance(views, int) and not isinstance(views, bool) and views >= 0:
                sample["views_per_day"] = round(views / elapsed_days, 1)
                interactions = [value for field in ENGAGEMENT_FIELDS if isinstance((value := latest.get(field)), int) and not isinstance(value, bool) and value >= 0]
                if views > 0 and interactions:
                    engagement = sum(interactions)
                    sample["engagement_rate"] = round(engagement / views, 4)
                watch_time = latest.get("watch_time_seconds")
                if views > 0 and isinstance(watch_time, (int, float)) and not isinstance(watch_time, bool) and watch_time >= 0:
                    sample["average_watch_seconds_per_view"] = round(watch_time / views, 2)
            grouped[platform].append(sample)
    summaries: dict[str, Any] = {}
    for platform, samples in grouped.items():
        rates = [sample["engagement_rate"] for sample in samples if "engagement_rate" in sample]
        views_per_day = [sample["views_per_day"] for sample in samples if "views_per_day" in sample]
        watch_times = [sample["average_watch_seconds_per_view"] for sample in samples if "average_watch_seconds_per_view" in sample]
        summaries[platform] = {
            "sample_count": len(samples),
            "median_views_per_day": round(statistics.median(views_per_day), 1) if views_per_day else None,
            "median_engagement_rate": round(statistics.median(rates), 4) if rates else None,
            "median_average_watch_seconds_per_view": round(statistics.median(watch_times), 2) if watch_times else None,
            "recent_samples": sorted(samples, key=lambda sample: sample["measured_at"], reverse=True)[:5],
        }
    return {
        "platforms": summaries,
        "note": "Metrics are manually entered observations; patterns are guidance, not proof of causation or reach guarantees.",
    }


def _parse_datetime(value: Any) -> datetime | None:
    """@brief Parse persisted ISO timestamps and normalize them to aware UTC values."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def summarize_experiment(experiment: dict[str, Any], items_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """@brief Compare latest manually entered metrics for two published variants.

    @param experiment Persisted A/B experiment with variant labels and item IDs.
    @param items_by_id Published items indexed by their local IDs.
    @returns A side-by-side observation without a statistical-significance claim.
    """
    platform = str(experiment.get("platform") or "")
    variants: list[dict[str, Any]] = []
    for item_key, label_key in (("item_a", "label_a"), ("item_b", "label_b")):
        item = items_by_id.get(str(experiment.get(item_key) or ""), {})
        publication = (item.get("publications") or {}).get(platform, {})
        records = (item.get("analytics") or {}).get(platform, [])
        dated = [(parsed, record) for record in records if isinstance(record, dict) and (parsed := _parse_datetime(record.get("measured_at"))) is not None]
        latest = max(dated, key=lambda entry: entry[0])[1] if dated else {}
        published_at = _parse_datetime(publication.get("updated_at"))
        measured_at = _parse_datetime(latest.get("measured_at"))
        views = latest.get("views") if isinstance(latest.get("views"), int) and not isinstance(latest.get("views"), bool) else None
        interactions = [latest.get(field) for field in ENGAGEMENT_FIELDS if isinstance(latest.get(field), int) and not isinstance(latest.get(field), bool)]
        elapsed_days = (measured_at - published_at).total_seconds() / 86400 if measured_at and published_at and measured_at > published_at else 0
        variants.append({
            "label": str(experiment.get(label_key) or item_key[-1:]),
            "item_id": item.get("id"),
            "topic": str(item.get("topic") or ""),
            "published_at": publication.get("updated_at"),
            "measured_at": latest.get("measured_at"),
            "views": views,
            "views_per_day": round(views / max(1.0, elapsed_days), 1) if views is not None and elapsed_days else None,
            "engagement_rate": round(sum(interactions) / views, 4) if views and interactions else None,
        })
    measured_times = [_parse_datetime(variant.get("measured_at")) for variant in variants]
    gap_hours = abs((measured_times[0] - measured_times[1]).total_seconds()) / 3600 if all(measured_times) else None
    return {
        "id": experiment.get("id"),
        "name": experiment.get("name"),
        "hypothesis": experiment.get("hypothesis"),
        "platform": platform,
        "status": experiment.get("status"),
        "created_at": experiment.get("created_at"),
        "updated_at": experiment.get("updated_at"),
        "measurement_gap_hours": round(gap_hours, 1) if gap_hours is not None else None,
        "variants": variants,
    }
