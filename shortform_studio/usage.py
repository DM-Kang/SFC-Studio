"""Context-scoped API usage, token, and estimated USD cost tracking per item workflow run."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

_ACTIVE_LEDGER: contextvars.ContextVar["UsageLedger | None"] = contextvars.ContextVar("shortform_usage_ledger", default=None)

# Estimated USD rates per 1M tokens (input, output, cached_input) for active text/multimodal models.
_LLM_RATES_PER_M: list[tuple[str, tuple[float, float, float]]] = [
    ("gemini-4-hydrogen", (1.50, 7.50, 0.075)),
    ("gemini-3.1-pro", (2.00, 12.00, 0.20)),
    ("gemini-3-pro", (2.00, 12.00, 0.20)),
    ("gemini-3-flash", (0.50, 3.00, 0.05)),
    ("gemini-3.8-flash", (0.50, 3.00, 0.05)),
    ("gemini-3.7-flash", (0.50, 3.00, 0.05)),
    ("gemini-3.1-flash-lite", (0.10, 0.40, 0.01)),
    ("gemini-flash", (0.35, 1.50, 0.035)),
    ("gpt-4.1-mini", (0.40, 1.60, 0.10)),
    ("gpt-4.1", (2.00, 8.00, 0.50)),
    ("gpt-5", (1.25, 10.00, 0.125)),
    ("claude-sonnet", (3.00, 15.00, 0.30)),
    ("claude-haiku", (0.80, 4.00, 0.08)),
]

_IMAGE_RATES_USD: list[tuple[str, float]] = [
    ("gemini-3-pro-image", 0.134),
    ("gemini-3.1-flash-image", 0.067),
    ("gemini-2.5-flash-image", 0.039),
    ("gpt-image-1", 0.042),
]


def _lookup_llm_rate(model: str) -> tuple[float, float, float]:
    low = str(model or "").lower()
    if low.startswith("ollama") or low.startswith("local"):
        return (0.0, 0.0, 0.0)
    for prefix, rates in _LLM_RATES_PER_M:
        if prefix in low:
            return rates
    return (1.00, 5.00, 0.10)


def _lookup_image_rate(model: str) -> float:
    low = str(model or "").lower()
    for prefix, rate in _IMAGE_RATES_USD:
        if prefix in low:
            return rate
    return 0.08


class UsageLedger:
    """Thread-local accumulator for API calls, token counts, media durations, and estimated USD cost."""

    def __init__(self, label: str = "workflow") -> None:
        self.label = label
        self.calls: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cached_tokens: int = 0
        self.tavily_credits: float = 0.0
        self.usd: float = 0.0
        self.by_category: dict[str, dict[str, Any]] = {}
        self.models: dict[str, int] = {}

    def _bucket(self, category: str) -> dict[str, Any]:
        if category not in self.by_category:
            self.by_category[category] = {"calls": 0, "usd": 0.0}
        return self.by_category[category]

    def add_llm(
        self,
        provider: str,
        model: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> None:
        in_tok = max(0, int(input_tokens or 0))
        out_tok = max(0, int(output_tokens or 0))
        cache_tok = max(0, min(in_tok, int(cached_tokens or 0)))
        non_cached_in = max(0, in_tok - cache_tok)
        in_rate, out_rate, cache_rate = _lookup_llm_rate(model if provider != "ollama" else "ollama")
        cost = (non_cached_in * in_rate + cache_tok * cache_rate + out_tok * out_rate) / 1_000_000.0
        self.calls += 1
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.cached_tokens += cache_tok
        self.usd += cost
        bucket = self._bucket("llm")
        bucket["calls"] += 1
        bucket["input_tokens"] = bucket.get("input_tokens", 0) + in_tok
        bucket["output_tokens"] = bucket.get("output_tokens", 0) + out_tok
        bucket["usd"] = round(bucket["usd"] + cost, 6)
        self.models[model or provider] = self.models.get(model or provider, 0) + 1

    def add_media(
        self,
        category: str,
        model: str,
        *,
        usd: float,
        units: float = 1.0,
        unit_label: str = "count",
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        safe_usd = max(0.0, float(usd or 0.0))
        in_tok = max(0, int(input_tokens or 0))
        out_tok = max(0, int(output_tokens or 0))
        self.calls += 1
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.usd += safe_usd
        bucket = self._bucket(category)
        bucket["calls"] += 1
        bucket[unit_label] = round(float(bucket.get(unit_label, 0.0)) + float(units or 0.0), 2)
        bucket["usd"] = round(bucket["usd"] + safe_usd, 6)
        self.models[model] = self.models.get(model, 0) + 1

    def add_search(self, credits: float, *, usd_per_credit: float = 0.008, model: str = "tavily") -> None:
        safe_credits = max(0.0, float(credits or 0.0))
        cost = safe_credits * usd_per_credit
        self.calls += 1
        self.tavily_credits += safe_credits
        self.usd += cost
        bucket = self._bucket("search")
        bucket["calls"] += 1
        bucket["credits"] = round(float(bucket.get("credits", 0.0)) + safe_credits, 2)
        bucket["usd"] = round(bucket["usd"] + cost, 6)
        self.models[model] = self.models.get(model, 0) + 1

    def to_run_record(self) -> dict[str, Any]:
        return {
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label": self.label,
            "usd": round(self.usd, 4),
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "tavily_credits": round(self.tavily_credits, 2),
            "by_category": self.by_category,
            "models": self.models,
        }


def get_active_usage_ledger() -> UsageLedger | None:
    return _ACTIVE_LEDGER.get()


@contextmanager
def track_usage_scope(label: str = "workflow") -> Iterator[UsageLedger]:
    """@brief Bind a UsageLedger to the current execution context if none is active yet."""
    existing = _ACTIVE_LEDGER.get()
    if existing is not None:
        yield existing
        return
    ledger = UsageLedger(label=label)
    token = _ACTIVE_LEDGER.set(ledger)
    try:
        yield ledger
    finally:
        _ACTIVE_LEDGER.reset(token)


def record_gemini_response_usage(provider: str, model: str, payload: dict[str, Any] | Any) -> None:
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None or not isinstance(payload, dict):
        return
    meta = payload.get("usageMetadata")
    if not isinstance(meta, dict):
        ledger.add_llm(provider, model, input_tokens=0, output_tokens=0)
        return
    in_tok = int(meta.get("promptTokenCount") or 0)
    cand_tok = int(meta.get("candidatesTokenCount") or 0)
    think_tok = int(meta.get("thoughtsTokenCount") or 0)
    cached_tok = int(meta.get("cachedContentTokenCount") or 0)
    ledger.add_llm(provider, model, input_tokens=in_tok, output_tokens=cand_tok + think_tok, cached_tokens=cached_tok)


def record_openai_response_usage(provider: str, model: str, payload: dict[str, Any] | Any) -> None:
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None or not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        ledger.add_llm(provider, model)
        return
    in_tok = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    cached_tok = int(details.get("cached_tokens") or 0)
    ledger.add_llm(provider, model, input_tokens=in_tok, output_tokens=out_tok, cached_tokens=cached_tok)


def record_anthropic_response_usage(model: str, payload: dict[str, Any] | Any) -> None:
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None or not isinstance(payload, dict):
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        ledger.add_llm("anthropic_api", model)
        return
    ledger.add_llm(
        "anthropic_api",
        model,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


def record_omni_video_usage(model: str, payload: dict[str, Any] | Any, *, duration_seconds: float) -> None:
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None:
        return
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if isinstance(usage, dict) and (usage.get("total_output_tokens") or usage.get("total_input_tokens")):
        in_tok = int(usage.get("total_input_tokens") or 0)
        out_tok = int(usage.get("total_output_tokens") or 0)
        cost = (in_tok * 1.50 + out_tok * 17.50) / 1_000_000.0
    else:
        in_tok = 0
        out_tok = 0
        cost = max(1.0, float(duration_seconds or 4.0)) * 0.07
    ledger.add_media("video", model, usd=cost, units=max(1.0, float(duration_seconds or 4.0)), unit_label="seconds", input_tokens=in_tok, output_tokens=out_tok)


def record_image_usage(model: str, count: int = 1, payload: dict[str, Any] | Any = None) -> None:
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None:
        return
    rate = _lookup_image_rate(model)
    in_tok = 0
    out_tok = 0
    if isinstance(payload, dict) and isinstance(payload.get("usageMetadata"), dict):
        meta = payload["usageMetadata"]
        in_tok = int(meta.get("promptTokenCount") or 0)
        out_tok = int(meta.get("candidatesTokenCount") or 0)
    ledger.add_media("image", model, usd=rate * max(1, int(count)), units=max(1, int(count)), unit_label="count", input_tokens=in_tok, output_tokens=out_tok)


def record_bgm_usage(model: str) -> None:
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None:
        return
    cost = 0.08 if "3.5" in str(model or "") or "pro" in str(model or "").lower() else 0.04
    ledger.add_media("audio_bgm", model, usd=cost, units=1.0, unit_label="tracks")


def record_tts_usage(provider: str, model: str, *, payload: dict[str, Any] | Any = None, duration_seconds: float = 0.0, char_count: int = 0) -> None:
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None:
        return
    if provider == "gemini_tts":
        meta = payload.get("usageMetadata") if isinstance(payload, dict) and isinstance(payload.get("usageMetadata"), dict) else {}
        in_tok = int(meta.get("promptTokenCount") or max(10, char_count // 2))
        out_tok = int(meta.get("candidatesTokenCount") or max(25, int(round(max(1.0, float(duration_seconds or 2.0)) * 25))))
        cost = (in_tok * 1.00 + out_tok * 20.00) / 1_000_000.0
        ledger.add_media("audio_tts", model, usd=cost, units=max(0.5, float(duration_seconds or 2.0)), unit_label="seconds", input_tokens=in_tok, output_tokens=out_tok)
    elif provider == "openai_tts":
        cost = (max(1, char_count) / 1000.0) * 0.015
        ledger.add_media("audio_tts", model, usd=cost, units=max(0.5, float(duration_seconds or 2.0)), unit_label="seconds")
    else:
        cost = (max(1, char_count) / 1000.0) * 0.18
        ledger.add_media("audio_tts", model, usd=cost, units=max(0.5, float(duration_seconds or 2.0)), unit_label="seconds")


def record_tavily_usage(endpoint: str, *, depth: str = "basic", urls_count: int = 1) -> None:
    ledger = _ACTIVE_LEDGER.get()
    if ledger is None:
        return
    if endpoint == "extract":
        rate = 2.0 if depth == "advanced" else 1.0
        credits = max(0.2, (max(1, urls_count) / 5.0) * rate)
    else:
        credits = 2.0 if depth == "advanced" else 1.0
    ledger.add_search(credits, model=f"tavily:{endpoint}")


def merge_usage_stats(existing: dict[str, Any] | None, ledger: UsageLedger) -> dict[str, Any]:
    """@brief Fold a completed UsageLedger run summary into an item's persistent usage_stats payload."""
    base = dict(existing) if isinstance(existing, dict) else {}
    run_entry = ledger.to_run_record()
    runs = list(base.get("runs") or [])
    if run_entry["calls"] > 0 or run_entry["usd"] > 0:
        runs.append(run_entry)
    total_usd = round(float(base.get("total_usd") or 0.0) + run_entry["usd"], 4)
    total_calls = int(base.get("total_calls") or 0) + run_entry["calls"]
    total_in = int(base.get("total_input_tokens") or 0) + run_entry["input_tokens"]
    total_out = int(base.get("total_output_tokens") or 0) + run_entry["output_tokens"]
    total_credits = round(float(base.get("tavily_credits") or 0.0) + run_entry["tavily_credits"], 2)
    return {
        "total_usd": total_usd,
        "total_calls": total_calls,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "tavily_credits": total_credits,
        "latest_run": run_entry if (run_entry["calls"] > 0 or run_entry["usd"] > 0) else base.get("latest_run"),
        "runs": runs[-15:],
    }
