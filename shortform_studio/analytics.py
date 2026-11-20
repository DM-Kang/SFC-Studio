"""Local analytics aggregation for evidence-informed future content planning."""

from __future__ import annotations

import csv
import io
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
    grouped: dict[str, list[dict[str, Any]]] = {"youtube": [], "instagram": [], "tiktok": []}
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


def build_analytics_dashboard(items: list[dict[str, Any]]) -> dict[str, Any]:
    """@brief Aggregate platform totals, per-series comparisons, and recent post rows."""
    platform_totals: dict[str, dict[str, Any]] = {
        platform: {"published_count": 0, "measured_count": 0, "total_views": 0, "engagement_rates": [], "watch_seconds_per_view": []}
        for platform in ("youtube", "instagram", "tiktok")
    }
    by_series: dict[str, dict[str, Any]] = {}
    recent_posts: list[dict[str, Any]] = []

    for item in items:
        publications = item.get("publications") if isinstance(item.get("publications"), dict) else {}
        analytics = item.get("analytics") if isinstance(item.get("analytics"), dict) else {}
        series = item.get("series_template_snapshot") if isinstance(item.get("series_template_snapshot"), dict) else {}
        series_name = str(series.get("name") or "일반 / 템플릿 없음")
        series_bucket = by_series.setdefault(series_name, {"series_name": series_name, "posts": 0, "total_views": 0, "engagement_rates": []})

        for platform in ("youtube", "instagram", "tiktok"):
            pub = publications.get(platform) or {}
            if pub.get("status") != "published":
                continue
            platform_totals[platform]["published_count"] += 1
            series_bucket["posts"] += 1
            records = analytics.get(platform, [])
            dated = [(p, rec) for rec in (records if isinstance(records, list) else []) if isinstance(rec, dict) and (p := _parse_datetime(rec.get("measured_at"))) is not None]
            latest = max(dated, key=lambda pair: pair[0])[1] if dated else None
            views = latest.get("views") if latest and isinstance(latest.get("views"), int) and not isinstance(latest.get("views"), bool) else None
            engagement_rate = None
            watch_per_view = None
            if latest and views is not None:
                platform_totals[platform]["measured_count"] += 1
                platform_totals[platform]["total_views"] += views
                series_bucket["total_views"] += views
                interactions = [latest.get(f) for f in ENGAGEMENT_FIELDS if isinstance(latest.get(f), int) and not isinstance(latest.get(f), bool)]
                if views > 0 and interactions:
                    engagement_rate = round(sum(interactions) / views, 4)
                    platform_totals[platform]["engagement_rates"].append(engagement_rate)
                    series_bucket["engagement_rates"].append(engagement_rate)
                watch_time = latest.get("watch_time_seconds")
                if views > 0 and isinstance(watch_time, (int, float)) and not isinstance(watch_time, bool) and watch_time >= 0:
                    watch_per_view = round(float(watch_time) / views, 2)
                    platform_totals[platform]["watch_seconds_per_view"].append(watch_per_view)
            recent_posts.append(
                {
                    "item_id": item.get("id"),
                    "title": str((item.get("plan") or {}).get("title") or item.get("topic") or ""),
                    "series_name": series_name,
                    "platform": platform,
                    "published_at": pub.get("updated_at"),
                    "views": views,
                    "engagement_rate": engagement_rate,
                    "average_watch_seconds_per_view": watch_per_view,
                    "measured_at": latest.get("measured_at") if latest else None,
                }
            )

    platforms_out: dict[str, Any] = {}
    for platform, data in platform_totals.items():
        rates = data["engagement_rates"]
        watches = data["watch_seconds_per_view"]
        platforms_out[platform] = {
            "published_count": data["published_count"],
            "measured_count": data["measured_count"],
            "total_views": data["total_views"],
            "median_engagement_rate": round(statistics.median(rates), 4) if rates else None,
            "median_watch_seconds_per_view": round(statistics.median(watches), 2) if watches else None,
        }

    series_out = [
        {
            "series_name": entry["series_name"],
            "posts": entry["posts"],
            "total_views": entry["total_views"],
            "median_engagement_rate": round(statistics.median(entry["engagement_rates"]), 4) if entry["engagement_rates"] else None,
        }
        for entry in sorted(by_series.values(), key=lambda row: row["total_views"], reverse=True)
    ]
    recent_posts.sort(key=lambda row: str(row.get("published_at") or ""), reverse=True)
    return {"platforms": platforms_out, "series": series_out, "recent_posts": recent_posts[:20]}


def _safe_csv_cell(value: Any) -> str:
    """@brief Neutralize spreadsheet formula prefixes when exporting user text to CSV."""
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{text}"
    return text


def export_analytics_csv(items: list[dict[str, Any]]) -> str:
    """@brief Export manually recorded platform metrics across items as UTF-8 CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["item_id", "title", "series_name", "platform", "measured_at", "views", "watch_time_seconds", "likes", "comments", "shares", "saves", "notes"])
    for item in items:
        analytics = item.get("analytics") if isinstance(item.get("analytics"), dict) else {}
        title = str((item.get("plan") or {}).get("title") or item.get("topic") or "")
        series_name = str((item.get("series_template_snapshot") or {}).get("name") or "")
        for platform, records in analytics.items():
            if not isinstance(records, list):
                continue
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                writer.writerow(
                    [
                        _safe_csv_cell(item.get("id")),
                        _safe_csv_cell(title),
                        _safe_csv_cell(series_name),
                        _safe_csv_cell(platform),
                        _safe_csv_cell(rec.get("measured_at")),
                        rec.get("views") if rec.get("views") is not None else "",
                        rec.get("watch_time_seconds") if rec.get("watch_time_seconds") is not None else "",
                        rec.get("likes") if rec.get("likes") is not None else "",
                        rec.get("comments") if rec.get("comments") is not None else "",
                        rec.get("shares") if rec.get("shares") is not None else "",
                        rec.get("saves") if rec.get("saves") is not None else "",
                        _safe_csv_cell(rec.get("notes", "")),
                    ]
                )
    return output.getvalue()


def parse_analytics_csv(csv_text: str) -> list[dict[str, Any]]:
    """@brief Parse uploaded analytics CSV rows into validated metric snapshots."""
    if not isinstance(csv_text, str) or not csv_text.strip():
        raise ValueError("CSV content cannot be empty.")
    if len(csv_text) > 5_000_000:
        raise ValueError("CSV upload exceeds 5 MB limit.")
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames or not {"item_id", "platform", "measured_at"}.issubset(set(reader.fieldnames)):
        raise ValueError("CSV must include item_id, platform, and measured_at columns.")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(reader, 1):
        if index > 2000:
            raise ValueError("CSV import is limited to 2,000 rows per upload.")
        item_id = str(raw.get("item_id") or "").strip()
        platform = str(raw.get("platform") or "").strip().lower()
        measured = _parse_datetime(str(raw.get("measured_at") or "").strip())
        if not item_id or platform not in {"youtube", "instagram", "tiktok"} or measured is None:
            raise ValueError(f"Row {index}: invalid item_id, platform, or measured_at timestamp.")

        def _opt_int(key: str) -> int | None:
            val = str(raw.get(key) or "").strip()
            if val == "":
                return None
            parsed = int(val)
            if parsed < 0:
                raise ValueError(f"Row {index}: {key} cannot be negative.")
            return parsed

        watch_raw = str(raw.get("watch_time_seconds") or "").strip()
        watch_time = float(watch_raw) if watch_raw != "" else None
        if watch_time is not None and watch_time < 0:
            raise ValueError(f"Row {index}: watch_time_seconds cannot be negative.")
        snapshot = {
            "item_id": item_id,
            "platform": platform,
            "measured_at": measured.isoformat(timespec="seconds"),
            "views": _opt_int("views"),
            "watch_time_seconds": watch_time,
            "likes": _opt_int("likes"),
            "comments": _opt_int("comments"),
            "shares": _opt_int("shares"),
            "saves": _opt_int("saves"),
            "notes": str(raw.get("notes") or "").strip()[:500],
        }
        if all(snapshot[k] is None for k in ("views", "watch_time_seconds", "likes", "comments", "shares", "saves")):
            raise ValueError(f"Row {index}: at least one metric value is required.")
        rows.append(snapshot)
    return rows
