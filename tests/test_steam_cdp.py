import base64
import hashlib
import http.server
import json
import shutil
import socket
import struct
import subprocess
import threading

import pytest

from integrations.steam.cdp import (
    SteamCdpClient,
    cdp_marker_path,
    encode_ws_frame,
    ensure_cdp_marker,
    shared_js_context_url,
)


_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _FakeSteamCdp:
    """Minimal /json + websocket Runtime.evaluate server for tests."""

    def __init__(self, evaluate):
        self._evaluate = evaluate
        self._ws_server = socket.create_server(("127.0.0.1", 0))
        self.ws_port = self._ws_server.getsockname()[1]
        handler = self._json_handler()
        self._http = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.http_port = self._http.server_port
        threading.Thread(target=self._http.serve_forever, daemon=True).start()
        threading.Thread(target=self._serve_ws, daemon=True).start()

    def close(self):
        self._http.shutdown()
        self._ws_server.close()

    def _json_handler(self):
        ws_port = self.ws_port

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps(
                    [
                        {"title": "unrelated", "webSocketDebuggerUrl": "ws://x/y"},
                        {
                            "title": "SharedJSContext",
                            "webSocketDebuggerUrl": f"ws://127.0.0.1:{ws_port}/devtools/page/1",
                        },
                    ]
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                del format, args

        return Handler

    def _serve_ws(self):
        while True:
            try:
                conn, _ = self._ws_server.accept()
            except OSError:
                return
            threading.Thread(
                target=self._serve_ws_client, args=(conn,), daemon=True
            ).start()

    def _serve_ws_client(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        key = ""
        for line in data.decode("ascii", errors="replace").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_ACCEPT_GUID).encode("ascii")).digest()
        ).decode("ascii")
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        buffer = b""
        while True:
            frame, buffer = self._read_frame(conn, buffer)
            if frame is None:
                return
            request = json.loads(frame)
            result = self._evaluate(request["params"]["expression"])
            response = json.dumps(
                {"id": request["id"], "result": {"result": {"value": result}}}
            )
            conn.sendall(encode_ws_frame(response.encode("utf-8"), mask=False))

    @staticmethod
    def _read_frame(conn, buffer):
        class _Closed(Exception):
            pass

        def read(count):
            nonlocal buffer
            while len(buffer) < count:
                chunk = conn.recv(65536)
                if not chunk:
                    raise _Closed
                buffer += chunk
            value, buffer = buffer[:count], buffer[count:]
            return value

        try:
            header = read(2)
            length = header[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", read(8))[0]
            mask = read(4)
            payload = read(length)
        except _Closed:
            return None, buffer
        return (
            bytes(b ^ mask[i % 4] for i, b in enumerate(payload)).decode("utf-8"),
            buffer,
        )


@pytest.fixture()
def fake_steam():
    state = {
        "launch_options": {"1089130": "gamemoderun %command%"},
        "terminated": [],
        "compat_tool": {"1089130": "proton_experimental"},
    }

    def evaluate(expression):
        if "typeof SteamClient?.Apps?.SetAppLaunchOptions" in expression:
            return True
        if "typeof SteamClient?.Apps?.TerminateApp" in expression:
            return True
        if "typeof SteamClient?.Apps?.GetAvailableCompatTools" in expression:
            return True
        if "GetAvailableCompatTools(" in expression:
            return [
                {
                    "strToolName": "proton_experimental",
                    "strDisplayName": "Proton Experimental",
                },
                {"strToolName": "GE-Proton10-34", "strDisplayName": "GE-Proton10-34"},
            ]
        if "SpecifyCompatTool(" in expression:
            args = expression.partition("SpecifyCompatTool(")[2].rpartition(")")[0]
            app_id, _, value = args.partition(",")
            state["compat_tool"][app_id.strip()] = json.loads(value.strip())
            return None
        if "TerminateApp(" in expression:
            state["terminated"].append(
                expression.partition("TerminateApp(")[2].partition(",")[0].strip()
            )
            return None
        if "SetAppLaunchOptions(" in expression:
            app_id, _, value = expression.partition("SetAppLaunchOptions(")[2].rpartition(
                ")"
            )[0].partition(",")
            state["launch_options"][app_id.strip()] = json.loads(value.strip())
            return None
        if "RegisterForAppDetails(" in expression:
            app_ids = json.loads(
                expression.partition("Promise.all(")[2].partition(".map(")[0]
            )

            def details(app_id):
                tool_name = state["compat_tool"].get(app_id, "")
                return {
                    "launchOptions": state["launch_options"].get(app_id, ""),
                    "compatToolName": tool_name,
                    "compatToolDisplayName": (
                        "Proton Experimental"
                        if tool_name == "proton_experimental"
                        else tool_name
                    ),
                    "compatToolPriority": 75,
                    "platforms": ["windows", "linux"],
                }

            return [details(app_id) for app_id in app_ids]
        raise AssertionError(f"unexpected expression: {expression}")

    server = _FakeSteamCdp(evaluate)
    try:
        yield server, state
    finally:
        server.close()


def test_target_discovery_matches_shared_js_context(fake_steam) -> None:
    server, _ = fake_steam
    url = shared_js_context_url(port=server.http_port)
    assert url == f"ws://127.0.0.1:{server.ws_port}/devtools/page/1"


def test_target_discovery_handles_no_endpoint() -> None:
    with socket.create_server(("127.0.0.1", 0)) as placeholder:
        free_port = placeholder.getsockname()[1]
    assert shared_js_context_url(port=free_port, timeout_s=0.2) is None


def test_read_launch_options_live(fake_steam) -> None:
    server, _ = fake_steam
    with SteamCdpClient(port=server.http_port) as client:
        assert client.set_app_launch_options_supported()
        assert client.app_launch_options("1089130") == "gamemoderun %command%"


def test_read_app_details_reports_steams_effective_compat_tool(fake_steam) -> None:
    server, _ = fake_steam
    with SteamCdpClient(port=server.http_port) as client:
        details = client.app_details("1089130")

    assert details is not None
    assert details.launch_options == "gamemoderun %command%"
    assert details.compat_tool_name == "proton_experimental"
    assert details.compat_tool_display_name == "Proton Experimental"
    assert details.compat_tool_priority == 75
    assert details.platforms == ("windows", "linux")


def test_empty_batch_does_not_contact_steam(monkeypatch) -> None:
    client = object.__new__(SteamCdpClient)
    monkeypatch.setattr(client, "evaluate", lambda *_: pytest.fail("empty request"))
    assert client.app_details_many([]) == {}


def test_batch_preserves_identity_and_successes_when_an_app_times_out(monkeypatch) -> None:
    client = object.__new__(SteamCdpClient)
    calls = []

    def evaluate(expression):
        calls.append(expression)
        return [
            {"launchOptions": "first %command%", "compatToolName": "proton"},
            None,
            {"launchOptions": 'env TITLE="third game" %command%'},
        ]

    monkeypatch.setattr(client, "evaluate", evaluate)
    details = client.app_details_many(["10", "20", "30", "10"])
    assert len(calls) == 1
    assert set(details) == {"10", "30"}
    assert details["10"].launch_options == "first %command%"
    assert details["10"].compat_tool_name == "proton"
    assert details["30"].launch_options == 'env TITLE="third game" %command%'


def test_batch_javascript_bounds_wait_and_cleans_up_every_subscription(monkeypatch) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the Steam subscription fixture")
    client = object.__new__(SteamCdpClient)
    observed = {}

    def evaluate(expression):
        script = """
const active = new Set();
const removed = {};
globalThis.SteamClient = {Apps: {RegisterForAppDetails(id, callback) {
    if (id === 4) throw new Error('app unavailable');
    active.add(id);
    if (id === 1) callback({strLaunchOptions: 'sync %command%'});
    if (id === 2) {
        setTimeout(() => callback({strLaunchOptions: 'async %command%'}), 5);
        setTimeout(() => callback({strLaunchOptions: 'duplicate'}), 10);
    }
    return {unregister() {
        active.delete(id); removed[id] = (removed[id] || 0) + 1;
    }};
}}};
""" + f"""
const value = await ({expression});
console.log(JSON.stringify({{value, active: [...active], removed}}));
"""
        result = subprocess.run(
            [node, "--input-type=module"], input=script, text=True,
            capture_output=True, timeout=5, check=True,
        )
        observed.update(json.loads(result.stdout))
        return observed["value"]

    monkeypatch.setattr(client, "evaluate", evaluate)
    details = client.app_details_many(["1", "2", "3", "4"], timeout_s=0.1)
    assert set(details) == {"1", "2"}
    assert details["1"].launch_options == "sync %command%"
    assert details["2"].launch_options == "async %command%"
    assert observed["active"] == []
    assert observed["removed"] == {"1": 1, "2": 1, "3": 1}


def test_write_verifies_by_read_back(fake_steam) -> None:
    server, state = fake_steam
    with SteamCdpClient(port=server.http_port) as client:
        assert client.set_app_launch_options(
            "1089130", "gamemoderun PENGUIN_BURNER %command%"
        )
    assert state["launch_options"]["1089130"] == (
        "gamemoderun PENGUIN_BURNER %command%"
    )


def test_terminate_app_uses_steam_stop_api(fake_steam) -> None:
    server, state = fake_steam
    with SteamCdpClient(port=server.http_port) as client:
        assert client.terminate_app_supported()
        client.terminate_app("1089130")
    assert state["terminated"] == ["String(1089130)"]


def test_compat_tools_are_listed_and_changed_through_steam(fake_steam) -> None:
    server, state = fake_steam
    with SteamCdpClient(port=server.http_port) as client:
        assert client.compat_tool_selection_supported()
        assert client.available_compat_tools("1089130") == (
            ("proton_experimental", "Proton Experimental"),
            ("GE-Proton10-34", "GE-Proton10-34"),
        )
        client.specify_compat_tool("1089130", "GE-Proton10-34")
    assert state["compat_tool"]["1089130"] == "GE-Proton10-34"


def test_marker_management(tmp_path) -> None:
    root = tmp_path / ".local" / "share" / "Steam"
    (root / "steamapps").mkdir(parents=True)
    marker = cdp_marker_path(tmp_path)
    assert marker is not None and not marker.exists()
    assert ensure_cdp_marker(tmp_path)
    assert marker.exists()
    assert ensure_cdp_marker(tmp_path)  # idempotent
