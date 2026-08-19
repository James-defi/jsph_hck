"""Small, testable client for the public Tutu Streamable HTTP MCP server.

The module intentionally exposes two layers:

* :class:`StreamableHttpMcpClient` knows JSON-RPC, initialization and SSE.
* :class:`TutuMcpGateway` knows the names of the Tutu tools and is easy to
  replace with a fake in tests.

It does not create bookings.  ``create_checkout_link`` is only a handoff to
Tutu and callers must enforce an explicit fare selection before using it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading
from typing import Any, Callable, Mapping, MutableMapping, Protocol, Sequence

try:  # Keep imports lazy-friendly for pure unit tests.
    import requests
except ImportError:  # pragma: no cover - exercised only in a broken runtime
    requests = None  # type: ignore[assignment]


JSON = dict[str, Any]


class TutuMcpError(RuntimeError):
    """Base class for an MCP failure that can be shown honestly to a user."""


class TutuMcpTransportError(TutuMcpError):
    """The remote MCP endpoint could not be reached or returned bad HTTP."""


class TutuMcpSessionExpired(TutuMcpTransportError):
    """A Streamable HTTP session id expired and one reinitialization is safe."""


class TutuMcpProtocolError(TutuMcpError):
    """The endpoint responded, but not with a usable MCP JSON-RPC response."""


class TutuMcpToolError(TutuMcpError):
    """A tool completed with an MCP-level error."""


class ToolCaller(Protocol):
    """The minimal interface needed by :class:`TutuMcpGateway`.

    A fake only needs to implement this method, which keeps all unit tests
    isolated from the public MCP endpoint.
    """

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> JSON:
        ...


def _as_json_object(value: Any, *, context: str) -> JSON:
    if isinstance(value, Mapping):
        return dict(value)
    raise TutuMcpProtocolError(f"{context} must be a JSON object")


def _json_rpc_messages(value: Any, *, context: str) -> list[JSON]:
    """Normalize one JSON-RPC response or a JSON-RPC batch to objects."""

    if isinstance(value, Mapping):
        return [dict(value)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        messages = [dict(item) for item in value if isinstance(item, Mapping)]
        if len(messages) != len(value):
            raise TutuMcpProtocolError(f"{context} batch contains a non-object message")
        return messages
    raise TutuMcpProtocolError(f"{context} must be a JSON-RPC object or batch")


def _decode_sse_or_json_messages(text: str) -> list[JSON]:
    """Decode all normal JSON, batch, and SSE JSON-RPC messages in a body.

    Streamable HTTP can emit progress notifications before the response to our
    request.  The caller must therefore select by JSON-RPC id rather than
    treating the first SSE event as its result.
    """

    stripped = text.strip()
    if not stripped:
        raise TutuMcpProtocolError("MCP response body is empty")
    try:
        return _json_rpc_messages(json.loads(stripped), context="MCP response")
    except json.JSONDecodeError:
        pass

    messages: list[JSON] = []
    data_lines: list[str] = []

    def flush_event() -> None:
        if not data_lines:
            return
        candidate = "\n".join(data_lines)
        data_lines.clear()
        try:
            messages.extend(_json_rpc_messages(json.loads(candidate), context="MCP SSE event"))
        except json.JSONDecodeError as exc:
            raise TutuMcpProtocolError("MCP SSE event is not JSON") from exc

    for raw_line in text.splitlines():
        # Do not strip the payload itself: SSE's ``data:`` may intentionally
        # include a leading space that is part of a multi-line JSON string.
        if raw_line == "" or raw_line == "\r":
            flush_event()
            continue
        line = raw_line.rstrip("\r")
        if line.startswith("data:"):
            payload = line[5:]
            data_lines.append(payload[1:] if payload.startswith(" ") else payload)
        # Ignore event/id/retry/comment lines; they are transport metadata.
    flush_event()
    if not messages:
        raise TutuMcpProtocolError("MCP response is neither JSON nor an SSE JSON-RPC event")
    return messages


def _decode_sse_or_json(text: str) -> JSON:
    """Compatibility helper returning the first decoded message.

    Internal request handling uses :func:`_decode_sse_or_json_messages` and
    correlates the response by id.  Keeping this helper makes simple callers
    and older tests readable without weakening the actual client path.
    """

    return _decode_sse_or_json_messages(text)[0]


def decode_tool_result(result: Mapping[str, Any]) -> JSON:
    """Return a JSON-compatible payload from an MCP ``tools/call`` result.

    MCP servers may expose data as ``structuredContent`` or as a JSON string in
    one of the text content blocks.  The raw wrapper is deliberately removed
    here: product code should consume the same dict in live and fake runs.
    """

    if result.get("isError"):
        detail = result.get("content") or result.get("structuredContent") or result
        raise TutuMcpToolError(f"Tutu MCP tool returned an error: {detail}")

    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)

    content = result.get("content")
    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        # A few compatible servers directly return their payload as ``result``.
        direct = {key: value for key, value in result.items() if key not in {"content", "isError"}}
        if direct:
            return direct
        raise TutuMcpProtocolError("MCP tool result has no structured content")

    text_blocks: list[str] = []
    non_text_blocks: list[Mapping[str, Any]] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text_blocks.append(block["text"])
        else:
            non_text_blocks.append(block)

    # Tutu returns a single JSON text block.  Supporting several blocks costs
    # little and produces a useful diagnostics payload for non-JSON content.
    for text in text_blocks:
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, Mapping):
            return dict(decoded)

    if len(text_blocks) == 1:
        return {"text": text_blocks[0], "content": [dict(item) for item in non_text_blocks]}
    if text_blocks or non_text_blocks:
        return {
            "text": "\n".join(text_blocks),
            "content": [dict(item) for item in non_text_blocks],
        }
    raise TutuMcpProtocolError("MCP tool result contains no readable content")


class StreamableHttpMcpClient:
    """Synchronous JSON-RPC client for a remote Streamable HTTP MCP server.

    The public Tutu endpoint needs no authorization.  ``requests`` is injected
    as a session when desired, so tests can use a tiny recording fake instead
    of monkeypatching network calls.
    """

    DEFAULT_ENDPOINT = "https://mcp.tutu.ru/mcp"
    PROTOCOL_VERSION = "2025-03-26"

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        timeout_seconds: float = 20.0,
        session: Any | None = None,
        client_name: str = "speakfare-groupsync",
        client_version: str = "0.1.0",
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.client_name = client_name
        self.client_version = client_version
        if session is None:
            if requests is None:
                raise TutuMcpTransportError("Install requests to use the live Tutu MCP client")
            session = requests.Session()
        self._session = session
        self._session_id: str | None = None
        self._initialized = False
        self._next_id = 1
        self._lock = threading.RLock()

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def initialize(self) -> JSON:
        """Perform the MCP handshake exactly once and return server metadata."""

        with self._lock:
            if self._initialized:
                return {
                    "already_initialized": True,
                    "session_id": self._session_id,
                }

            response = self._rpc(
                "initialize",
                {
                    "protocolVersion": self.PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                },
                include_session=False,
            )
            # The server may issue a session id in headers even for a JSON body.
            self._session_id = response.pop("__mcp_session_id", None)
            result = self._extract_rpc_result(response)
            self._initialized = True
            self._notify_initialized()
            return result

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> JSON:
        """Call one MCP tool and decode its product payload to a dictionary."""

        if not isinstance(name, str) or not name:
            raise ValueError("tool name must be a non-empty string")
        with self._lock:
            if not self._initialized:
                self.initialize()
            params = {"name": name, "arguments": dict(arguments or {})}
            try:
                response = self._rpc("tools/call", params)
            except TutuMcpSessionExpired:
                # Streamable HTTP defines a 404 for an unknown/expired
                # Mcp-Session-Id.  Reinitialize and retry the same idempotent
                # read/checkout-link creation call exactly once.
                self._initialized = False
                self._session_id = None
                self.initialize()
                response = self._rpc("tools/call", params)
            response_session_id = response.pop("__mcp_session_id", None)
            if response_session_id:
                self._session_id = response_session_id
            result = self._extract_rpc_result(response)
            return decode_tool_result(result)

    def _notify_initialized(self) -> None:
        # The initialization notification intentionally has no id and no result.
        try:
            self._post_json(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
                include_session=True,
                expect_response=False,
            )
        except TutuMcpTransportError:
            # Some gateways do not send a response to notifications.  A failure
            # here should not discard a successfully initialized session.
            return

    def _rpc(self, method: str, params: Mapping[str, Any], *, include_session: bool = True) -> JSON:
        request_id = self._next_id
        self._next_id += 1
        received = self._post_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
            include_session=include_session,
            expect_response=True,
        )
        if received is None:
            raise TutuMcpProtocolError(f"MCP {method} unexpectedly returned no response")
        messages, session_id = received
        response = next((message for message in messages if message.get("id") == request_id), None)
        if response is None:
            raise TutuMcpProtocolError(f"MCP {method} returned no JSON-RPC response for id {request_id}")
        if session_id:
            response = dict(response)
            response["__mcp_session_id"] = session_id
        return response

    def _post_json(
        self,
        payload: Mapping[str, Any],
        *,
        include_session: bool,
        expect_response: bool,
    ) -> tuple[list[JSON], str | None] | None:
        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if include_session and self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            response = self._session.post(
                self.endpoint,
                json=dict(payload),
                headers=headers,
                timeout=self.timeout_seconds,
            )
        except Exception as exc:  # ``requests`` and fake sessions differ here.
            raise TutuMcpTransportError(f"Unable to reach Tutu MCP: {exc}") from exc

        status_code = getattr(response, "status_code", None)
        if status_code is not None and not (200 <= int(status_code) < 300):
            body = getattr(response, "text", "")
            if int(status_code) == 404 and include_session and self._session_id:
                raise TutuMcpSessionExpired("Tutu MCP session expired")
            raise TutuMcpTransportError(f"Tutu MCP HTTP {status_code}: {body[:500]}")

        if not expect_response:
            return None

        text = getattr(response, "text", "")
        decoded = _decode_sse_or_json_messages(text)
        headers_object = getattr(response, "headers", {}) or {}
        session_id = headers_object.get("Mcp-Session-Id") or headers_object.get("mcp-session-id")
        return decoded, str(session_id) if session_id else None

    @staticmethod
    def _extract_rpc_result(response: Mapping[str, Any]) -> JSON:
        if "error" in response:
            raise TutuMcpToolError(f"MCP JSON-RPC error: {response['error']}")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise TutuMcpProtocolError("MCP response has no object result")
        return dict(result)


DOMAIN_INSTRUCTION_TOOLS: dict[str, str] = {
    "avia": "get_avia_instructions",
    "rail": "get_rail_instructions",
    "bus": "get_bus_instructions",
    "etrain": "get_etrain_instructions",
    "hotels": "get_hotels_instructions",
    "multitransport": "get_multitransport_instructions",
}

SEARCH_TOOLS: dict[str, str] = {
    "avia": "search_avia",
    "rail": "search_rail",
    "bus": "search_bus",
    "etrain": "search_etrain",
    "hotels": "search_hotels",
    "multitransport": "search_multitransport",
}

# Live Tutu pydantic models use extra="forbid".  Callers (especially the LLM)
# still send aliases and shapes that the schema rejects; normalize here so
# Streamable HTTP never sees those payloads.  ``create_checkout_link`` is NOT
# exempt: search offers carry more keys in ``checkout_ref`` than the checkout
# tool accepts, so its flattened ref is filtered to the live schema below.
_LOCATION_SEARCH_TOOLS = frozenset(
    {"search_avia", "search_rail", "search_bus", "search_etrain", "search_multitransport"}
)
_PASSENGERS_INT_TOOLS = frozenset({"search_rail", "search_etrain"})
_ADULTS_INT_TOOLS = frozenset({"search_avia", "search_bus", "search_hotels", "search_multitransport"})
_CHILD_INFANT_INT_TOOLS = frozenset({"search_avia", "search_multitransport"})
_ALWAYS_DROP_KEYS = frozenset({"query"})
_ORIGIN_ALIASES = ("from", "from_city")
_DESTINATION_ALIASES = ("to", "to_city")

_COMMON_SEARCH_FILTERS = frozenset(
    {"price_max", "direct_only", "carriers", "page", "page_size", "sort", "view"}
)
_TRANSPORT_ROUTE_KEYS = frozenset({"origin", "destination", "departure_date", "return_date"})

TOOL_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "search_avia": _COMMON_SEARCH_FILTERS
    | _TRANSPORT_ROUTE_KEYS
    | {"adults", "children", "infants", "service_class", "flight_numbers"},
    # Live Tutu ``search_rail`` forbids ``adults`` (extra_forbidden).  Callers
    # may still pass adults; ``_apply_passenger_shapes`` copies it to
    # ``passengers`` and this allow-list drops the alias.
    "search_rail": _COMMON_SEARCH_FILTERS | _TRANSPORT_ROUTE_KEYS | {"passengers"},
    "search_bus": _COMMON_SEARCH_FILTERS | _TRANSPORT_ROUTE_KEYS | {"adults"},
    "search_etrain": _COMMON_SEARCH_FILTERS | _TRANSPORT_ROUTE_KEYS | {"passengers"},
    "search_multitransport": frozenset(
        {"price_max", "direct_only", "carriers", "page", "page_size", "view", "optimize_for"}
    )
    | _TRANSPORT_ROUTE_KEYS
    | {"adults", "children", "infants"},
    "search_hotels": frozenset(
        {
            "city_name",
            "geo_id",
            "check_in",
            "check_out",
            "adults",
            "children_ages",
            "stars",
            "price_max",
            "meals",
            "hotel_types",
            "min_rating",
            "free_cancellation",
            "breakfast_included",
            "hotel_amenities",
            "room_amenities",
            "page",
            "page_size",
            "sort",
            "view",
        }
    ),
    "get_offer_details": frozenset({"product_type", "details_ref"}),
    "fetch_resource": frozenset({"uri"}),
    # Verified against the live ``tools/list`` schema on 2026-08-19.  Search
    # offers emit a superset of these keys inside ``checkout_ref`` (e.g.
    # ``is_multi_pnr``), and the tool rejects them with extra_forbidden.
    # Values are forwarded byte-for-byte; only the key set is constrained.
    "create_checkout_link": frozenset(
        {
            "product_type",
            "transport",
            "search_results_url",
            "departure_geo_city_id",
            "arrival_geo_city_id",
            "service_class",
            "passengers_full",
            "passengers_child",
            "passengers_infant",
            "departure_avia_id",
            "arrival_avia_id",
            "passengers_adult",
            "is_round_trip",
            "return_departure_at",
            "offer_hash",
            "departure_city_id",
            "arrival_city_id",
            "departure_station_code",
            "arrival_station_code",
            "departure_etrain_id",
            "arrival_etrain_id",
            "train_number",
            "city_from",
            "city_to",
            "departure_id",
            "arrival_id",
            "departure_stop_id",
            "arrival_stop_id",
            "departure_stop_name",
            "arrival_stop_name",
            "passengers",
            "departure_geo_point_id",
            "arrival_geo_point_id",
            "segment_hash",
            "car_number",
            "seat_numbers",
            "fare_type",
            "gender_type",
            "search_id",
            "result_id",
            "card_id",
            "seat_count",
            "hotel_alias",
            "offer_pack_hash",
            "hotel_geo_id",
            "check_in",
            "check_out",
            "adults",
            "children_ages",
            "fallback_url",
            "departure_at",
        }
    ),
}
for _instruction_tool in DOMAIN_INSTRUCTION_TOOLS.values():
    TOOL_ALLOWED_KEYS[_instruction_tool] = frozenset()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _passenger_breakdown(value: Any) -> tuple[int | None, int | None, int | None]:
    if isinstance(value, Mapping):
        return (
            _first_int(
                value.get("adults"),
                value.get("passengers_full"),
                value.get("adult_count"),
                value.get("passengers"),
            ),
            _first_int(value.get("children"), value.get("passengers_child"), value.get("child_count")),
            _first_int(value.get("infants"), value.get("passengers_infant"), value.get("infant_count")),
        )
    count = _as_int(value)
    return count, None, None


def _fill_if_empty(arguments: JSON, key: str, value: Any) -> None:
    current = arguments.get(key)
    if current is None or current == "":
        arguments[key] = value


def _apply_location_aliases(arguments: JSON) -> None:
    if arguments.get("origin") in (None, ""):
        for alias in _ORIGIN_ALIASES:
            candidate = arguments.get(alias)
            if candidate not in (None, ""):
                arguments["origin"] = candidate
                break
    if arguments.get("destination") in (None, ""):
        for alias in _DESTINATION_ALIASES:
            candidate = arguments.get(alias)
            if candidate not in (None, ""):
                arguments["destination"] = candidate
                break
    for alias in _ORIGIN_ALIASES + _DESTINATION_ALIASES:
        arguments.pop(alias, None)


def _apply_passenger_shapes(tool_name: str, arguments: JSON) -> None:
    passengers_raw = arguments.get("passengers")
    adults_raw = arguments.get("adults")
    from_passengers = _passenger_breakdown(passengers_raw) if passengers_raw is not None else (None, None, None)
    if isinstance(adults_raw, Mapping):
        from_adults = _passenger_breakdown(adults_raw)
    else:
        from_adults = (_as_int(adults_raw), None, None)

    adults = from_adults[0] if from_adults[0] is not None else from_passengers[0]
    children = _first_int(arguments.get("children"), from_adults[1], from_passengers[1])
    infants = _first_int(arguments.get("infants"), from_adults[2], from_passengers[2])

    if _as_int(arguments.get("passengers")) is None:
        arguments.pop("passengers", None)
    if isinstance(arguments.get("adults"), Mapping):
        arguments.pop("adults", None)

    if tool_name in _PASSENGERS_INT_TOOLS:
        if _as_int(arguments.get("passengers")) is None and adults is not None:
            arguments["passengers"] = adults
        arguments.pop("adults", None)
        arguments.pop("children", None)
        arguments.pop("infants", None)
        return

    if tool_name in _ADULTS_INT_TOOLS:
        arguments.pop("passengers", None)
        if _as_int(arguments.get("adults")) is None and adults is not None:
            arguments["adults"] = adults
        if tool_name in _CHILD_INFANT_INT_TOOLS:
            if _as_int(arguments.get("children")) is None and children is not None:
                arguments["children"] = children
            if _as_int(arguments.get("infants")) is None and infants is not None:
                arguments["infants"] = infants
        else:
            arguments.pop("children", None)
            arguments.pop("infants", None)


def _has_required_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    if isinstance(value, Mapping) and not value:
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and not value:
        return False
    return True


def _require_offer_details(arguments: JSON) -> None:
    missing: list[str] = []
    if not _has_required_value(arguments.get("product_type")):
        missing.append("product_type")
    if not _has_required_value(arguments.get("details_ref")):
        missing.append("details_ref")
    if missing:
        raise ValueError("get_offer_details requires " + " and ".join(missing))


def normalize_tutu_tool_arguments(tool_name: str, arguments: Mapping[str, Any] | None = None) -> JSON:
    """Coerce caller arguments to shapes live Tutu MCP accepts.

    Unknown extra keys are dropped for tools whose live schema is extra-forbidden.
    """

    payload: JSON = dict(arguments or {})
    if tool_name == "create_checkout_link":
        nested = payload.get("checkout_ref")
        if isinstance(nested, Mapping):
            flattened = dict(nested)
            for key, value in payload.items():
                if key != "checkout_ref":
                    flattened[key] = value
            payload = flattened
    if tool_name in _LOCATION_SEARCH_TOOLS:
        _apply_location_aliases(payload)
    if tool_name == "search_hotels":
        if payload.get("check_in") in (None, ""):
            _fill_if_empty(payload, "check_in", payload.get("checkin_date"))
        if payload.get("check_out") in (None, ""):
            _fill_if_empty(payload, "check_out", payload.get("checkout_date"))
        payload.pop("checkin_date", None)
        payload.pop("checkout_date", None)

    if tool_name != "create_checkout_link":
        _apply_passenger_shapes(tool_name, payload)
    for key in _ALWAYS_DROP_KEYS:
        payload.pop(key, None)

    allowed = TOOL_ALLOWED_KEYS.get(tool_name)
    if allowed is not None:
        payload = {key: value for key, value in payload.items() if key in allowed}

    payload = {key: value for key, value in payload.items() if value is not None}
    if tool_name == "get_offer_details":
        _require_offer_details(payload)
    return payload


def _arguments(arguments: Mapping[str, Any] | None, kwargs: Mapping[str, Any]) -> JSON:
    merged: JSON = dict(arguments or {})
    for key, value in kwargs.items():
        if value is not None:
            merged[key] = value
    return merged


class TutuMcpGateway:
    """Tutu domain wrappers on top of a live client or a fake ``ToolCaller``."""

    def __init__(self, client: ToolCaller | None = None) -> None:
        self.client: ToolCaller = client or StreamableHttpMcpClient()

    def call(self, tool_name: str, arguments: Mapping[str, Any] | None = None) -> JSON:
        return self.client.call_tool(tool_name, normalize_tutu_tool_arguments(tool_name, arguments))

    def get_domain_instructions(self, domain: str) -> JSON:
        try:
            tool_name = DOMAIN_INSTRUCTION_TOOLS[domain]
        except KeyError as exc:
            allowed = ", ".join(sorted(DOMAIN_INSTRUCTION_TOOLS))
            raise ValueError(f"Unknown Tutu domain {domain!r}; expected one of {allowed}") from exc
        return self.call(tool_name)

    def search(self, domain: str, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        try:
            tool_name = SEARCH_TOOLS[domain]
        except KeyError as exc:
            allowed = ", ".join(sorted(SEARCH_TOOLS))
            raise ValueError(f"Unknown Tutu search domain {domain!r}; expected one of {allowed}") from exc
        return self.call(tool_name, _arguments(arguments, kwargs))

    def search_avia(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        return self.search("avia", arguments, **kwargs)

    def search_rail(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        return self.search("rail", arguments, **kwargs)

    def search_bus(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        return self.search("bus", arguments, **kwargs)

    def search_etrain(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        return self.search("etrain", arguments, **kwargs)

    def search_hotels(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        return self.search("hotels", arguments, **kwargs)

    def search_multitransport(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        return self.search("multitransport", arguments, **kwargs)

    def get_offer_details(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        return self.call("get_offer_details", _arguments(arguments, kwargs))

    def get_rail_seatmap(self, arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        return self.call("get_rail_seatmap", _arguments(arguments, kwargs))

    def create_checkout_link(self, checkout_ref: Mapping[str, Any] | None = None, **kwargs: Any) -> JSON:
        """Forward the stored checkout-ref fields in Tutu's live tool schema.

        Tutu returns an opaque ref *inside* a search offer, but its
        ``create_checkout_link`` tool expects the ref's fields at the top
        level, restricted to its own schema keys.  Values are preserved
        exactly; only keys outside the live schema are dropped by
        ``normalize_tutu_tool_arguments``.  Do not invent a URL or accept
        ad-hoc overrides from a caller.
        """

        if checkout_ref is None:
            raise ValueError("create_checkout_link requires the exact checkout_ref from a Tutu offer")
        if not isinstance(checkout_ref, Mapping):
            raise TypeError("checkout_ref must be a mapping from a Tutu offer")
        if kwargs:
            raise ValueError("create_checkout_link accepts only the stored checkout_ref")
        return self.call("create_checkout_link", dict(checkout_ref))

    def fetch_resource(self, uri: str) -> JSON:
        if not uri:
            raise ValueError("resource URI must not be empty")
        return self.call("fetch_resource", {"uri": uri})

    def get_search_health(self) -> JSON:
        return self.fetch_resource("tutu://status")


@dataclass
class FakeTutuMcpClient:
    """A deterministic fake for unit tests and local agent-loop tests.

    ``responses`` maps a tool name either to a JSON payload, a list of payloads
    consumed in order, or a function receiving ``(name, arguments)``.
    """

    responses: MutableMapping[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, JSON]] = field(default_factory=list)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> JSON:
        args = dict(arguments or {})
        self.calls.append((name, args))
        if name not in self.responses:
            raise TutuMcpToolError(f"Fake Tutu MCP has no response for {name}")
        response = self.responses[name]
        if callable(response):
            response = response(name, args)
        elif isinstance(response, list):
            if not response:
                raise TutuMcpToolError(f"Fake Tutu MCP response queue is empty for {name}")
            response = response.pop(0)
        if isinstance(response, Exception):
            raise response
        return _as_json_object(response, context=f"fake tool {name} response")


# A shorter name is convenient in application wiring while preserving the
# explicit StreamableHttpMcpClient name for documentation and tests.
TutuMcpClient = StreamableHttpMcpClient
