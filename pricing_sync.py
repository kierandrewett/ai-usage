"""
pricing_sync.py - Resolve canonical per-model pricing from models.dev and emit a
pricing.json the dashboard overlays onto its built-in PRICING table.

models.dev lists the same model under dozens of gateways at wildly different
prices (first-party list price, discounted resellers, marked-up routers). We
resolve a single canonical price per model by preferring the first-party
provider (openai, anthropic, deepseek, google, ...) and falling back to the
most common (input, output) pair across all gateways when no first-party entry
exists.

Usage:
    python pricing_sync.py            # refresh cache if stale, write pricing.json
    python pricing_sync.py --refresh  # force re-download of the models.dev catalog
    python pricing_sync.py --print    # print resolved prices, don't write

The dashboard imports load_pricing() and overlays the result on its PRICING map;
if the catalog can't be fetched it silently falls back to the built-in table.
"""

import json
import os
import time
import urllib.request
from collections import Counter
from pathlib import Path

CATALOG_URL = "https://models.dev/api.json"
CACHE_PATH = Path.home() / ".claude" / "models_dev.json"
PRICING_PATH = Path(__file__).resolve().parent / "pricing.json"
CACHE_TTL_SECONDS = 24 * 60 * 60

# First-party providers, in preference order. A model's canonical list price is
# whatever its own vendor charges; everyone else is reselling.
FIRST_PARTY = (
    "openai",
    "anthropic",
    "google",
    "google-vertex",
    "deepseek",
    "moonshotai",
    "xai",
    "mistral",
    "alibaba",
)

# Map a DB model name to the models.dev entry that carries its real list price.
# Used for internal/unlisted names and for bare names that collide with a
# different SKU (e.g. Oh My Pi's "kimi-code/k3" → bare "k3" matches a free
# coding-plan SKU; the metered list price lives under "kimi-k3").
ALIASES = {
    "codex-auto-review": "gpt-5.3-codex",  # a codex review pass; price as gpt-5.3-codex
    "k3": "kimi-k3",                       # Oh My Pi kimi-code/k3 → Kimi K3 list price
}


def fetch_catalog(refresh=False):
    """Return the models.dev catalog dict, using a 24h on-disk cache."""
    if not refresh and CACHE_PATH.exists():
        age = time.time() - CACHE_PATH.stat().st_mtime
        if age < CACHE_TTL_SECONDS:
            try:
                return json.loads(CACHE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass  # fall through and re-download

    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "claude-usage/pricing_sync"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_bytes(raw)
    return json.loads(raw)


def _bare(model_id):
    """models.dev ids can be nested ('google/gemini-3-pro'); key by last segment."""
    return model_id.rsplit("/", 1)[-1].lower()


def build_index(catalog):
    """{bare_model_name: {provider_id: cost_dict}} across every gateway."""
    index = {}
    for provider_id, provider in catalog.items():
        for model_id, model in (provider.get("models") or {}).items():
            cost = model.get("cost")
            if not cost:
                continue
            index.setdefault(_bare(model_id), {})[provider_id] = cost
    return index


def _normalize(cost):
    """Flatten a models.dev cost object to the dashboard's four fields.

    Base-tier prices only (ignore context_over_200k / tiers — the dashboard
    doesn't model tiered pricing). Fill sensible defaults for cache rates that a
    provider omits: cache_write defaults to the input rate (OpenAI convention),
    cache_read to 10% of input.
    """
    inp = cost.get("input")
    out = cost.get("output")
    if inp is None or out is None:
        return None
    cache_read = cost.get("cache_read")
    cache_write = cost.get("cache_write")
    return {
        "input": inp,
        "output": out,
        "cache_write": cache_write if cache_write is not None else inp,
        "cache_read": cache_read if cache_read is not None else round(inp * 0.1, 6),
    }


def resolve(name, index):
    """Canonical price for a bare model name, or None if unknown."""
    key = ALIASES.get(name.lower(), name.lower())
    providers = index.get(key)
    if not providers:
        return None

    # Prefer the first-party vendor's own price.
    for vendor in FIRST_PARTY:
        if vendor in providers:
            norm = _normalize(providers[vendor])
            if norm:
                return norm

    # No first-party entry: take the most common (input, output) pair across
    # gateways — resellers cluster at list price, outliers are discounts/markups.
    pairs = Counter()
    by_pair = {}
    for cost in providers.values():
        norm = _normalize(cost)
        if not norm:
            continue
        pair = (norm["input"], norm["output"])
        pairs[pair] += 1
        by_pair.setdefault(pair, norm)
    if not pairs:
        return None
    return by_pair[pairs.most_common(1)[0][0]]


def generate(model_names, refresh=False):
    """Resolve canonical prices for a list of bare model names. Returns
    (pricing_dict, unresolved_list)."""
    catalog = fetch_catalog(refresh=refresh)
    index = build_index(catalog)
    pricing, unresolved = {}, []
    for name in sorted(set(model_names)):
        price = resolve(name, index)
        if price:
            pricing[name] = price
        else:
            unresolved.append(name)
    return pricing, unresolved


def _db_bare_models(db_path):
    """Bare model names (provider prefix stripped) present in usage.db that need a
    token rate: sources with no reported cost (NULL) OR a reported cost of 0 (some
    Oh My Pi OAuth providers log $0), so those fall back to a token estimate."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT model FROM sessions "
            "WHERE (actual_cost_usd IS NULL OR actual_cost_usd = 0) AND model != ''"
        ).fetchall()
    finally:
        conn.close()
    names = set()
    for (model,) in rows:
        names.add(model.split("/", 1)[1] if "/" in model else model)
    return names


def load_pricing():
    """Read the generated pricing.json (written by a prior sync). Returns {} if
    absent so the dashboard cleanly falls back to its built-in table."""
    try:
        return json.loads(PRICING_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sync model pricing from models.dev")
    parser.add_argument("--refresh", action="store_true", help="force re-download the catalog")
    parser.add_argument("--print", dest="print_only", action="store_true", help="print, don't write")
    args = parser.parse_args()

    from scanner import DB_PATH

    if Path(DB_PATH).exists():
        names = _db_bare_models(DB_PATH)
        print(f"Resolving pricing for {len(names)} models seen in {DB_PATH} ...")
    else:
        names = set()
        print(f"No usage.db at {DB_PATH}; resolving nothing.")

    pricing, unresolved = generate(names, refresh=args.refresh)

    for name in sorted(pricing):
        p = pricing[name]
        print(f"  {name:34} in={p['input']:<7} out={p['output']:<7} "
              f"cw={p['cache_write']:<7} cr={p['cache_read']}")
    if unresolved:
        print(f"\n  Unresolved (no models.dev entry, priced by dashboard fallback): "
              f"{', '.join(sorted(unresolved))}")

    if not args.print_only:
        PRICING_PATH.write_text(json.dumps(pricing, indent=2, sort_keys=True) + "\n")
        print(f"\nWrote {len(pricing)} entries to {PRICING_PATH}")


if __name__ == "__main__":
    main()
