#!/usr/bin/env python3
"""Actual controlled-tool stop race on the one authorized qualification thread."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from codex_host import CodexHost, AdmissionBoundary
from production import atomic_write

BASE = Path(__file__).resolve().parents[1] / "materials/packages/workspace-upgrade-20261007/host-qualification"


def main():
    if sys.argv[1:] != ["--same-authorized-thread"]:
        raise SystemExit("Only the already authorized qualification thread may be used.")
    tid = json.loads((BASE / "host-binding.json").read_text())["thread_id"]
    state = {"thread_id": tid, "intent": "active", "effects": [], "denials": [], "events": []}
    entered = threading.Event()
    denied = threading.Event()
    completed = threading.Event()
    current_turn = [None]

    def save_pause():
        state["intent"] = "paused"
        state["stop_confirmed_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write(BASE / "stop-boundary.json", state)

    boundary = AdmissionBoundary(lambda: state["intent"], save_pause)

    def on_tool(params):
        def action():
            entered.set()
            time.sleep(2)
            state["effects"].append({"turn_id": params["turnId"], "call_id": params["callId"],
                                     "effect_at": datetime.now(timezone.utc).isoformat()})
            return {"admitted": True, "instruction": "Now call architecture_probe again."}
        result = boundary.execute(action)
        if not result.get("admitted"):
            state["denials"].append({"turn_id": params["turnId"], "call_id": params["callId"],
                                     "denied_at": datetime.now(timezone.utc).isoformat()})
            denied.set()
        atomic_write(BASE / "stop-boundary.json", state)
        return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result)}]}

    def on_event(message):
        if message.get("method") in ("turn/started", "turn/completed"):
            turn = message.get("params", {}).get("turn", {})
            state["events"].append({"method": message["method"], "turn_id": turn.get("id"), "status": turn.get("status")})
            if message["method"] == "turn/started":
                current_turn[0] = turn.get("id")
            else:
                completed.set()

    host = CodexHost(on_tool=on_tool, on_event=on_event)
    try:
        host.call("thread/resume", {"threadId": tid, "excludeTurns": True,
                                    "config": {"features": {"code_mode_host": True}},
                                    "approvalPolicy": "never", "sandbox": "read-only"})
        # Goal stays paused. This explicit synthetic turn tests already-admitted
        # work and then a stale tool request, without automatic continuation.
        host.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text":
            "Authorized isolated stop-boundary test only: call architecture_probe twice sequentially. It may refuse the second call. Report the returned values without doing anything else; keep Goal paused."}], "environments": []})
        if not entered.wait(60):
            raise RuntimeError("No real tool request reached the controlled boundary.")
        receipt = boundary.stop()
        state["stop_receipt"] = receipt
        if not denied.wait(60):
            raise RuntimeError("No actual post-stop tool request was observed; cannot claim race coverage.")
        host.call("thread/goal/set", {"threadId": tid, "status": "paused", "origin": "user"})
        if not completed.is_set() and current_turn[0]:
            host.call("turn/interrupt", {"threadId": tid, "turnId": current_turn[0]})
            completed.wait(15)
        state["passed"] = (len(state["effects"]) == 1 and len(state["denials"]) >= 1
                           and all(x["effect_at"] <= state["stop_confirmed_at"] for x in state["effects"]))
        atomic_write(BASE / "stop-boundary.json", state)
        print(json.dumps({"passed": state["passed"], "effects_before_stop": len(state["effects"]),
                          "real_calls_denied_after_stop": len(state["denials"]), "thread_id": tid}), flush=True)
    finally:
        boundary.stop()
        host.close()


if __name__ == "__main__":
    main()
