"""Rule tests for the interlock freeze review verifier.

Covered scenarios required by the audit procedure:

* 合法重叠时窗: closed jitter windows meeting the cooling/confirmation guards
  on their closed endpoints must FREEZE;
* 冷却区间缺口: a hole between two closed cooling bands must REJECT, naming the
  earliest event with a concrete clock assignment and the blocking guards;
* 重传: semantically equivalent retransmissions replay the original conclusion;
  semantically different retransmissions keep the original evidence and report
  a conflict.

Plus determinism/reset/non-accepting/illegal-guard rules and witness replay.
"""

import json
import os
import sys
import tempfile
import unittest
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.dbm import DBM  # noqa: E402
from app.model import ModelError, build_model  # noqa: E402
from app.store import AuditStore, semantic_fingerprint  # noqa: E402
from app.verifier import run_review  # noqa: E402


def legal_payload():
    return {
        "audit_id": "AUDIT-LEGAL",
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


def gap_payload():
    p = legal_payload()
    p["audit_id"] = "AUDIT-GAP"
    p["transitions"] = [
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
    p["event_sequence"] = [
        {"event": "close_cmd", "min": "0", "max": "0"},
        {"event": "confirm", "min": "19", "max": "22"},
        {"event": "freeze_cmd", "min": "6", "max": "8"},
    ]
    return p


def replay_witness(payload, result):
    """Replay the concrete witness trajectory and check every constraint."""
    names = payload["clocks"]
    trans = {(t["source"], t["event"], i): t
             for i, t in enumerate(payload["transitions"])}
    clocks = {c: Fraction(0) for c in names}
    last_loc = payload["initial"]
    for i, w in enumerate(result["witness_trajectory"]):
        ev = payload["event_sequence"][i]
        delta = Fraction(w["relative_delay"])
        assert Fraction(ev["min"]) <= delta <= Fraction(ev["max"]), (
            f"delay {delta} outside {ev}")
        for c, v in w["clocks_at_event"].items():
            val = Fraction(v)
            assert val >= 0, f"negative clock {c}={val}"
        for c in names:
            before = Fraction(w["clocks_before"][c])
            assert before == clocks[c], (
                f"event {i} clock {c} before={before} != carried {clocks[c]}")
            at = Fraction(w["clocks_at_event"][c])
            assert at == before + delta, (
                f"event {i} clock {c}: {before}+{delta}!={at}")
        if w.get("stalled"):
            return last_loc, clocks, True
        # find the taken transition by id
        t = payload["transitions"][w["transition_id"]]
        assert t["source"] == last_loc == w["location_before"]
        for g in t["guard"]:
            v = Fraction(w["clocks_at_event"][g["clock"]])
            assert Fraction(g["min"]) <= v <= Fraction(g["max"]), (
                f"guard violated: {g['clock']}={v}")
        # after reset: reset clocks 0, others equal at-event
        for c in names:
            at = Fraction(w["clocks_at_event"][c])
            after = Fraction(w["clocks_after_reset"][c])
            if c in t["resets"]:
                assert after == 0
            else:
                assert after == at
            clocks[c] = after
        last_loc = t["target"]
    return last_loc, clocks, False


class TestLegalOverlapWindow(unittest.TestCase):
    def test_closed_windows_freeze(self):
        res = run_review(legal_payload())
        self.assertEqual(res["verdict"], "freeze_approved")
        self.assertEqual(res["final_locations"], ["FROZEN"])
        loc, _, stalled = replay_witness(legal_payload(), res)
        self.assertFalse(stalled)
        self.assertEqual(loc, "FROZEN")

    def test_fraction_endpoint_closed_covered(self):
        p = legal_payload()
        # jitter window whose endpoints exactly touch 9/2 and 20 (closed)
        p["transitions"][1]["guard"][0] = {"clock": "tCool", "min": "9/2", "max": "20"}
        p["event_sequence"][1] = {"event": "confirm", "min": "9/2", "max": "20"}
        res = run_review(p)
        self.assertEqual(res["verdict"], "freeze_approved")
        replay_witness(p, res)

    def test_fraction_slip_just_past_boundary(self):
        p = legal_payload()
        p["event_sequence"][1] = {"event": "confirm", "min": "10", "max": "1001/50"}
        res = run_review(p)
        self.assertEqual(res["verdict"], "rejected")
        self.assertEqual(res["reason"], "uncovered")
        self.assertEqual(res["earliest_event_index"], 1)
        v = Fraction(res["detail"]["clock_values"]["tCool"])
        self.assertGreater(v, 20)


class TestCoolingGap(unittest.TestCase):
    def test_gap_rejected_with_earliest_event_and_guards(self):
        payload = gap_payload()
        res = run_review(payload)
        self.assertEqual(res["verdict"], "rejected")
        self.assertEqual(res["reason"], "uncovered")
        self.assertEqual(res["earliest_event_index"], 1)
        self.assertEqual(res["earliest_event"], "confirm")
        val = Fraction(res["detail"]["clock_values"]["tCool"])
        self.assertGreater(val, 20)
        self.assertLess(val, 22)
        ids = {g["transition_id"] for g in res["detail"]["blocking_guards"]}
        self.assertEqual(ids, {1, 2})
        for g in res["detail"]["blocking_guards"]:
            self.assertFalse(g["satisfied"])
            self.assertTrue(g["violations"])
        replay_witness(payload, res)

    def test_gap_closed_endpoint_is_covered(self):
        # shrinking the jitter to the closed endpoint 20 must pass
        p = gap_payload()
        p["event_sequence"][1] = {"event": "confirm", "min": "20", "max": "20"}
        res = run_review(p)
        self.assertEqual(res["verdict"], "freeze_approved")
        replay_witness(p, res)


class TestRetransmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AuditStore(os.path.join(self.tmp.name, "r.json"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_equivalent_replay(self):
        p = legal_payload()
        r1 = run_review(p)
        out1, status1 = self.store.submit(p["audit_id"],
                                          semantic_fingerprint(p), r1, p)
        self.assertEqual(status1, "new")

        p2 = json.loads(json.dumps(p))
        # resurface fractions, reorder transitions and guard entries
        p2["transitions"][1]["guard"][0] = {"clock": "tCool", "min": "20/2", "max": "40/2"}
        p2["transitions"][2], p2["transitions"][1] = p2["transitions"][1], p2["transitions"][2]
        p2["transitions"][0], p2["transitions"][1] = p2["transitions"][1], p2["transitions"][0]
        r2 = run_review(p2)
        out2, status2 = self.store.submit(p2["audit_id"],
                                          semantic_fingerprint(p2), r2, p2)
        self.assertEqual(status2, "replayed")
        self.assertEqual(out2["verdict"], out1["verdict"])
        self.assertEqual(out2["retransmission"]["outcome"],
                         "semantic_equivalent_replay")
        self.assertEqual(
            out2["witness_trajectory"][0]["event"],
            out1["witness_trajectory"][0]["event"],
        )

    def test_conflict_keeps_original(self):
        p = legal_payload()
        r1 = run_review(p)
        self.store.submit(p["audit_id"], semantic_fingerprint(p), r1, p)

        p2 = json.loads(json.dumps(p))
        p2["event_sequence"][1] = {"event": "confirm", "min": "9", "max": "25"}
        r2 = run_review(p2)
        self.assertEqual(r2["verdict"], "rejected")
        out, status = self.store.submit(p2["audit_id"],
                                        semantic_fingerprint(p2), r2, p2)
        self.assertEqual(status, "conflict")
        # original evidence preserved: original verdict was freeze approval
        self.assertEqual(out["verdict"], "freeze_approved")
        self.assertEqual(out["retransmission"]["outcome"], "content_conflict")
        # stored result is untouched as well
        self.assertEqual(self.store.get(p["audit_id"])["verdict"],
                         "freeze_approved")


class TestModelRules(unittest.TestCase):
    def test_overlapping_guards_illegal(self):
        p = gap_payload()
        p["transitions"][1]["guard"][0] = {"clock": "tCool", "min": "10", "max": "22"}
        # transitions #1 [10,22] and #2 [22,30] now overlap at closed point 22
        with self.assertRaises(ModelError) as ctx:
            build_model(p)
        self.assertIn("守卫闭区间重叠", str(ctx.exception))

    def test_open_touch_is_legal_then_gap_case(self):
        # guards [10,20) are not expressible; closed [10,20] and [22,30]
        # disjoint => model legal even though runtime gap exists
        build_model(gap_payload())  # must not raise

    def test_non_accepting_final(self):
        p = legal_payload()
        p["accepting"] = ["ARMED"]
        res = run_review(p)
        self.assertEqual(res["verdict"], "rejected")
        self.assertEqual(res["reason"], "non_accepting")
        self.assertEqual(res["detail"]["location"], "FROZEN")

    def test_reset_is_required(self):
        # without resetting tArm on confirm, the clock accumulates past the
        # confirmation timeout guard [5,14] (it lands in [15,35]) -> uncovered
        p = legal_payload()
        p["transitions"][1]["resets"] = []
        p["transitions"][2]["guard"][0] = {"clock": "tArm", "min": "5", "max": "14"}
        res = run_review(p)
        self.assertEqual(res["verdict"], "rejected")
        self.assertEqual(res["earliest_event_index"], 2)
        self.assertIn(res["reason"], ("uncovered", "no_transition"))

    def test_no_transition_for_event(self):
        p = legal_payload()
        p["events"].append("abort")
        p["event_sequence"].append({"event": "abort", "min": "1", "max": "1"})
        res = run_review(p)
        self.assertEqual(res["verdict"], "rejected")
        self.assertEqual(res["reason"], "no_transition")

    def test_float_rejected(self):
        p = legal_payload()
        p["event_sequence"][1]["min"] = 10.5
        with self.assertRaises(ModelError):
            run_review(p)
        p2 = legal_payload()
        p2["transitions"][1]["guard"][0]["min"] = 10.5
        with self.assertRaises(ModelError):
            build_model(p2)

    def test_limits_enforced(self):
        p = legal_payload()
        p["locations"] = [f"L{i}" for i in range(9)]
        with self.assertRaises(ModelError):
            build_model(p)


class TestDBMPrimitives(unittest.TestCase):
    def test_elapse_and_reset(self):
        z = DBM.zero_valuation(2)
        z = z.elapse(Fraction(5), Fraction(10))
        lo1, hi1 = z.clock_range(1)
        self.assertEqual((lo1.v, hi1.v), (5, 10))
        lo2, hi2 = z.clock_range(2)
        self.assertEqual((lo2.v, hi2.v), (5, 10))
        z2 = z.reset([1])
        lo, hi = z2.clock_range(1)
        self.assertEqual((lo.v, hi.v), (0, 0))
        lo, hi = z2.clock_range(2)
        self.assertEqual((lo.v, hi.v), (5, 10))

    def test_subtract_box_partitions(self):
        z = DBM.zero_valuation(1).elapse(Fraction(0), Fraction(30))
        parts = z.subtract_box({1: (Fraction(10), Fraction(20))})
        witness = parts[0].lex_min_witness()[0]
        self.assertTrue(witness < 10 or witness > 20)
        # covered band non-empty
        self.assertIsNotNone(z.add_box({1: (Fraction(10), Fraction(20))}).lex_min_witness())

    def test_difference_constraint_propagation(self):
        # after shared elapse both clocks stay equal
        z = DBM.zero_valuation(2).elapse(Fraction(3), Fraction(7))
        z = z.reset([2]).elapse(Fraction(1), Fraction(2))
        # x1 - x2 in [1, 6]: (3+1)-(0+2)=2 ... check feasible witness
        pt = z.lex_min_witness()
        self.assertIsNotNone(pt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
