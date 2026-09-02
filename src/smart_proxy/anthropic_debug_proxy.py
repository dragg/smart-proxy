"""Transparent debug proxy for Anthropic API (separate from smart_proxy).

Listens on ANTHROPIC_DEBUG_PROXY_PORT (default 8090). Logs each request (headers
with masked Authorization) and forwards to ANTHROPIC_DEBUG_PROXY_UPSTREAM
(default https://api.anthropic.com) unchanged.

Set ANTHROPIC_DEBUG_PROXY_RAW_DIR to a directory to write **unredacted** request/
response bodies and headers to disk (secrets on disk — do not commit).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from aiohttp import web

from smart_proxy.oauth_token_inspect import analyze_bearer_value

logger = logging.getLogger("anthropic_debug_proxy")

DEFAULT_PORT = int(os.environ.get("ANTHROPIC_DEBUG_PROXY_PORT", "8090"))
DEFAULT_UPSTREAM = os.environ.get("ANTHROPIC_DEBUG_PROXY_UPSTREAM", "https://api.anthropic.com").rstrip("/")
RAW_CAPTURE_DIR = os.environ.get("ANTHROPIC_DEBUG_PROXY_RAW_DIR", "").strip()
MAX_CAPTURE_RESPONSE_BYTES = int(os.environ.get("ANTHROPIC_DEBUG_PROXY_CAPTURE_MAX_MB", "80")) * 1024 * 1024

_capture_seq = 0
_capture_lock = asyncio.Lock()

# Hop-by-hop / invalid to forward
_DROP_REQUEST_HEADERS = frozenset({
    "host", "connection", "content-length", "transfer-encoding", "te", "trailer",
    "proxy-connection", "keep-alive", "upgrade",
})
_DROP_RESPONSE_HEADERS = frozenset({
    "transfer-encoding", "connection", "content-length", "content-encoding",
})


def _mask(s: str, keep: int = 18) -> str:
    if len(s) <= keep:
        return "***"
    return s[:keep] + "…"


def _sanitize_headers_for_log(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers.items():
        kl = k.lower()
        if kl in ("authorization", "x-api-key"):
            out[k] = _mask(v)
        else:
            out[k] = v
    return out


def _body_preview(body: bytes, max_len: int = 8192) -> str:
    if not body:
        return ""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary {len(body)} bytes>"
    if len(text) > max_len:
        return text[:max_len] + f"\n… [truncated, total {len(body)} bytes]"
    return text


def _maybe_json_preview(body: bytes) -> str:
    if len(body) > 512 * 1024:
        return f"large body {len(body)} bytes (preview skipped)"
    raw = _body_preview(body, max_len=min(65536, len(body) + 1))
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        model = obj.get("model") if isinstance(obj, dict) else None
        stream = obj.get("stream") if isinstance(obj, dict) else None
        return f"json model={model!r} stream={stream!r} keys={list(obj.keys())[:12] if isinstance(obj, dict) else '?'}"
    except json.JSONDecodeError:
        return "non-json body"


def _forward_headers(request: web.Request) -> dict[str, str]:
    h: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _DROP_REQUEST_HEADERS:
            continue
        h[k] = v
    return h


def _incoming_headers_dict(request: web.Request) -> dict[str, str]:
    """All headers as sent by the client (before proxy strips hop-by-hop)."""
    return {k: v for k, v in request.headers.items()}


async def _next_capture_id() -> int:
    global _capture_seq
    async with _capture_lock:
        _capture_seq += 1
        return _capture_seq


def _write_raw_capture(
    seq: int,
    base: Path,
    *,
    method: str,
    path: str,
    query_string: str,
    incoming_headers: dict[str, str],
    request_body: bytes,
    upstream_url: str,
    response_status: int,
    response_headers: dict[str, str],
    response_body: bytes,
    response_truncated: bool,
) -> None:
    base.mkdir(parents=True, exist_ok=True)
    prefix = f"{seq:06d}"
    req_h_path = base / f"{prefix}_request_headers.json"
    req_b_path = base / f"{prefix}_request_body.bin"
    res_h_path = base / f"{prefix}_response_headers.json"
    res_b_path = base / f"{prefix}_response_body.bin"
    meta_path = base / f"{prefix}_meta.json"

    req_h_path.write_text(
        json.dumps(incoming_headers, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    req_b_path.write_bytes(request_body)

    res_h_path.write_text(
        json.dumps(response_headers, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    res_b_path.write_bytes(response_body)

    meta = {
        "seq": seq,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": method,
        "path": path,
        "query_string": query_string,
        "upstream_url": upstream_url,
        "request_headers_file": req_h_path.name,
        "request_body_file": req_b_path.name,
        "request_body_bytes": len(request_body),
        "response_status": response_status,
        "response_headers_file": res_h_path.name,
        "response_body_file": res_b_path.name,
        "response_body_bytes": len(response_body),
        "response_truncated": response_truncated,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


async def _proxy_handler(request: web.Request) -> web.StreamResponse:
    body = await request.read()
    path = request.path
    if request.query_string:
        url = f"{DEFAULT_UPSTREAM}{path}?{request.query_string}"
    else:
        url = f"{DEFAULT_UPSTREAM}{path}"

    fwd = _forward_headers(request)
    log_hdrs = _sanitize_headers_for_log(fwd)

    auth = None
    for _k, _v in fwd.items():
        if _k.lower() == "authorization":
            auth = _v
            break
    if auth:
        logger.info("auth analysis: %s", analyze_bearer_value(auth))

    preview = _maybe_json_preview(body)
    logger.info(
        ">>> %s %s  bytes=%d  %s",
        request.method,
        path,
        len(body),
        preview,
    )
    logger.info(">>> headers: %s", log_hdrs)

    timeout = httpx.Timeout(600.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout, http2=False) as client:
        req = client.build_request(
            request.method,
            url,
            headers=fwd,
            content=body if body else None,
        )
        r = await client.send(req, stream=True)

        stream_resp = web.StreamResponse(status=r.status_code)
        for name, value in r.headers.items():
            if name.lower() in _DROP_RESPONSE_HEADERS:
                continue
            stream_resp.headers[name] = value

        response_headers_out: dict[str, str] = {
            k: v for k, v in r.headers.items()
        }
        resp_buf = bytearray()
        truncated = False

        await stream_resp.prepare(request)
        try:
            try:
                async for chunk in r.aiter_bytes():
                    if len(resp_buf) + len(chunk) <= MAX_CAPTURE_RESPONSE_BYTES:
                        resp_buf.extend(chunk)
                    else:
                        if not truncated:
                            truncated = True
                            logger.warning(
                                "response capture truncated at %d MB for %s %s",
                                MAX_CAPTURE_RESPONSE_BYTES // (1024 * 1024),
                                request.method,
                                path,
                            )
                        remain = MAX_CAPTURE_RESPONSE_BYTES - len(resp_buf)
                        if remain > 0:
                            resp_buf.extend(chunk[:remain])
                    await stream_resp.write(chunk)
            except httpx.ReadError as exc:
                logger.warning(
                    "upstream read error while streaming %s %s: %s",
                    request.method,
                    path,
                    exc,
                )
        finally:
            await r.aclose()
        await stream_resp.write_eof()

    if RAW_CAPTURE_DIR:
        cap_base = Path(RAW_CAPTURE_DIR).expanduser().resolve()
        try:
            incoming = _incoming_headers_dict(request)
            seq = await request.app["capture_seq_get"]()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: _write_raw_capture(
                    seq,
                    cap_base,
                    method=request.method,
                    path=path,
                    query_string=request.query_string,
                    incoming_headers=incoming,
                    request_body=body,
                    upstream_url=url,
                    response_status=r.status_code,
                    response_headers=response_headers_out,
                    response_body=bytes(resp_buf),
                    response_truncated=truncated,
                ),
            )
            logger.info("raw capture #%d written under %s", seq, cap_base)
        except Exception as exc:
            logger.exception("raw capture failed: %s", exc)

    logger.info(
        "<<< %s %s status=%s",
        request.method,
        path,
        r.status_code,
    )
    return stream_resp


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok\n", content_type="text/plain")


def create_app() -> web.Application:
    app = web.Application(client_max_size=100 * 1024 * 1024)

    async def _seq() -> int:
        return await _next_capture_id()

    app["capture_seq_get"] = _seq
    app.router.add_get("/health", _health)
    app.router.add_route("*", "/{path:.*}", _proxy_handler)
    return app


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", DEFAULT_PORT)
    await site.start()
    logger.info(
        "Anthropic debug proxy listening on 0.0.0.0:%s → %s",
        DEFAULT_PORT,
        DEFAULT_UPSTREAM,
    )
    if RAW_CAPTURE_DIR:
        p = Path(RAW_CAPTURE_DIR).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        logger.warning(
            "RAW capture ON (unredacted secrets on disk): %s  (max response capture %d MB)",
            p,
            MAX_CAPTURE_RESPONSE_BYTES // (1024 * 1024),
        )
    await asyncio.Event().wait()


def main_sync() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main_sync()
