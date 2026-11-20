#!/usr/bin/env python3
"""Explicit real-host acceptance, never run by unittest discovery.

Run only with current human permission for one dedicated persistent test thread.
No general shell, dataset operations or arbitrary path/command tool is exposed.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from codex_host import CodexHost, HostError
from production import atomic_write

BASE = Path(__file__).resolve().parents[1] / "materials/packages/workspace-upgrade-20261007/host-qualification"
OBJECTIVE = ("Complete the isolated architecture qualification by advancing the synthetic counter to 2 "
             "in two distinct model turns using only architecture_probe. Call it at most once per turn, "
             "then give a final message for that turn. At counter 1 keep this native Goal active. "
             "On the next automatic continuation, advance to 2, verify the returned evidence, and "
             "mark this Goal complete. Never read or change files, run commands, access apps/network, "
             "start other agents, touch data tasks or credentials. This is authorized synthetic testing only.")


def now():
    return datetime.now(timezone.utc).isoformat()


class Probe:
    def __init__(self, base=BASE):
        self.base = base
        self.lock = threading.Lock()
        self.state = {"counter": 0, "turns": [], "calls": [], "intent": "active"}
        if (base / "probe.json").exists():
            self.state = json.loads((base / "probe.json").read_text())

    def save(self):
        atomic_write(self.base / "probe.json", self.state)

    def call(self, params):
        with self.lock:
            if self.state["intent"] != "active":
                result = {"admitted": False, "reason": "paused", "counter": self.state["counter"]}
            elif params["tool"] != "architecture_probe":
                result = {"admitted": False, "reason": "unqualified tool"}
            elif params["turnId"] in self.state["turns"]:
                result = {"admitted": False, "reason": "one step per turn", "counter": self.state["counter"]}
            else:
                self.state["counter"] += 1
                self.state["turns"].append(params["turnId"])
                self.state["calls"].append({"call_id": params["callId"], "turn_id": params["turnId"], "admitted_at": now()})
                self.save()
                result = {"admitted": True, "counter": self.state["counter"], "target": 2,
                          "instruction": "Give final for this turn; keep Goal active if counter<2, otherwise verify and mark complete."}
        return {"success": True, "contentItems": [{"type": "inputText", "text": json.dumps(result)}]}

    def pause(self):
        with self.lock:
            self.state["intent"] = "paused"
            self.state["paused_at"] = now()
            self.save()


def main():
    resume = sys.argv[1:] == ["--resume-authorized-thread"]
    if not resume and sys.argv[1:] != ["--create-one-authorized-thread"]:
        raise SystemExit("Explicit dedicated-thread authorization flag required.")
    BASE.mkdir(parents=True, exist_ok=True)
    request = BASE / "start-request.json"
    # O_EXCL protects against a second create after an uncertain response.
    if not resume:
        fd = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"requested_at": now(), "objective": OBJECTIVE, "mode": "synthetic-only", "outcome": "pending_response_do_not_recreate"}, stream)
            stream.flush(); os.fsync(stream.fileno())
    probe = Probe()
    probe.state["intent"] = "active"
    probe.save()
    events = json.loads((BASE / "events.json").read_text())["events"] if (BASE / "events.json").exists() else []

    def on_event(message):
        method = message.get("method", "")
        if method in ("turn/started", "turn/completed", "thread/goal/updated", "error"):
            events.append(message)
            atomic_write(BASE / "events.json", {"events": events})
            print(json.dumps({"event": method, "turn_status": message.get("params", {}).get("turn", {}).get("status")}), flush=True)

    host = CodexHost(on_event=on_event, on_tool=probe.call)
    tid = None
    try:
        start_params = {
            "cwd": str(BASE), "ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only",
            "environments": [], "selectedCapabilityRoots": [],
            "baseInstructions": "You are testing this host in an isolated synthetic fixture. Use only architecture_probe and native goal lifecycle tools. No other tools are authorized.",
            "dynamicTools": [{"type": "function", "name": "architecture_probe", "description": "Advance one synthetic step per model turn and return evidence. No arbitrary commands or paths accepted.",
                              "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}],
        }
        if resume:
            tid = json.loads((BASE / "host-binding.json").read_text())["thread_id"]
            result = host.call("thread/resume", {"threadId": tid, "excludeTurns": True,
                                                "config": {"features": {"code_mode_host": True}},
                                                "approvalPolicy": "never", "sandbox": "read-only"})
        else:
            result = host.call("thread/start", start_params)
        tid = result["thread"]["id"]
        if not resume:
            atomic_write(BASE / "host-binding.json", {"thread_id": tid, "cwd": str(BASE), "created_at": now(), "profile": "synthetic-only"})
        print(json.dumps({"thread_id": tid, "binding": str(BASE / "host-binding.json")}), flush=True)
        goal = host.call("thread/goal/set", {"threadId": tid, "objective": OBJECTIVE, "status": "active", "origin": "user"})
        if not resume:
            atomic_write(BASE / "initial-goal.json", goal)
        host.call("turn/start", {"threadId": tid, "input": [{"type": "text", "text": OBJECTIVE}], "environments": []})
        deadline = time.monotonic() + 180
        current = None
        while time.monotonic() < deadline:
            time.sleep(2)
            current = host.call("thread/goal/get", {"threadId": tid})
            if (current.get("goal") or {}).get("status") in ("complete", "blocked", "usageLimited", "budgetLimited"):
                break
        atomic_write(BASE / "goal-after-continuation.json", current or {})
        print(json.dumps({"goal_status": (current.get("goal") or {}).get("status"), "counter": probe.state["counter"], "turns": len(probe.state["turns"])}), flush=True)
    finally:
        probe.pause()
        if tid:
            try:
                stopped = host.call("thread/goal/set", {"threadId": tid, "status": "paused", "origin": "user"})
                atomic_write(BASE / "paused-goal.json", stopped)
            except HostError as exc:
                atomic_write(BASE / "pause-error.json", {"error": str(exc), "thread_id": tid})
        host.close()
    if tid:
        reopened = CodexHost(on_event=on_event, on_tool=probe.call)
        try:
            result = reopened.call("thread/resume", {"threadId": tid, "excludeTurns": True, "cwd": str(BASE), "approvalPolicy": "never", "sandbox": "read-only"})
            goal = reopened.call("thread/goal/get", {"threadId": tid})
            atomic_write(BASE / "cold-resume.json", {"thread_id": result["thread"]["id"], "goal": goal,
                                                       "counter": probe.state["counter"], "observed_at": now()})
            print(json.dumps({"cold_resumed_same_thread": result["thread"]["id"] == tid,
                              "cold_goal_status": (goal.get("goal") or {}).get("status")}), flush=True)
        finally:
            reopened.close()


if __name__ == "__main__":
    main()
