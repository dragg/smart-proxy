"""Usage-cost dashboard (``GET /_usage``), served over aiohttp.

Renders per-proxy-key / per-model cost from the shared ``usage_daily`` table.
The module is decoupled from any specific service: callers register the route
via :func:`register_usage_dashboard`, injecting how to authorize a request and
how to obtain the :class:`~smart_proxy.db.Database`. This lets both the Anthropic
proxy and (temporarily) the smart proxy serve the same dashboard without
duplicating the rendering logic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from html import escape

from aiohttp import web

from smart_proxy.db import Database
from smart_proxy.usage import build_price_lookup, calculate_cost


def _parse_usage_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _usage_date_range(request: web.Request) -> tuple[str, str] | web.Response:
    today = datetime.now(timezone.utc).date()
    default_start = today - timedelta(days=6)
    start_raw = (request.query.get("start") or default_start.isoformat()).strip()
    end_raw = (request.query.get("end") or today.isoformat()).strip()

    try:
        start = _parse_usage_date(start_raw)
        end = _parse_usage_date(end_raw)
    except ValueError:
        return web.Response(
            status=400,
            text="start and end must use YYYY-MM-DD",
            content_type="text/plain",
        )

    if start > end:
        return web.Response(
            status=400,
            text="start must be before or equal to end",
            content_type="text/plain",
        )

    return start.isoformat(), end.isoformat()


def _int_usage(row: dict, key: str) -> int:
    return int(row.get(key) or 0)


def _short_proxy_key(proxy_key: str) -> str:
    if not proxy_key:
        return "(no auth)"
    if len(proxy_key) <= 12:
        return proxy_key
    return f"{proxy_key[:12]}..."


def _usage_key_label(row: dict) -> str:
    key_name = str(row.get("key_name") or "").strip()
    if key_name:
        return key_name
    group_name = str(row.get("group_name") or "").strip()
    if group_name:
        return group_name
    return _short_proxy_key(str(row.get("proxy_key") or ""))


def _format_int(value: int) -> str:
    return f"{value:,}"


def _format_cost(value: float | None, *, partial: bool = False, unknown: bool = False) -> str:
    if unknown or value is None:
        return "?"
    prefix = "~" if partial else ""
    return f"{prefix}${value:,.2f}"


def _format_total_cost(total: float, *, partial: bool, unknown: bool) -> str:
    prefix = "~" if partial else ""
    if unknown and total > 0:
        return f"{prefix}${total:,.2f} + ?"
    if unknown:
        return "?"
    return f"{prefix}${total:,.2f}"


def _row_cost_split(row: dict, prices: dict):
    """Compute the per-model :class:`CostBreakdown` plus its base/cache split
    for one usage row. Shared by ``_build_usage_cost_groups``,
    ``build_usage_kind_json``, and ``build_sessions_json`` so the
    base/cache convention (base = input+output+web, cache = read+5m+1h
    writes) lives in exactly one place."""
    cost = calculate_cost(
        model=str(row.get("model") or ""),
        input_tokens=_int_usage(row, "input_tokens"),
        output_tokens=_int_usage(row, "output_tokens"),
        cache_read_tokens=_int_usage(row, "cache_read_tokens"),
        cache_creation_tokens=_int_usage(row, "cache_creation_tokens"),
        cache_creation_5m_tokens=_int_usage(row, "cache_creation_5m_tokens"),
        cache_creation_1h_tokens=_int_usage(row, "cache_creation_1h_tokens"),
        web_search_requests=_int_usage(row, "web_search_requests"),
        prices=prices,
    )
    base_cost = cost.input_cost + cost.output_cost + cost.web_search_cost
    cache_cost = cost.cache_read_cost + cost.cache_write_5m_cost + cost.cache_write_1h_cost
    return cost, base_cost, cache_cost


def _build_usage_cost_groups(rows: list[dict], prices: dict) -> list[dict]:
    groups: dict[tuple[str, str, str, int], dict] = {}
    for row in rows:
        proxy_key = str(row.get("proxy_key") or "")
        group_name = str(row.get("group_name") or "")
        key_name = str(row.get("key_name") or "")
        via_openai_compat = int(row.get("via_openai_compat") or 0)
        group_id = (proxy_key, group_name, key_name, via_openai_compat)
        label = _usage_key_label(row)
        if via_openai_compat:
            label = f"{label} · OpenAI"
        group = groups.setdefault(
            group_id,
            {
                "label": label,
                "via_openai_compat": via_openai_compat,
                "proxy_key": proxy_key,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "web_search_requests": 0,
                "known_cost": 0.0,
                "base_cost": 0.0,
                "cache_cost": 0.0,
                "partial": False,
                "unknown": False,
                "models": [],
            },
        )

        input_tokens = _int_usage(row, "input_tokens")
        output_tokens = _int_usage(row, "output_tokens")
        cache_read_tokens = _int_usage(row, "cache_read_tokens")
        cache_creation_tokens = _int_usage(row, "cache_creation_tokens")
        web_search_requests = _int_usage(row, "web_search_requests")
        requests = _int_usage(row, "requests")

        cost, model_base_cost, model_cache_cost = _row_cost_split(row, prices)

        group["requests"] += requests
        group["input_tokens"] += input_tokens
        group["output_tokens"] += output_tokens
        group["cache_read_tokens"] += cache_read_tokens
        group["cache_creation_tokens"] += cache_creation_tokens
        group["web_search_requests"] += web_search_requests

        group["partial"] = group["partial"] or cost.partial
        group["unknown"] = group["unknown"] or cost.unknown_pricing
        if cost.total_cost is not None:
            group["known_cost"] += cost.total_cost
            group["base_cost"] += model_base_cost
            group["cache_cost"] += model_cache_cost

        group["models"].append(
            {
                "provider": str(row.get("provider") or ""),
                "model": str(row.get("model") or ""),
                "requests": requests,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_creation_tokens": cache_creation_tokens,
                "web_search_requests": web_search_requests,
                "cost": cost.total_cost,
                "base_cost": model_base_cost if not cost.unknown_pricing else None,
                "cache_cost": model_cache_cost if not cost.unknown_pricing else None,
                "partial": cost.partial,
                "unknown": cost.unknown_pricing,
            }
        )

    return sorted(groups.values(), key=lambda item: item["label"].lower())


def build_usage_cost_json(
    start: str, end: str, rows: list[dict], prices: dict
) -> dict:
    """JSON-serializable version of the usage dashboard data (for /api/usage)."""
    groups = _build_usage_cost_groups(rows, prices)
    total_known = sum(float(g["known_cost"]) for g in groups)
    return {
        "start": start,
        "end": end,
        "total_known_cost": round(total_known, 2),
        "partial": any(bool(g["partial"]) for g in groups),
        "unknown": any(bool(g["unknown"]) for g in groups),
        "groups": groups,
    }


def build_usage_kind_json(rows: list[dict], prices: dict) -> list[dict]:
    """Aggregate ``query_usage_by_kind`` rows (per request_kind/proxy_key/
    provider/model) into one entry per ``(request_kind, proxy_key)``, summing
    tokens and cost across models (for ``/api/usage/kinds``)."""
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        proxy_key = str(row.get("proxy_key") or "")
        request_kind = str(row.get("request_kind") or "")
        group_id = (request_kind, proxy_key)
        group = groups.setdefault(
            group_id,
            {
                "request_kind": request_kind,
                "proxy_key": proxy_key,
                "key_name": str(row.get("key_name") or ""),
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "web_search_requests": 0,
                "base_cost": 0.0,
                "cache_cost": 0.0,
                "cost": 0.0,
                "partial": False,
                "unknown": False,
            },
        )

        cost, base_cost, cache_cost = _row_cost_split(row, prices)

        group["requests"] += _int_usage(row, "requests")
        group["input_tokens"] += _int_usage(row, "input_tokens")
        group["output_tokens"] += _int_usage(row, "output_tokens")
        group["cache_read_tokens"] += _int_usage(row, "cache_read_tokens")
        group["cache_creation_tokens"] += _int_usage(row, "cache_creation_tokens")
        group["web_search_requests"] += _int_usage(row, "web_search_requests")
        group["partial"] = group["partial"] or cost.partial
        group["unknown"] = group["unknown"] or cost.unknown_pricing
        if cost.total_cost is not None:
            group["base_cost"] += base_cost
            group["cache_cost"] += cache_cost
            group["cost"] += cost.total_cost

    return sorted(
        groups.values(),
        key=lambda item: (item["request_kind"], item["key_name"].lower(), item["proxy_key"]),
    )


def _session_sort_metric(group: dict) -> float:
    """Sort key for one aggregated session entry: total cost descending,
    falling back to summed token volume when the cost is unknown/None (e.g.
    an unpriced model was involved), so those sessions still rank
    meaningfully instead of collapsing to the same "0" cost."""
    cost = group.get("cost")
    if group.get("unknown") or cost is None:
        return float(
            _int_usage(group, "input_tokens")
            + _int_usage(group, "output_tokens")
            + _int_usage(group, "cache_read_tokens")
            + _int_usage(group, "cache_creation_tokens")
        )
    return float(cost)


def build_sessions_json(rows: list[dict], prices: dict) -> list[dict]:
    """Aggregate ``query_top_sessions`` rows (one per session_id/proxy_key/
    request_kind/provider/model) into one entry per ``(session_id, proxy_key)``,
    summing tokens/cost across models, merging first/last dates, carrying the
    project/title label, and bucketing per request_kind into ``kinds``. Re-sort
    by total cost descending (for ``/api/sessions``). Per-model rows are kept so
    ``_row_cost_split`` can price each model correctly."""
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        session_id = str(row.get("session_id") or "")
        proxy_key = str(row.get("proxy_key") or "")
        request_kind = str(row.get("request_kind") or "unknown")
        group_id = (session_id, proxy_key)
        first_date = row.get("first_date")
        last_date = row.get("last_date")

        group = groups.get(group_id)
        if group is None:
            group = {
                "session_id": session_id,
                "proxy_key": proxy_key,
                "key_name": str(row.get("key_name") or ""),
                "project": "",
                "title": "",
                "first_date": first_date,
                "last_date": last_date,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "web_search_requests": 0,
                "base_cost": 0.0,
                "cache_cost": 0.0,
                "cost": 0.0,
                "partial": False,
                "unknown": False,
                "kinds": {},
            }
            groups[group_id] = group
        else:
            if first_date is not None and (
                group["first_date"] is None or first_date < group["first_date"]
            ):
                group["first_date"] = first_date
            if last_date is not None and (
                group["last_date"] is None or last_date > group["last_date"]
            ):
                group["last_date"] = last_date

        # First non-empty label wins (the main row supplies it).
        proj = str(row.get("project") or "")
        if proj and not group["project"]:
            group["project"] = proj
        ttl = str(row.get("title") or "")
        if ttl and not group["title"]:
            group["title"] = ttl

        cost, base_cost, cache_cost = _row_cost_split(row, prices)

        # Top-level totals.
        group["requests"] += _int_usage(row, "requests")
        group["input_tokens"] += _int_usage(row, "input_tokens")
        group["output_tokens"] += _int_usage(row, "output_tokens")
        group["cache_read_tokens"] += _int_usage(row, "cache_read_tokens")
        group["cache_creation_tokens"] += _int_usage(row, "cache_creation_tokens")
        group["web_search_requests"] += _int_usage(row, "web_search_requests")
        group["partial"] = group["partial"] or cost.partial
        group["unknown"] = group["unknown"] or cost.unknown_pricing
        if cost.total_cost is not None:
            group["base_cost"] += base_cost
            group["cache_cost"] += cache_cost
            group["cost"] += cost.total_cost

        # Per-kind bucket.
        kind = group["kinds"].get(request_kind)
        if kind is None:
            kind = {
                "request_kind": request_kind,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "web_search_requests": 0,
                "base_cost": 0.0,
                "cache_cost": 0.0,
                "cost": 0.0,
                "partial": False,
                "unknown": False,
            }
            group["kinds"][request_kind] = kind
        kind["requests"] += _int_usage(row, "requests")
        kind["input_tokens"] += _int_usage(row, "input_tokens")
        kind["output_tokens"] += _int_usage(row, "output_tokens")
        kind["cache_read_tokens"] += _int_usage(row, "cache_read_tokens")
        kind["cache_creation_tokens"] += _int_usage(row, "cache_creation_tokens")
        kind["web_search_requests"] += _int_usage(row, "web_search_requests")
        kind["partial"] = kind["partial"] or cost.partial
        kind["unknown"] = kind["unknown"] or cost.unknown_pricing
        if cost.total_cost is not None:
            kind["base_cost"] += base_cost
            kind["cache_cost"] += cache_cost
            kind["cost"] += cost.total_cost

    result = []
    for group in groups.values():
        group["kinds"] = sorted(
            group["kinds"].values(), key=lambda k: k["cost"], reverse=True
        )
        result.append(group)
    return sorted(result, key=_session_sort_metric, reverse=True)


def _usage_cost_html(start: str, end: str, groups: list[dict], *, query_key: str = "") -> str:
    rows_html: list[str] = []
    for group in groups:
        total = _format_total_cost(
            float(group["known_cost"]),
            partial=bool(group["partial"]),
            unknown=bool(group["unknown"]),
        )
        group_cache_cost = float(group["cache_cost"])
        group_base_cost = float(group["base_cost"])
        if not group["unknown"] and group_cache_cost > 0:
            cost_cell = (
                f"<strong>{escape(total)}</strong>"
                f"<div class=\"cost-breakdown\">"
                f"tokens&nbsp;${group_base_cost:,.2f}"
                f" &middot; cache&nbsp;${group_cache_cost:,.2f}"
                f"</div>"
            )
        else:
            cost_cell = escape(total)
        rows_html.append(
            "<tr class=\"key-row\">"
            f"<td><strong>{escape(group['label'])}</strong>"
            f"<div class=\"muted\">{escape(_short_proxy_key(group['proxy_key']))}</div></td>"
            f"<td>{_format_int(group['requests'])}</td>"
            f"<td>{_format_int(group['input_tokens'])}</td>"
            f"<td>{_format_int(group['output_tokens'])}</td>"
            f"<td>{_format_int(group['cache_read_tokens'])}</td>"
            f"<td>{_format_int(group['cache_creation_tokens'])}</td>"
            f"<td>{_format_int(group['web_search_requests'])}</td>"
            f"<td class=\"cost\">{cost_cell}</td>"
            "</tr>"
        )
        for model in group["models"]:
            model_cost = _format_cost(
                model["cost"],
                partial=bool(model["partial"]),
                unknown=bool(model["unknown"]),
            )
            mdl_cache = model.get("cache_cost") or 0.0
            mdl_base = model.get("base_cost") or 0.0
            if not model["unknown"] and mdl_cache > 0:
                mdl_cost_cell = (
                    f"<strong>{escape(model_cost)}</strong>"
                    f"<div class=\"cost-breakdown\">"
                    f"tokens&nbsp;${mdl_base:,.2f}"
                    f" &middot; cache&nbsp;${mdl_cache:,.2f}"
                    f"</div>"
                )
            else:
                mdl_cost_cell = escape(model_cost)
            rows_html.append(
                "<tr class=\"model-row\">"
                f"<td><span class=\"muted\">{escape(model['provider'])}</span> / "
                f"{escape(model['model'])}</td>"
                f"<td>{_format_int(model['requests'])}</td>"
                f"<td>{_format_int(model['input_tokens'])}</td>"
                f"<td>{_format_int(model['output_tokens'])}</td>"
                f"<td>{_format_int(model['cache_read_tokens'])}</td>"
                f"<td>{_format_int(model['cache_creation_tokens'])}</td>"
                f"<td>{_format_int(model['web_search_requests'])}</td>"
                f"<td class=\"cost\">{mdl_cost_cell}</td>"
                "</tr>"
            )

    table_body = "\n".join(rows_html) if rows_html else (
        "<tr><td colspan=\"8\" class=\"empty\">No usage data for this period.</td></tr>"
    )
    key_input = (
        f'<input type="hidden" name="key" value="{escape(query_key, quote=True)}">'
        if query_key
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Usage Cost</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #111827; }}
    h1 {{ margin-bottom: 8px; }}
    form {{ display: flex; gap: 12px; align-items: end; margin: 24px 0; flex-wrap: wrap; }}
    label {{ display: grid; gap: 4px; font-size: 13px; color: #4b5563; }}
    input {{ padding: 8px 10px; border: 1px solid #d1d5db; border-radius: 6px; }}
    button {{ padding: 9px 14px; border: 0; border-radius: 6px; background: #111827; color: white; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
    th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 12px; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #f9fafb; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: #6b7280; }}
    .key-row td {{ background: #f3f4f6; }}
    .model-row td:first-child {{ padding-left: 32px; }}
    .muted {{ color: #6b7280; font-size: 12px; margin-top: 2px; }}
    .cost {{ font-variant-numeric: tabular-nums; font-weight: 600; }}
    .cost-breakdown {{ font-size: 11px; font-weight: 400; color: #6b7280; margin-top: 2px; white-space: nowrap; }}
    .empty {{ text-align: center; color: #6b7280; padding: 28px; }}
    .note {{ margin-top: 24px; max-width: 900px; color: #4b5563; line-height: 1.5; }}
  </style>
</head>
<body>
  <h1>Usage Cost</h1>
  <p class="muted">UTC dates. Showing usage from {escape(start)} through {escape(end)}.</p>
  <form method="get" action="/_usage">
    {key_input}
    <label>Start date
      <input type="date" name="start" value="{escape(start)}">
    </label>
    <label>End date
      <input type="date" name="end" value="{escape(end)}">
    </label>
    <button type="submit">Update</button>
  </form>
  <table>
    <thead>
      <tr>
        <th>Key / Provider / Model</th>
        <th>Requests</th>
        <th>Input</th>
        <th>Output</th>
        <th>Cache read</th>
        <th>Cache write</th>
        <th>Web search</th>
        <th>Cost</th>
      </tr>
    </thead>
    <tbody>
      {table_body}
    </tbody>
  </table>
  <section class="note">
    <h2>How cost is calculated</h2>
    <p>Costs are estimated from <code>model_prices</code> for each model prefix and the aggregated counters in <code>usage_daily</code>. Input, output, Anthropic cache reads/writes, and web search requests are included when pricing is available. A <code>?</code> means pricing is missing for that model; a <code>~</code> means part of the cache write split had to be estimated.</p>
  </section>
</body>
</html>"""


def make_usage_dashboard_handler(
    *,
    authorize: Callable[[web.Request], bool],
    get_db: Callable[[web.Request], Database | None],
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Build the ``GET /_usage`` handler.

    ``authorize`` decides whether the request may see the dashboard (e.g. a
    valid ``sp-*`` proxy token). ``get_db`` resolves the usage database for the
    request. Both are injected so the handler stays independent of any specific
    service's app layout.
    """

    async def _usage_cost_handler(request: web.Request) -> web.Response:
        if not authorize(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        date_range = _usage_date_range(request)
        if isinstance(date_range, web.Response):
            return date_range
        start, end = date_range

        db = get_db(request)
        if db is None:
            return web.json_response({"error": "usage database unavailable"}, status=500)

        rows = await db.query_usage_by_key_model(start, end)
        prices = build_price_lookup(await db.get_all_model_prices())
        groups = _build_usage_cost_groups(rows, prices)
        return web.Response(
            text=_usage_cost_html(start, end, groups, query_key=request.query.get("key", "")),
            content_type="text/html",
        )

    return _usage_cost_handler


def register_usage_dashboard(
    app: web.Application,
    *,
    authorize: Callable[[web.Request], bool],
    get_db: Callable[[web.Request], Database | None],
) -> None:
    """Register ``GET /_usage`` on ``app`` with injected auth and db access."""
    app.router.add_get(
        "/_usage",
        make_usage_dashboard_handler(authorize=authorize, get_db=get_db),
    )
