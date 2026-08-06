"""Loopback-only compatibility gateway for the stock T300 serial UI bridge."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
from typing import Any, Iterable

from .touchscreen_policy import (
    TouchscreenPolicyError,
    error_response,
    review_request,
    success_response,
    translate_response,
)


LOG = logging.getLogger("t300-touchscreen-gateway")

QUIET_LOCAL_NOOPS = {
    "The legacy bridge may not disable runout protection.",
    "Runout protection is already forced on by the candidate.",
}


def local_noop_log_level(reason: str) -> int:
    """Keep expected vendor polling from consuming the bounded journal."""
    return logging.DEBUG if reason in QUIET_LOCAL_NOOPS else logging.INFO


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def decode_bridge_message(message: str) -> dict[str, Any]:
    """Decode one bridge message while rejecting duplicate object keys."""
    parsed = json.loads(message, object_pairs_hook=_unique_object)
    if not isinstance(parsed, dict):
        raise ValueError("touchscreen request must be one object")
    return parsed


def _loopback_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("address must be one IP literal") from exc
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("touchscreen gateway addresses must be loopback")
    return str(address)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", type=_loopback_address, default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=7125)
    parser.add_argument("--upstream-host", type=_loopback_address, default="127.0.0.1")
    parser.add_argument("--upstream-port", type=int, default=7126)
    return parser


def _valid_port(value: int, label: str) -> int:
    if isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValueError("%s must be between 1 and 65535" % label)
    return value


async def serve(
    listen_host: str,
    listen_port: int,
    upstream_host: str,
    upstream_port: int,
) -> None:
    # Tornado is supplied by the exact pinned Moonraker environment on the
    # candidate. Keeping the import here lets policy tests run without adding
    # an unrelated host dependency.
    import tornado.httpclient
    import tornado.ioloop
    import tornado.web
    import tornado.websocket

    upstream_url = "ws://%s:%d/websocket" % (upstream_host, upstream_port)

    class HealthHandler(tornado.web.RequestHandler):
        def get(self) -> None:
            self.set_header("Content-Type", "application/json")
            self.finish({"service": "t300-touchscreen-gateway", "status": "ready"})

    class RejectHttpHandler(tornado.web.RequestHandler):
        def prepare(self) -> None:
            self.set_status(404)
            self.finish(
                {
                    "error": "The physical touchscreen may use only its reviewed WebSocket contract."
                }
            )

    class BridgeWebSocket(tornado.websocket.WebSocketHandler):
        clients = 0

        def initialize(self) -> None:
            self.upstream = None
            self.reader_task: asyncio.Task[Any] | None = None
            self.counted_client = False
            self.startup_restart_suppressed = False

        def check_origin(self, origin: str) -> bool:
            return not origin or origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")

        async def open(self) -> None:
            if BridgeWebSocket.clients:
                self.close(code=1013, reason="one physical touchscreen client is supported")
                return
            BridgeWebSocket.clients += 1
            self.counted_client = True
            try:
                request = tornado.httpclient.HTTPRequest(
                    upstream_url,
                    connect_timeout=5,
                    request_timeout=0,
                    headers={"Origin": "http://127.0.0.1"},
                )
                self.upstream = await tornado.websocket.websocket_connect(
                    request,
                    ping_interval=30,
                    ping_timeout=10,
                    max_message_size=1024 * 1024,
                )
            except Exception as exc:
                LOG.error("Moonraker WebSocket connection failed: %s", exc)
                self.close(code=1011, reason="Moonraker is unavailable")
                return
            self.reader_task = asyncio.create_task(self._read_upstream())
            LOG.info("physical touchscreen bridge connected")

        async def _read_upstream(self) -> None:
            try:
                while self.upstream is not None:
                    message = await self.upstream.read_message()
                    if message is None:
                        break
                    if isinstance(message, bytes):
                        LOG.warning("discarded unexpected binary Moonraker message")
                        continue
                    try:
                        parsed = json.loads(message)
                    except json.JSONDecodeError:
                        LOG.warning("discarded malformed Moonraker JSON")
                        continue
                    if not isinstance(parsed, dict):
                        LOG.warning("discarded non-object Moonraker JSON")
                        continue
                    await self.write_message(
                        json.dumps(translate_response(parsed), separators=(",", ":"))
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.error("Moonraker WebSocket reader failed: %s", exc)
            finally:
                if self.ws_connection is not None:
                    self.close(code=1011, reason="Moonraker connection closed")

        async def on_message(self, message: str | bytes) -> None:
            if isinstance(message, bytes):
                self.close(code=1003, reason="binary bridge messages are unsupported")
                return
            try:
                request = decode_bridge_message(message)
            except (json.JSONDecodeError, ValueError):
                self.close(code=1007, reason="malformed touchscreen JSON")
                return
            try:
                decision = review_request(
                    request,
                    allow_explicit_restart=self.startup_restart_suppressed,
                )
            except TouchscreenPolicyError as exc:
                response = error_response(request, str(exc))
                if response is not None:
                    await self.write_message(json.dumps(response, separators=(",", ":")))
                LOG.warning("refused malformed touchscreen action: %s", exc)
                return
            if (
                decision.outcome == "emulate_success"
                and decision.reason
                == "The stock bridge startup restart is intentionally suppressed."
            ):
                self.startup_restart_suppressed = True
            if decision.outcome == "emulate_success":
                response = success_response(request)
                if response is not None:
                    await self.write_message(json.dumps(response, separators=(",", ":")))
                LOG.log(
                    local_noop_log_level(decision.reason),
                    "acknowledged local touchscreen no-op: %s",
                    decision.reason,
                )
                return
            if decision.outcome == "reject":
                response = error_response(request, decision.reason)
                if response is not None:
                    await self.write_message(json.dumps(response, separators=(",", ":")))
                LOG.warning("refused touchscreen action: %s", decision.reason)
                return
            if self.upstream is None:
                response = error_response(request, "Moonraker is unavailable.")
                if response is not None:
                    await self.write_message(json.dumps(response, separators=(",", ":")))
                return
            await self.upstream.write_message(
                json.dumps(decision.request, separators=(",", ":"))
            )

        def on_close(self) -> None:
            if self.counted_client:
                BridgeWebSocket.clients -= 1
                self.counted_client = False
            if self.reader_task is not None:
                self.reader_task.cancel()
            if self.upstream is not None:
                self.upstream.close()
            LOG.info("physical touchscreen bridge disconnected")

    application = tornado.web.Application(
        [
            (r"/healthz", HealthHandler),
            (r"/websocket", BridgeWebSocket),
            (r"/.*", RejectHttpHandler),
        ],
        compress_response=False,
        debug=False,
        websocket_max_message_size=1024 * 1024,
    )
    application.listen(listen_port, address=listen_host, xheaders=False)
    LOG.info(
        "listening on %s:%d for Moonraker %s:%d",
        listen_host,
        listen_port,
        upstream_host,
        upstream_port,
    )
    await asyncio.Event().wait()


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        listen_port = _valid_port(args.listen_port, "listen port")
        upstream_port = _valid_port(args.upstream_port, "upstream port")
        if args.listen_host == args.upstream_host and listen_port == upstream_port:
            raise ValueError("gateway and Moonraker cannot use the same endpoint")
    except ValueError as exc:
        build_parser().error(str(exc))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(
        serve(args.listen_host, listen_port, args.upstream_host, upstream_port)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
