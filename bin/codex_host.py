"""Small JSON-RPC transport for the installed Codex app-server.

Owns no model loop or data state. The caller supplies a qualified tool boundary;
Codex owns thread/goal state and automatic continuation. No credential inspection.
"""
import json
import queue
import re
from collections import deque
import subprocess
import threading
import time
from safe_diagnostics import safe_text as safe_diagnostic, retain, DIRECTORY


class HostError(RuntimeError):
    def __init__(self, message, *, method=None, request_id=None, outcome="unknown", code=None):
        self.detail = {"message": safe_diagnostic(message), "method": method,
                       "request_id": request_id, "outcome": outcome, "code": code}
        if DIRECTORY.get():
            self.detail = {**retain(self.detail, source='codex_rpc', code=code),
                           'method': method, 'request_id': request_id, 'outcome': outcome}
        super().__init__(json.dumps(self.detail, ensure_ascii=False))


class NativePaginationError(HostError):
    pass


def rpc_pages(call, method, params):
    """Follow one native collection, failing on malformed cursor lineage."""
    cursors = set()
    while True:
        page = call(method, params)
        if not isinstance(page, dict) or not isinstance(page.get('data'), list):
            raise NativePaginationError('Native list returned an invalid page.', method=method)
        yield page['data']
        cursor = page.get('nextCursor')
        if cursor is None:
            return
        if not isinstance(cursor, str) or not cursor or cursor in cursors:
            raise NativePaginationError('Native list returned an invalid or repeated cursor.', method=method)
        cursors.add(cursor)
        params = {**params, 'cursor': cursor}


ISOLATED_FEATURES = (
    "shell_tool", "unified_exec", "multi_agent", "apps", "plugins", "hooks",
    "browser_use", "browser_use_external", "computer_use", "image_generation",
    "view_image", "workspace_dependencies", "skill_search",
)


class CodexHost:
    def __init__(self, binary="codex", *, on_event=None, on_tool=None, profile="isolated", environment=None, role_cwd=None):
        command = [binary, "app-server", "--stdio", "-c", "web_search=\"disabled\""]
        if not role_cwd:
            command.extend(["-c", "project_doc_max_bytes=0"])
        for feature in ISOLATED_FEATURES:
            if profile in ("native-single-writer", "native-collaboration", "programmatic-worker") and feature in ("shell_tool", "unified_exec"):
                continue
            if profile == "programmatic-worker" and feature in ("skill_search","hooks"):
                continue
            if profile == "native-collaboration" and feature == "multi_agent":
                continue
            command.extend(["--disable", feature])
        if profile == "programmatic-worker":
            command.extend(["--enable", "goals", "--enable", "hooks", "--disable", "skip_host_skill_discovery"])
        else:
            command.extend(["--enable", "goals", "--enable", "skip_host_skill_discovery"])
        # Existing qualified callers retain their inherited environment. New
        # independently dispatched runtimes can remove parent actor identity
        # without changing global process environment or credential settings.
        process_options = {'env': environment} if environment is not None else {}
        if role_cwd:
            process_options['cwd'] = role_cwd
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, bufsize=1, **process_options)
        except OSError as exc:
            raise HostError('Local app-server executable did not start.', outcome='not_started',
                            code=type(exc).__name__) from exc
        self.diagnostics = deque(maxlen=8)
        self.diagnostic_directory = DIRECTORY.get()
        self.stderr_reader = threading.Thread(target=self._stderr, daemon=True)
        self.stderr_reader.start()
        self.on_event = on_event or (lambda message: None)
        self.on_tool = on_tool
        self.serial = 0
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.disconnected = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.call("initialize", {"clientInfo": {"name": "data_workspace_production", "version": "1.0"},
                                 "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized", "params": {}})

    def send(self, message):
        with self.write_lock:
            try:
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise HostError("Host transport closed; preserve original thread/goal handle.") from exc

    def call(self, method, params, timeout=30):
        with self.pending_lock:
            self.serial += 1
            serial = self.serial
            response = queue.Queue(maxsize=1)
            self.pending[serial] = response
        self.send({"id": serial, "method": method, "params": params})
        try:
            message = response.get(timeout=timeout)
        except queue.Empty as exc:
            raise HostError("Host response not received; preserve original handle and reconcile before retry.", method=method, request_id=serial) from exc
        finally:
            with self.pending_lock:
                self.pending.pop(serial, None)
        if "error" in message:
            raise HostError(message["error"].get("message", "Host RPC error"), method=method, request_id=serial, code=message["error"].get("code"), outcome="rejected" if message["error"].get("code") in (-32600, -32601, -32602) else "unknown")
        return message.get("result")

    def _stderr(self):
        for line in self.process.stderr:
            if any(level in line for level in ("ERROR", "WARN", "Error", "error:")):
                self.diagnostics.append(retain(line.rstrip('\n'), directory=self.diagnostic_directory,
                                               source='codex_stderr'))

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if "id" in message and "method" not in message:
                    with self.pending_lock:
                        target = self.pending.get(message["id"])
                    if target:
                        target.put(message)
                elif "id" in message:
                    threading.Thread(target=self._request, args=(message,), daemon=True).start()
                else:
                    self.on_event(message)
        finally:
            self.disconnected.set()
            with self.pending_lock:
                waiting = list(self.pending.values())
            for target in waiting:
                try:
                    target.put_nowait({"error": {"message": "Host disconnected; recover original handle."}})
                except queue.Full:
                    pass

    def _request(self, message):
        try:
            if message["method"] == "item/tool/call" and self.on_tool:
                result = self.on_tool(message["params"])
                self.send({"id": message["id"], "result": result})
            else:
                self.send({"id": message["id"], "error": {"code": -32601,
                           "message": "This isolated host profile does not admit this tool or approval."}})
        except Exception as exc:
            reason = safe_diagnostic(exc) if isinstance(exc, (ValueError, HostError)) else type(exc).__name__
            self.send({"id": message["id"], "error": {"code": -32603,
                       "message": "Controlled tool " + message.get("method", "unknown") + " failed: " + reason,
                       "data": {"request_id": message["id"], "exception_type": type(exc).__name__,
                                "outcome": "unknown", "replay_allowed": False}}})

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.reader.join(timeout=1)
        self.stderr_reader.join(timeout=1)
        self.process.stdout.close()
        self.process.stderr.close()
        # Transport closure alone is never reported as native cancellation.


class AdmissionBoundary:
    """One local owner for stop versus controlled tool admission.

    It is only a boundary for actions routed through execute. It makes no claims
    about built-in host tools, external sessions, or native database cancellation.
    """
    def __init__(self, read_intent, save_pause):
        self.lock = threading.RLock()
        self.read_intent = read_intent
        self.save_pause = save_pause
        self.active = 0

    def execute(self, action):
        with self.lock:
            if self.read_intent() != "active":
                return {"admitted": False, "reason": "paused"}
            self.active += 1
            try:
                return action()
            finally:
                self.active -= 1

    def stop(self):
        with self.lock:
            self.save_pause()
            return {"controlled_admission_closed": True, "controlled_inflight": self.active,
                    "other_hosts_or_tools": "not_covered"}
