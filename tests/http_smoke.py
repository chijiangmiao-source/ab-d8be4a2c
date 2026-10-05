"""HTTP smoke test against the running review API.

Exercises the real HTTP server end to end:

  GET  /health
  POST legal overlap-window model          -> freeze_approved (201)
  POST cooling-gap model                   -> rejected, earliest event, guards
  POST semantically equivalent retransmit  -> replayed original conclusion
  POST conflicting retransmit              -> original evidence kept
  GET  /api/reviews/<id>                   -> stored conclusion

Exits 0 only if every assertion holds.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from fractions import Fraction

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000")

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}")
        failures.append(msg)


def request(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def legal_payload(audit_id):
    return {
        "audit_id": audit_id,
        "locations": ["INIT", "COOLING", "ARMED", "FROZEN"],
        "initial": "INIT",
        "accepting": ["FROZEN"],
        "clocks": ["tCool", "tArm"],
        "events": ["close_cmd", "confirm", "freeze_cmd"],
        "transitions": [
            {"source": "INIT", "target": "COOLING", "event": "close_cmd",
             "guard": [], "resets": ["tCool"]},
            {"source": "COOLING", "target": "ARMED", "event": "confirm",
             "guard": [{"clock": "tCool", "min": "10", "max": "20"}],
             "resets": ["tArm"]},
            {"source": "ARMED", "target": "FROZEN", "event": "freeze_cmd",
             "guard": [{"clock": "tArm", "min": "5", "max": "15"}],
             "resets": []},
        ],
        "event_sequence": [
            {"event": "close_cmd", "min": "0", "max": "0"},
            {"event": "confirm", "min": "10", "max": "20"},
            {"event": "freeze_cmd", "min": "5", "max": "15"},
        ],
    }


def wait_for_health(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, body = request("GET", "/health")
            if status == 200 and body.get("status") == "ok":
                return True
        except OSError:
            time.sleep(0.4)
    return False


def main():
    print(f"[smoke] target {BASE}")
    check(wait_for_health(), "GET /health 返回 200 ok")

    print("[smoke] 1) legal overlap window -> freeze")
    p = legal_payload("SMOKE-LEGAL")
    status, res = request("POST", "/api/reviews", p)
    check(status == 201, f"首次提交返回 201（实际 {status}）")
    check(res.get("verdict") == "freeze_approved", "合法重叠时窗冻结通过")
    check(len(res.get("evidence", [])) == 3, "返回 3 个逐事件区域证据")
    traj = res.get("witness_trajectory", [])
    check(len(traj) == 3 and all("clocks_at_event" in w for w in traj),
          "返回 3 步可代入具体轨迹")
    check(traj[-1].get("location_after") == "FROZEN", "轨迹终态为 FROZEN")

    print("[smoke] 2) cooling gap -> rejected with earliest event + guards")
    g = legal_payload("SMOKE-GAP")
    g["transitions"] = [
        {"source": "INIT", "target": "COOLING", "event": "close_cmd",
         "guard": [], "resets": ["tCool"]},
        {"source": "COOLING", "target": "ARMED", "event": "confirm",
         "guard": [{"clock": "tCool", "min": "10", "max": "20"}],
         "resets": ["tArm"]},
        {"source": "COOLING", "target": "ARMED", "event": "confirm",
         "guard": [{"clock": "tCool", "min": "22", "max": "30"}],
         "resets": ["tArm"]},
        {"source": "ARMED", "target": "FROZEN", "event": "freeze_cmd",
         "guard": [{"clock": "tArm", "min": "5", "max": "15"}],
         "resets": []},
    ]
    g["event_sequence"] = [
        {"event": "close_cmd", "min": "0", "max": "0"},
        {"event": "confirm", "min": "19", "max": "22"},
        {"event": "freeze_cmd", "min": "6", "max": "8"},
    ]
    status, res = request("POST", "/api/reviews", g)
    check(res.get("verdict") == "rejected", "冷却缺口场景被拒绝")
    check(res.get("reason") == "uncovered", "原因为 uncovered")
    check(res.get("earliest_event_index") == 1, "最早事件稳定指向 #1")
    check(res.get("earliest_event") == "confirm", "最早事件名为 confirm")
    cv = res["detail"]["clock_values"]
    v = Fraction(cv["tCool"])
    check(Fraction(20) < v < Fraction(22), f"阻断时钟取值落在缺口内（tCool={v}）")
    blocking = res["detail"]["blocking_guards"]
    check({b["transition_id"] for b in blocking} == {1, 2},
          "同时展示两个阻断守卫")
    check(all(not b["satisfied"] and b["violations"] for b in blocking),
          "每个阻断守卫给出具体违背项")

    print("[smoke] 3) semantically equivalent retransmission -> replay")
    p2 = json.loads(json.dumps(p))
    p2["transitions"][1]["guard"][0] = {"clock": "tCool", "min": "20/2", "max": "40/2"}
    p2["transitions"][2], p2["transitions"][1] = p2["transitions"][1], p2["transitions"][2]
    status, res = request("POST", "/api/reviews", p2)
    check(status == 200, f"重传返回 200（实际 {status}）")
    check(res.get("submission") == "replayed", "语义等价重传标记为 replayed")
    check(res.get("verdict") == "freeze_approved", "回放原冻结通过结论")
    check(res.get("retransmission", {}).get("outcome")
          == "semantic_equivalent_replay", "重传结论码正确")

    print("[smoke] 4) conflicting retransmission -> keep original evidence")
    p3 = json.loads(json.dumps(p))
    p3["event_sequence"][1] = {"event": "confirm", "min": "9", "max": "25"}
    status, res = request("POST", "/api/reviews", p3)
    check(res.get("submission") == "conflict", "内容不同标记为 conflict")
    check(res.get("verdict") == "freeze_approved", "冲突时保留原证据（原结论仍为通过）")
    check(res.get("retransmission", {}).get("outcome") == "content_conflict",
          "冲突结论码正确")
    status, stored = request("GET", "/api/reviews/SMOKE-LEGAL")
    check(status == 200 and stored.get("verdict") == "freeze_approved",
          "GET 取回的原始结论未被覆盖")

    print("[smoke] 5) illegal model -> 422 stable rejection")
    bad = legal_payload("SMOKE-BAD")
    bad["transitions"][1]["guard"][0] = {"clock": "tCool", "min": "10", "max": "22"}
    bad["transitions"].append({
        "source": "COOLING", "target": "ARMED", "event": "confirm",
        "guard": [{"clock": "tCool", "min": "22", "max": "30"}],
        "resets": ["tArm"]})
    status, res = request("POST", "/api/reviews", bad)
    check(status == 422 and res.get("reason") == "illegal_model",
          f"非法模型返回 422 illegal_model（实际 {status}）")

    if failures:
        print(f"[smoke] FAILED: {len(failures)} assertion(s)")
        return 1
    print("[smoke] ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
