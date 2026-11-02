"""Construct customer adapters only from a locally approved, hash-pinned binding.

Remote connection payloads are inputs to verification, never a source of commands,
imports, credentials, drivers, or network exceptions.
"""

from __future__ import annotations

import ipaddress
import importlib
import socket
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from susanoo_contracts import content_hash

from .journal import JournalError


_ADAPTERS: dict[str, tuple[str, str]] = {
    "http_sync": ("I03", "HttpConnection"),
    "http_async": ("I04", "AsyncHttpConnection"),
    "graphql": ("I03", "GraphqlConnection"),
    "sse": ("I05", "StreamConnection"),
    "websocket": ("I05", "StreamConnection"),
    "a2a": ("I10", "A2AConnection"),
    "grpc": ("I10", "GrpcConnection"),
    "browser": ("I11", "BrowserConnection"),
    "cli": ("I08", "CliProfile"),
    "python_sdk": ("I06", "CliProfile"),
    "typescript_sdk": ("I07", "CliProfile"),
    "container": ("I08", "ContainerProfile"),
    "mcp_tools": ("I09", "McpConnection"),
    "mcp_agent": ("I09", "McpConnection"),
}


def _canonical_host(host: str) -> str:
    host = host.lower().rstrip(".")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        try:
            return host.encode("idna").decode("ascii")
        except UnicodeError:
            raise JournalError("egress_denied", "Connection host is invalid") from None


def _resolve_host(
    host: str, port: int | None, *, allowed_hosts: list[str], allow_private: list[str]
) -> tuple[str, ...]:
    canonical = _canonical_host(host)
    allowed = {_canonical_host(item) for item in allowed_hosts}
    if canonical not in allowed:
        raise JournalError("egress_denied", "Connection host is not explicitly allowed by the local binding")
    try:
        networks = [ipaddress.ip_network(item, strict=False) for item in allow_private]
    except ValueError:
        raise JournalError("profile_refused", "Local private-network approvals must be valid CIDR ranges") from None

    try:
        literal = ipaddress.ip_address(canonical)
        addresses = {literal}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(canonical, port, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError):
            raise JournalError("egress_denied", "Connection host did not resolve safely") from None
    if not addresses:
        raise JournalError("egress_denied", "Connection host did not resolve safely")
    for address in addresses:
        if not address.is_global and not any(address in network for network in networks):
            raise JournalError("egress_denied", "Connection resolves to a private, link-local, or reserved address")
    return tuple(str(address) for address in sorted(addresses, key=lambda item: (item.version, int(item))))


def check_egress_url(url: str, *, allowed_hosts: list[str], allow_private: list[str] = ()) -> tuple[str, ...]:
    """Resolve an explicitly allowed URL and reject unsafe destinations.

    The returned numeric addresses must be used by the connection transport. A URL-only
    check without connect-time pinning is not a rebinding defense.
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise JournalError("egress_denied", "Connection URL is malformed") from None
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not host:
        raise JournalError("egress_denied", "Connection URL has an unsupported scheme or missing host")
    if parsed.username or parsed.password or parsed.fragment:
        raise JournalError("egress_denied", "Connection URLs may not embed credentials or fragments")
    return _resolve_host(host, port, allowed_hosts=allowed_hosts, allow_private=allow_private)


def _check_binding_hosts(binding: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    allowed_hosts = binding.get("allowed_hosts", [])
    allow_private = binding.get("allow_private_cidrs", [])
    if not isinstance(allowed_hosts, list) or not all(isinstance(item, str) for item in allowed_hosts):
        raise JournalError("profile_refused", "Local allowed_hosts must be a list of hostnames")
    if not isinstance(allow_private, list) or not all(isinstance(item, str) for item in allow_private):
        raise JournalError("profile_refused", "Local allow_private_cidrs must be a list of CIDR ranges")
    return allowed_hosts, allow_private


def _pin_urls(urls: list[str], binding: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    allowed_hosts, allow_private = _check_binding_hosts(binding)
    pins: dict[str, tuple[str, ...]] = {}
    for url in urls:
        parsed = urlsplit(url)
        host = _canonical_host(parsed.hostname or "")
        if host not in pins:
            pins[host] = check_egress_url(url, allowed_hosts=allowed_hosts, allow_private=allow_private)
        else:
            # Still validate the complete URL's scheme and credentials, but retain the
            # first resolution so one run never observes two DNS answers for one host.
            check_egress_url(url, allowed_hosts=allowed_hosts, allow_private=allow_private)
    return pins


class _PinnedNetworkBackend:
    """httpcore network backend that connects only to the numeric answers pinned at setup."""

    def __init__(self, pins: Mapping[str, tuple[str, ...]], httpcore_module=None):
        if httpcore_module is None:
            import httpcore as httpcore_module

        backend_module = importlib.import_module(f"{httpcore_module.__name__}._backends.auto")

        self._httpcore = httpcore_module
        self._backend = backend_module.AutoBackend()
        self._pins = {key: tuple(value) for key, value in pins.items()}

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        host = _canonical_host(host)
        candidates = self._pins.get(host)
        if candidates is None:
            raise self._httpcore.ConnectError("Destination was not pinned by the local binding")
        errors = []
        for address in candidates:
            try:
                return await self._backend.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (self._httpcore.ConnectError, self._httpcore.ConnectTimeout) as error:
                errors.append(error)
        if errors:
            raise errors[-1]
        raise self._httpcore.ConnectError("No pinned destination addresses are available")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return await self._backend.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


class _PinnedHttpTransport(httpx.AsyncHTTPTransport):
    """Direct HTTP transport with fixed DNS answers, TLS authority preservation and byte cap."""

    def __init__(self, pins: Mapping[str, tuple[str, ...]], max_bytes: int):
        import httpcore

        self.max_bytes = max_bytes
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        super().__init__(trust_env=False, retries=0, limits=limits)
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=_PinnedNetworkBackend(pins, httpcore),
            retries=0,
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=5,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.headers["Accept-Encoding"] = "identity"
        response = await super().handle_async_request(request)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            await response.aclose()
            raise JournalError("encoding_denied", "Compressed responses are not approved")
        chunks, size = [], 0
        try:
            async for chunk in response.aiter_raw():
                size += len(chunk)
                if size > self.max_bytes:
                    raise JournalError("output_limit", "Connector response exceeded the local byte limit")
                chunks.append(chunk)
        finally:
            await response.aclose()
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=b"".join(chunks),
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def _pinned_mcp_transport(pins: Mapping[str, tuple[str, ...]], max_bytes: int):
    """Build an httpx2 transport lazily so MCP remains an optional connector import."""
    import httpcore2
    import httpx2

    class PinnedMcpTransport(httpx2.AsyncHTTPTransport):
        def __init__(self):
            limits = httpx2.Limits(max_connections=20, max_keepalive_connections=10)
            super().__init__(trust_env=False, retries=0, limits=limits)
            self._pool = httpcore2.AsyncConnectionPool(
                network_backend=_PinnedNetworkBackend(pins, httpcore2),
                retries=0,
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=5,
            )

        async def handle_async_request(self, request):
            request.headers["Accept-Encoding"] = "identity"
            response = await super().handle_async_request(request)
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                await response.aclose()
                raise JournalError("encoding_denied", "Compressed responses are not approved")
            chunks, size = [], 0
            try:
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > max_bytes:
                        raise JournalError("output_limit", "MCP response exceeded the local byte limit")
                    chunks.append(chunk)
            finally:
                await response.aclose()
            return httpx2.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=request,
                extensions=response.extensions,
            )

    return PinnedMcpTransport()


def factory_for_connection(
    connection: dict | Any,
    connection_hash: str,
    bindings: Mapping[str, dict],
    *,
    secret_env: Mapping[str, str] | None = None,
    browser_driver: Any = None,
    mcp_driver: Any = None,
    grpc_transport: Any = None,
    egress_transport: Any = None,
):
    """Return a zero-argument adapter factory after exact local approval.

    Binding JSON keys: ``adapter``, ``config`` (typed connector config), ``allowed_hosts``
    and ``allow_private_cidrs``. These are local runner settings, keyed by connection hash.
    """
    data = connection.model_dump(mode="json") if hasattr(connection, "model_dump") else dict(connection)
    if content_hash(data) != connection_hash:
        raise JournalError("manifest_mismatch", "Connection does not match the pinned connection hash")
    binding = bindings.get(connection_hash)
    if not isinstance(binding, dict):
        raise JournalError("unapproved_connection", "No local binding exists for this pinned connection")
    adapter = data.get("adapter")
    spec = _ADAPTERS.get(str(adapter))
    if spec is None or binding.get("adapter") != adapter:
        raise JournalError("unapproved_adapter", "Connection adapter is not supported by this local binding")
    family, model_name = spec
    if data.get("family") != family:
        raise JournalError("unapproved_adapter", "Connection adapter does not match its canonical family")
    config = binding.get("config")
    expected_kind = "mcp" if adapter in {"mcp_tools", "mcp_agent"} else (
        "cli" if adapter in {"python_sdk", "typescript_sdk"} else adapter)
    if not isinstance(config, dict) or config.get("kind", expected_kind) != expected_kind:
        raise JournalError("profile_refused", "Local binding needs a typed config matching its adapter")
    if binding.get("egress_proxy"):
        raise JournalError("profile_refused", "Unverifiable proxy settings are not accepted; use the pinned transport")
    if secret_env is not None and adapter not in {"mcp_tools", "mcp_agent"}:
        raise JournalError("profile_refused", "Non-MCP authentication uses named local process environment references")
    if adapter in {"grpc", "mcp_tools", "mcp_agent"}:
        return _special_factory(
            adapter,
            config,
            binding,
            secret_env,
            mcp_driver,
            grpc_transport,
            egress_transport=egress_transport,
        )

    from susanoo_connectors import (
        A2AAgentAdapter,
        A2AConnection,
        AsyncHttpAgentAdapter,
        AsyncHttpConnection,
        BrowserAgentAdapter,
        BrowserConnection,
        CliAgentAdapter,
        CliProfile,
        ContainerAgentAdapter,
        ContainerProfile,
        GraphqlAgentAdapter,
        GraphqlConnection,
        HttpAgentAdapter,
        HttpConnection,
        PlaywrightDriver,
        SseAgentAdapter,
        StreamConnection,
        WebSocketAgentAdapter,
    )

    classes = {
        "HttpConnection": (HttpConnection, HttpAgentAdapter),
        "AsyncHttpConnection": (AsyncHttpConnection, AsyncHttpAgentAdapter),
        "GraphqlConnection": (GraphqlConnection, GraphqlAgentAdapter),
        "StreamConnection": (StreamConnection, SseAgentAdapter if adapter == "sse" else WebSocketAgentAdapter),
        "A2AConnection": (A2AConnection, A2AAgentAdapter),
        "BrowserConnection": (BrowserConnection, BrowserAgentAdapter),
        "CliProfile": (CliProfile, CliAgentAdapter),
        "ContainerProfile": (ContainerProfile, ContainerAgentAdapter),
    }
    model, constructor = classes[model_name]
    if model_name in {"CliProfile", "ContainerProfile"}:
        config = {key: value for key, value in config.items() if key != "kind"}
    try:
        typed = model.model_validate(config)
    except ValidationError:
        raise JournalError("profile_refused", "Local connector binding failed typed validation") from None
    urls, network_pins = _validate_urls(adapter, typed, binding)

    def create():
        if adapter in {"cli", "container", "python_sdk", "typescript_sdk"}:
            return constructor(typed)
        if adapter == "browser":
            if egress_transport is not None:
                raise JournalError("profile_refused", "Browser bindings use the built-in Chromium network policy")
            pinned_hosts = _browser_pins(urls, network_pins)
            driver = PlaywrightDriver(pinned_hosts=pinned_hosts)
            if browser_driver is not None:
                if not isinstance(browser_driver, PlaywrightDriver) or browser_driver.pinned_hosts != pinned_hosts:
                    raise JournalError(
                        "egress_denied", "Browser override must be a PlaywrightDriver with the pinned host map"
                    )
                driver = browser_driver
            return constructor(typed, driver)
        if adapter == "websocket":
            if egress_transport is not None:
                raise JournalError("profile_refused", "WebSocket bindings do not accept arbitrary transport overrides")
            host = _canonical_host(urlsplit(typed.url).hostname or "")
            return constructor(typed, pinned_addresses=network_pins[host])
        max_bytes = getattr(typed, "max_response_bytes", getattr(typed, "max_total_bytes", 262144))
        transport = egress_transport or _PinnedHttpTransport(network_pins, max_bytes)
        return constructor(typed, transport=transport)

    return create


def _validate_urls(
    adapter: str, typed: Any, binding: Mapping[str, Any]
) -> tuple[list[str], dict[str, tuple[str, ...]]]:
    urls = []
    for field in ("url", "submit_url", "status_url", "cancel_url", "card_url"):
        value = getattr(typed, field, None)
        if value:
            urls.append(value.replace("{task_id}", "susanoo-egress-check"))
    if adapter in {"websocket", "sse"}:
        urls.append(typed.url)
    if adapter == "browser":
        urls.extend([typed.start_url, *typed.allowed_origins])
    if adapter in {"cli", "container", "python_sdk", "typescript_sdk"}:
        return urls, {}
    return urls, _pin_urls(urls, binding)


def _browser_pins(urls: list[str], pins: Mapping[str, tuple[str, ...]]) -> dict[str, str]:
    result = {}
    for url in urls:
        host = _canonical_host(urlsplit(url).hostname or "")
        addresses = pins.get(host)
        if not addresses:
            raise JournalError("egress_denied", "Browser origin has no pinned numeric destination")
        result[host] = addresses[0]
    return result


def _special_factory(adapter, config, binding, secret_env, mcp_driver, grpc_transport, *, egress_transport):
    allowed_hosts, allow_private = _check_binding_hosts(binding)
    if adapter == "grpc":
        from susanoo_connectors.grpc_connector import GrpcAgentAdapter, GrpcConnection, GrpcioTransport

        try:
            typed = GrpcConnection.model_validate(config)
        except ValidationError:
            raise JournalError("profile_refused", "Local connector binding failed typed validation") from None
        if typed.target.startswith("["):
            host, separator, raw_port = typed.target[1:].partition("]:")
            if not separator:
                raise JournalError("profile_refused", "gRPC target must be a valid host and port")
        else:
            host, separator, raw_port = typed.target.rpartition(":")
            if not separator:
                raise JournalError("profile_refused", "gRPC target must be a valid host and port")
        try:
            port = int(raw_port)
        except ValueError:
            raise JournalError("profile_refused", "gRPC target port must be numeric") from None
        addresses = _resolve_host(host, port, allowed_hosts=allowed_hosts, allow_private=allow_private)
        numeric_host = addresses[0]
        numeric_target = f"[{numeric_host}]:{port}" if ":" in numeric_host else f"{numeric_host}:{port}"

        def create_grpc():
            transport = grpc_transport or GrpcioTransport(
                numeric_target,
                tls=typed.tls,
                max_message_bytes=typed.max_message_bytes,
                authority=_canonical_host(host),
            )
            return GrpcAgentAdapter(typed, transport=transport)

        return create_grpc

    from susanoo_connectors.mcp import McpAgentAdapter, McpAgentToolDriver, McpConnection, McpToolClient

    try:
        typed = McpConnection.model_validate(config)
    except ValidationError:
        raise JournalError("profile_refused", "Local connector binding failed typed validation") from None
    mcp_pins = None
    if typed.transport == "streamable_http":
        if egress_transport is not None:
            raise JournalError("profile_refused", "MCP HTTP bindings use the built-in pinned client transport")
        mcp_pins = _pin_urls([typed.url or ""], binding)
    elif not binding.get("command_approved") or typed.command != binding.get("approved_argv"):
        raise JournalError("profile_refused", "MCP stdio argv must exactly match the locally approved command")
    if adapter == "mcp_tools":
        raise JournalError("not_an_agent", "MCP tool-only binding cannot be used as an agent")
    driver_config = binding.get("agent_driver")
    if mcp_driver is not None:
        if driver_config is not None or not isinstance(mcp_driver, McpAgentToolDriver):
            raise JournalError("profile_refused", "MCP agent overrides must be a single typed agent-tool mapping")
        driver = mcp_driver
    elif isinstance(driver_config, dict):
        try:
            driver = McpAgentToolDriver.model_validate(driver_config)
        except ValidationError:
            raise JournalError("profile_refused", "MCP agent driver failed typed validation") from None
    else:
        raise JournalError("not_an_agent", "MCP agent binding needs a local typed agent_driver")
    if driver.tool not in typed.allowed_tools:
        raise JournalError("profile_refused", "MCP agent tool must be in the locally approved tool allowlist")

    def create_mcp():
        http_transport = _pinned_mcp_transport(mcp_pins, typed.max_result_bytes) if mcp_pins is not None else None
        tools = McpToolClient(typed, http_transport=http_transport, secret_env=secret_env)
        return McpAgentAdapter(typed, driver, tools=tools)

    return create_mcp
