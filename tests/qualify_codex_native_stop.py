#!/usr/bin/env python3
"""Real native shell interrupt on the single authorized synthetic thread."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from codex_host import CodexHost
from production import atomic_write

BASE = Path(__file__).resolve().parents[1] / "materials/packages/workspace-upgrade-20261007/host-qualification"


def main():
    if sys.argv[1:] != ["--same-authorized-thread"]:
        raise SystemExit("Use only the explicitly authorized dedicated test thread.")
    tid = json.loads((BASE / "host-binding.json").read_text())["thread_id"]
    started, completed = threading.Event(), threading.Event()
    events = []
    turn_id = [None]
    confirmation = [None]
    def event(message):
        m = message.get("method")
        p = message.get("params", {})
        item = p.get("item", {})
        if m == "turn/started":
            turn_id[0] = p["turn"]["id"]
        if m in ("item/started", "item/completed") and item.get("type") == "commandExecution":
            events.append({"method": m, "type": item["type"], "observed_at": time.monotonic(), "status": item.get("status"), "after_confirmation": confirmation[0] is not None})
            if m == "item/started":started.set()
        if m == "turn/completed":
            events.append({"method": m, "status": p["turn"].get("status"), "observed_at": time.monotonic()})
            completed.set()
    host = CodexHost(on_event=event, profile="native-single-writer")
    try:
        host.call("thread/resume", {"threadId": tid, "excludeTurns": True, "cwd": str(BASE),
                                    "baseInstructions": "Isolated local architecture test. Only execute the exact synthetic local test command requested. No apps, credentials, data tools, or unrelated files.",
                                    "config": {"features": {"shell_tool": True, "unified_exec": True, "code_mode_host": True, "multi_agent": False}},
                                    "approvalPolicy": "never", "sandbox": "workspace-write"})
        prompt = ('Run one foreground shell command in this exact cwd: python3 -c "import time; time.sleep(20); print(\'SYNTHETIC-NATIVE-ACTION-COMPLETE\')". '
                  'Then run a second foreground command that prints SYNTHETIC-SECOND-ACTION. Do not use architecture_probe. Do not touch files, secrets, network, data tools, or other sessions. Keep the Goal paused.')
        host.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": prompt}],
                                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(BASE)],
                                                  "networkAccess": False, "excludeSlashTmp": True, "excludeTmpdirEnvVar": True},
                                "environments": [{"environmentId": "local", "cwd": str(BASE), "runtimeWorkspaceRoots": [str(BASE)]}]})
        if not started.wait(60):raise RuntimeError("No native shell action observed.")
        paused = host.call("thread/goal/set", {"threadId": tid, "status": "paused", "origin": "user"})
        interrupt = host.call("turn/interrupt", {"threadId": tid, "turnId": turn_id[0]})
        if not completed.wait(30):raise RuntimeError("No completed native interruption; cannot confirm stop.")
        confirmation[0] = time.monotonic()
        time.sleep(2)
        result = {"thread_id": tid, "passed": any(e.get("status") == "interrupted" for e in events if e["method"] == "turn/completed")
                  and not any(e.get("after_confirmation") and e["method"] == "item/started" for e in events),
                  "events": events, "native_goal": paused, "interrupt_response": interrupt,
                  "stop_confirmation_at": confirmation[0], "observed_at": datetime.now(timezone.utc).isoformat(),
                  "coverage": "one parent native turn; no child agents or external remote cancellation"}
        atomic_write(BASE / "native-stop.json", result)
        print(json.dumps({"passed": result["passed"], "events": events, "thread_id": tid}), flush=True)
    finally:
        host.close()


if __name__ == "__main__":main()
