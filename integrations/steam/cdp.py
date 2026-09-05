"""Live launch-option access via Steam's CEF remote-debugging endpoint.

With the ``.cef-enable-remote-debugging`` marker present, Steam listens on
localhost:8080 (unauthenticated, local only — creating the marker is an
explicit user opt-in). ``SteamClient.Apps.SetAppLaunchOptions`` over
``Runtime.evaluate`` is the same internal call Steam's Properties dialog
uses: it applies while Steam runs and Steam persists it itself. Reads use
``RegisterForAppDetails`` — ``GetLaunchOptionsForApp`` returns the game's
declared launch configurations, not the user's string.

The API is unofficial: feature-detect before use and verify writes by
read-back; callers fall back to the Steam-stopped localconfig path.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import struct
import time

from .users import default_steam_root


CDP_HOST = "127.0.0.1"
CDP_PORT = 8080
CDP_MARKER_FILENAME = ".cef-enable-remote-debugging"

# Names the shared JS context has carried across Steam releases.
_TARGET_TITLES = (
    "SharedJSContext",
    "Steam Shared Context presented by Valve™",
    "Steam",
    "SP",
)

_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class SteamCdpError(RuntimeError):
    pass


@dataclass(frozen=True)
class SteamAppDetails:
    """Runtime facts reported by Steam for one installed app.

    ``compat_tool_name`` is Steam's effective choice, including a default
    Proton selected without a per-game ``CompatToolMapping`` override.
    """

    launch_options: str
    compat_tool_name: str
    compat_tool_display_name: str
    compat_tool_priority: int
    platforms: tuple[str, ...]


def cdp_marker_path(home: Path | None = None) -> Path | None:
    root = default_steam_root(home)
    return root / CDP_MARKER_FILENAME if root is not None else None


def cdp_marker_present(home: Path | None = None) -> bool:
    marker = cdp_marker_path(home)
    return marker is not None and marker.exists()


def ensure_cdp_marker(home: Path | None = None) -> bool:
    """Create the opt-in marker; Steam must restart once to honor it."""
    marker = cdp_marker_path(home)
    if marker is None:
        return False
    if not marker.exists():
        try:
            marker.touch()
        except OSError:
            return False
    return True


def shared_js_context_url(
    *,
    host: str = CDP_HOST,
    port: int = CDP_PORT,
    timeout_s: float = 3.0,
) -> str | None:
    """webSocketDebuggerUrl of Steam's shared JS context, or None."""
    connection = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        connection.request("GET", "/json")
        payload = json.loads(connection.getresponse().read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    finally:
        connection.close()
    if not isinstance(payload, list):
        return None
    for title in _TARGET_TITLES:
        for target in payload:
            if not isinstance(target, dict):
                continue
            if target.get("title") == title:
                url = target.get("webSocketDebuggerUrl")
                if isinstance(url, str) and url:
                    return url
    return None


def cdp_available(**kwargs: object) -> bool:
    return shared_js_context_url(**kwargs) is not None  # type: ignore[arg-type]


def _split_ws_url(url: str) -> tuple[str, int, str]:
    if not url.startswith("ws://"):
        raise SteamCdpError(f"unsupported websocket url: {url}")
    remainder = url[len("ws://") :]
    authority, slash, path = remainder.partition("/")
    host, _, port_text = authority.partition(":")
    try:
        port = int(port_text) if port_text else 80
    except ValueError as error:
        raise SteamCdpError(f"unsupported websocket url: {url}") from error
    return host, port, f"{slash}{path}" or "/"


class _WebSocket:
    """Just enough RFC 6455 for local CDP: text frames, client masking."""

    def __init__(self, url: str, *, timeout_s: float = 10.0) -> None:
        host, port, path = _split_ws_url(url)
        self._pending = b""
        try:
            self._sock = socket.create_connection((host, port), timeout=timeout_s)
        except OSError as error:
            raise SteamCdpError(f"websocket connect failed: {error}") from error
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        try:
            self._sock.sendall(handshake.encode("ascii"))
            response = self._read_handshake_response()
        except OSError as error:
            self._sock.close()
            raise SteamCdpError(f"websocket handshake failed: {error}") from error
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_ACCEPT_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if f"sec-websocket-accept: {expected.lower()}" not in response.lower():
            self._sock.close()
            raise SteamCdpError("websocket handshake rejected")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def send_text(self, payload: str) -> None:
        try:
            self._sock.sendall(encode_ws_frame(payload.encode("utf-8"), mask=True))
        except OSError as error:
            raise SteamCdpError(f"websocket send failed: {error}") from error

    def receive_text(self) -> str:
        message = b""
        while True:
            try:
                opcode, data, final = self._read_frame()
                if opcode == 0x9:  # ping -> pong
                    self._sock.sendall(
                        encode_ws_frame(data, mask=True, opcode=0xA)
                    )
                    continue
            except OSError as error:
                raise SteamCdpError(f"websocket receive failed: {error}") from error
            if opcode == 0x8:
                raise SteamCdpError("websocket closed by Steam")
            if opcode in (0x1, 0x0):
                message += data
                if final:
                    try:
                        return message.decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise SteamCdpError(
                            f"invalid websocket payload: {error}"
                        ) from error

    def _read_handshake_response(self) -> str:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise SteamCdpError("websocket handshake failed")
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        self._pending = rest
        return head.decode("ascii", errors="replace")

    def _read_exactly(self, count: int) -> bytes:
        pending = self._pending
        while len(pending) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise SteamCdpError("websocket connection dropped")
            pending += chunk
        self._pending = pending[count:]
        return pending[:count]

    def _read_frame(self) -> tuple[int, bytes, bool]:
        header = self._read_exactly(2)
        final = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exactly(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exactly(8))
        mask = self._read_exactly(4) if masked else b""
        data = self._read_exactly(length)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return opcode, data, final


def encode_ws_frame(payload: bytes, *, mask: bool, opcode: int = 0x1) -> bytes:
    header = bytes([0x80 | opcode])
    mask_bit = 0x80 if mask else 0
    length = len(payload)
    if length < 126:
        header += bytes([mask_bit | length])
    elif length < 65536:
        header += bytes([mask_bit | 126]) + struct.pack(">H", length)
    else:
        header += bytes([mask_bit | 127]) + struct.pack(">Q", length)
    if not mask:
        return header + payload
    key = os.urandom(4)
    masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return header + key + masked


class SteamCdpClient:
    """One evaluate in flight at a time over the shared JS context."""

    def __init__(
        self,
        *,
        host: str = CDP_HOST,
        port: int = CDP_PORT,
        timeout_s: float = 10.0,
    ) -> None:
        url = shared_js_context_url(host=host, port=port, timeout_s=timeout_s)
        if url is None:
            raise SteamCdpError(
                "Steam CDP endpoint unavailable (marker missing or Steam "
                "not restarted since it was created)"
            )
        self._socket = _WebSocket(url, timeout_s=timeout_s)
        self._next_id = 0

    def close(self) -> None:
        self._socket.close()

    def __enter__(self) -> "SteamCdpClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def evaluate(self, expression: str) -> object:
        self._next_id += 1
        request_id = self._next_id
        self._socket.send_text(
            json.dumps(
                {
                    "id": request_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                }
            )
        )
        while True:
            try:
                message = json.loads(self._socket.receive_text())
            except ValueError as error:
                raise SteamCdpError(f"malformed CDP reply: {error}") from error
            if message.get("id") != request_id:
                continue  # unsolicited CDP event
            if "error" in message:
                raise SteamCdpError(str(message["error"]))
            result = message.get("result", {})
            details = result.get("exceptionDetails")
            if details:
                raise SteamCdpError(
                    str(details.get("text") or "JS exception in Steam context")
                )
            return result.get("result", {}).get("value")

    def set_app_launch_options_supported(self) -> bool:
        return (
            self.evaluate(
                "typeof SteamClient?.Apps?.SetAppLaunchOptions === 'function'"
            )
            is True
        )

    def terminate_app_supported(self) -> bool:
        return (
            self.evaluate("typeof SteamClient?.Apps?.TerminateApp === 'function'")
            is True
        )

    def compat_tool_selection_supported(self) -> bool:
        return (
            self.evaluate(
                "typeof SteamClient?.Apps?.GetAvailableCompatTools === 'function'"
                " && typeof SteamClient?.Apps?.SpecifyCompatTool === 'function'"
            )
            is True
        )

    def available_compat_tools(self, app_id: str) -> tuple[tuple[str, str], ...]:
        value = self.evaluate(
            f"SteamClient.Apps.GetAvailableCompatTools({int(app_id)})"
        )
        if not isinstance(value, list):
            return ()
        tools: list[tuple[str, str]] = []
        seen: set[str] = set()
        for entry in value:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("strToolName") or "").strip()
            display = str(entry.get("strDisplayName") or name).strip()
            if name and name not in seen:
                tools.append((name, display or name))
                seen.add(name)
        return tuple(tools)

    def specify_compat_tool(self, app_id: str, tool_name: str) -> None:
        """Set a per-app tool through Steam; empty restores Steam default."""
        self.evaluate(
            f"SteamClient.Apps.SpecifyCompatTool({int(app_id)},"
            f" {json.dumps(str(tool_name))})"
        )

    def app_details(
        self,
        app_id: str,
        *,
        timeout_s: float = 2.0,
    ) -> SteamAppDetails | None:
        """Read launch options and the effective compatibility tool from Steam.

        This is the same per-app details subscription used by Steam's own
        Properties UI. In particular, ``strCompatToolName`` remains populated
        when Steam selected Proton through its global/default policy and no
        explicit per-game mapping exists on disk.
        """
        return self.app_details_many((app_id,), timeout_s=timeout_s).get(str(int(app_id)))

    def app_details_many(
        self,
        app_ids: Iterable[str],
        *,
        timeout_s: float = 2.0,
    ) -> dict[str, SteamAppDetails]:
        """Read a library in one round trip with concurrent subscriptions.

        Each subscription has its own timeout and cleanup. A missing app
        leaves a hole in the result without discarding successful reads.
        """
        ids = list(dict.fromkeys(str(int(app_id)) for app_id in app_ids))
        if not ids:
            return {}
        timeout_ms = max(100, int(timeout_s * 1000))
        value = self.evaluate(
            f"Promise.all({json.dumps(ids)}.map((appId) => new Promise((resolve) => {{"
            "  let done = false;"
            "  let reg = null;"
            "  let timer = null;"
            "  const cleanup = () => {"
            "    if (timer !== null) clearTimeout(timer);"
            "    try { reg?.unregister(); } catch (err) {}"
            "  };"
            "  const finish = (value) => {"
            "    if (done) return;"
            "    done = true; cleanup(); resolve(value);"
            "  };"
            f"  timer = setTimeout(() => finish(null), {timeout_ms});"
            "  try {"
            "  reg = SteamClient.Apps.RegisterForAppDetails(Number(appId),"
            "    (details) => {"
            "      finish(details ? {"
            "        launchOptions: typeof details.strLaunchOptions === 'string'"
            "          ? details.strLaunchOptions : '',"
            "        compatToolName: typeof details.strCompatToolName === 'string'"
            "          ? details.strCompatToolName : '',"
            "        compatToolDisplayName:"
            "          typeof details.strCompatToolDisplayName === 'string'"
            "            ? details.strCompatToolDisplayName : '',"
            "        compatToolPriority: Number.isFinite(details.nCompatToolPriority)"
            "          ? details.nCompatToolPriority : 0,"
            "        platforms: Array.isArray(details.vecPlatforms)"
            "          ? details.vecPlatforms : []"
            "      } : null);"
            "    });"
            "  if (done) cleanup();"  # Steam may deliver cached details synchronously.
            "  } catch (err) { finish(null); }"
            "})))"
        )
        if not isinstance(value, list) or len(value) != len(ids):
            return {}
        return {
            app_id: details
            for app_id, payload in zip(ids, value)
            if (details := self._parse_app_details(payload)) is not None
        }

    @staticmethod
    def _parse_app_details(value: object) -> SteamAppDetails | None:
        if not isinstance(value, dict):
            return None
        platforms = value.get("platforms")
        try:
            priority = int(value.get("compatToolPriority") or 0)
        except (TypeError, ValueError):
            priority = 0
        return SteamAppDetails(
            launch_options=str(value.get("launchOptions") or ""),
            compat_tool_name=str(value.get("compatToolName") or ""),
            compat_tool_display_name=str(value.get("compatToolDisplayName") or ""),
            compat_tool_priority=priority,
            platforms=tuple(
                str(platform).strip().lower()
                for platform in platforms
                if str(platform).strip()
            )
            if isinstance(platforms, list)
            else (),
        )

    def terminate_app(self, app_id: str) -> None:
        """Ask Steam to shut the running game down (the Stop button in
        Steam's own UI). TerminateApp takes the gameid as a string; for
        regular Steam apps that is the app id."""
        self.evaluate(
            f"SteamClient.Apps.TerminateApp(String({int(app_id)}), false)"
        )

    def app_launch_options(self, app_id: str, *, timeout_s: float = 2.0) -> str | None:
        """The user's launch-options string, live from the running client.

        The in-page timeout must stay below the client's socket timeout, or
        the socket read gives up before the JS fallback can resolve null.
        """
        details = self.app_details(app_id, timeout_s=timeout_s)
        return details.launch_options if details is not None else None

    def set_app_launch_options(
        self,
        app_id: str,
        launch_options: str,
        *,
        verify_timeout_s: float = 5.0,
    ) -> bool:
        """Write and confirm by read-back polling (MoonDeck's pattern)."""
        self.evaluate(
            f"SteamClient.Apps.SetAppLaunchOptions({int(app_id)},"
            f" {json.dumps(launch_options)})"
        )
        deadline = time.monotonic() + verify_timeout_s
        while True:
            if self.app_launch_options(app_id) == launch_options:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
