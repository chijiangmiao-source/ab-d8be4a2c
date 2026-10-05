"""Stable audit storage with semantic retransmission handling.

Submissions are keyed by the stable audit identifier.  A retransmission whose
model and event stream are semantically equal replays the original conclusion;
a retransmission whose content differs keeps the original evidence and is
reported as a conflict.
"""

import json
import os
import threading
from fractions import Fraction
from typing import Any, Dict, Optional, Tuple

from .model import ModelError
from .verifier import run_review


def _norm_fraction(value: Any, what: str) -> str:
    try:
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError
        return str(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, TypeError):
        raise ModelError(f"{what} 不是合法有理数: {value!r}")


def semantic_fingerprint(payload: dict) -> str:
    """Content hash ignoring identifier, item order and rational surface form.

    The model semantics (sets of locations/clocks/transitions, the timed event
    stream) determine the fingerprint; resets/guards entry order, transition
    list order and fractions like 2 / "2" / "4/2" are normalized away.
    """
    if not isinstance(payload, dict):
        raise ModelError("请求体必须为 JSON 对象")

    def strs(key, max_n):
        v = payload.get(key)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ModelError(f"{key} 必须为字符串列表")
        if len(v) > max_n:
            raise ModelError(f"{key} 数量超限")
        return v

    locations = sorted(set(strs("locations", 8)))
    clocks = sorted(set(strs("clocks", 4)))
    alphabet = strs("events", 32)

    trans_raw = payload.get("transitions")
    if not isinstance(trans_raw, list):
        raise ModelError("transitions 必须为列表")
    transitions = []
    for t in trans_raw:
        if not isinstance(t, dict):
            raise ModelError("迁移必须为对象")
        guards = {}
        for g in t.get("guard", []) or []:
            guards[g["clock"]] = [
                _norm_fraction(g["min"], "守卫 min"),
                _norm_fraction(g["max"], "守卫 max"),
            ]
        resets = sorted(set(t.get("resets", []) or []))
        transitions.append([
            t.get("source"), t.get("target"), t.get("event"),
            sorted(guards.items()), sorted(resets),
        ])
    transitions.sort(key=lambda x: json.dumps(x, ensure_ascii=False, sort_keys=True))

    seq_raw = payload.get("event_sequence")
    if not isinstance(seq_raw, list):
        raise ModelError("event_sequence 必须为列表")
    sequence = []
    for e in seq_raw:
        sequence.append([
            e["event"],
            _norm_fraction(e["min"], "相对时刻 min"),
            _norm_fraction(e["max"], "相对时刻 max"),
        ])

    normalized = {
        "locations": locations,
        "initial": payload.get("initial"),
        "accepting": sorted(set(payload.get("accepting", []) or [])),
        "clocks": clocks,
        "events": sorted(alphabet),
        "transitions": transitions,
        "event_sequence": sequence,
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


class AuditStore:
    """JSON-file backed store; safe under concurrent in-process requests."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if not os.path.exists(path):
            self._write({})

    def _read(self) -> Dict[str, dict]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: Dict[str, dict]) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def submit(
        self, audit_id: str, fingerprint: str, result: dict, payload: dict
    ) -> Tuple[dict, str]:
        """Return (result, status) where status is new/replayed/conflict."""
        with self.lock:
            data = self._read()
            existing = data.get(audit_id)
            now = {
                "fingerprint": fingerprint,
                "result": result,
                "payload": payload,
            }
            if existing is None:
                data[audit_id] = now
                self._write(data)
                stored = dict(result)
                stored["submission"] = "new"
                return stored, "new"
            if existing["fingerprint"] == fingerprint:
                replayed = dict(existing["result"])
                replayed["submission"] = "replayed"
                replayed["retransmission"] = {
                    "audit_id": audit_id,
                    "outcome": "semantic_equivalent_replay",
                    "message": "同标识语义等价重传，回放原冻结复核结论",
                }
                return replayed, "replayed"
            # content differs: keep original evidence untouched
            conflict = dict(existing["result"])
            conflict["submission"] = "conflict"
            conflict["retransmission"] = {
                "audit_id": audit_id,
                "outcome": "content_conflict",
                "message": "同标识重传内容与原提交语义不同，保留原证据并报告冲突",
                "original_fingerprint": existing["fingerprint"],
                "incoming_fingerprint": fingerprint,
            }
            return conflict, "conflict"

    def get(self, audit_id: str) -> Optional[dict]:
        with self.lock:
            data = self._read()
            return data.get(audit_id, {}).get("result")
