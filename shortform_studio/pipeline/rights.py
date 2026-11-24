"""Rights ledger construction and approval confirmation helpers."""

from __future__ import annotations

from typing import Any
from uuid import uuid4


def final_video_rights_record(source: str) -> dict[str, Any]:
    """@brief Create an unresolved rights record for a newly replaced final video."""
    return {
        "id": str(uuid4()),
        "asset_name": "Final video and all included audio, visuals, and voices",
        "asset_type": "final_video",
        "source_description": f"Generated or uploaded via {source}"[:300],
        "source_url": "",
        "license_name": "",
        "license_url": "",
        "rights_basis": "",
        "attribution": "",
        "included": True,
        "rights_status": "unverified",
    }


def replace_final_video_rights_record(existing: Any, source: str) -> list[dict[str, Any]]:
    """@brief Preserve supporting-source records while resetting the replaced final video's rights."""
    supporting_assets = []
    if isinstance(existing, list):
        supporting_assets = [
            dict(asset)
            for asset in existing
            if isinstance(asset, dict) and asset.get("asset_type") != "final_video"
        ]
    return [*supporting_assets, final_video_rights_record(source)]


def _merge_supporting_rights_record(existing: Any, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    """@brief Append a supporting asset rights record once based on source URL or asset name."""
    records = [dict(entry) for entry in existing if isinstance(entry, dict)] if isinstance(existing, list) else []
    source_url = str(candidate.get("source_url") or "")
    name = str(candidate.get("asset_name") or "")
    for entry in records:
        if source_url and entry.get("source_url") == source_url:
            return records
        if not source_url and name and entry.get("asset_name") == name and entry.get("asset_type") == candidate.get("asset_type"):
            return records
    records.append({"id": str(uuid4()), **candidate})
    return records


def _material_rights_record(candidate: dict[str, Any], fallback_id: str) -> dict[str, Any]:
    """@brief Build a normalized unverified rights-ledger entry for a stock material clip."""
    provider_name = str(candidate.get("provider") or "Stock").strip() or "Stock"
    default_license_url = "https://pixabay.com/service/license-summary/" if provider_name.lower() == "pixabay" else "https://www.pexels.com/license/"
    return {
        "asset_name": candidate.get("title") or f"{provider_name} {fallback_id}",
        "asset_type": "video",
        "source_description": f"{candidate.get('author') or 'Unknown'} · {provider_name}",
        "source_url": candidate.get("source_url") or "",
        "license_name": f"{provider_name} License · verify current terms",
        "license_url": candidate.get("license_url") or default_license_url,
        "rights_basis": "",
        "attribution": candidate.get("author") or "",
        "included": True,
        "rights_status": "unverified",
    }


def confirm_item_rights_for_approval(item: dict[str, Any]) -> list[dict[str, Any]]:
    """@brief Mark included production and stock rights entries as operator-confirmed during approval."""
    existing = item.get("rights_assets")
    confirmed: list[dict[str, Any]] = []
    if isinstance(existing, list):
        for asset in existing:
            if not isinstance(asset, dict):
                continue
            entry = dict(asset)
            if entry.get("included", True) and entry.get("rights_status") != "cleared":
                entry["rights_status"] = "cleared"
                if not str(entry.get("rights_basis") or "").strip():
                    basis = str(entry.get("license_name") or entry.get("source_description") or "Operator verified final production and source rights on approval").strip()
                    entry["rights_basis"] = basis[:500]
            confirmed.append(entry)
    if not any(a.get("included", True) for a in confirmed):
        record = final_video_rights_record("autopilot production")
        record["rights_status"] = "cleared"
        record["rights_basis"] = "Operator confirmed final production and source rights upon final approval"
        confirmed.append(record)
    return confirmed
