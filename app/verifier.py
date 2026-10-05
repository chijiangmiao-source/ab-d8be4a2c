"""Exact-rational zone propagation verifier.

For every event (in arrival order) the verifier

1. advances time by the event's closed relative-time interval,
2. splits each possible zone by every outgoing transition guard,
3. executes clock resets,

and approves a freeze only when every possible trajectory takes exactly one
transition at every event and ends in an accepting location.
"""

from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .dbm import DBM, solve_delay
from .model import Model, ModelError, Transition, build_model

VERDICT_FREEZE = "freeze_approved"
VERDICT_REJECTED = "rejected"


@dataclass
class Fragment:
    """One possible region after processing an event."""

    location: str
    zone: DBM
    event_index: Optional[int]
    parent: Optional["Fragment"]
    transition: Optional[Transition]
    elapse_zone: Optional[DBM] = None
    covered_zone: Optional[DBM] = None


@dataclass
class EventItem:
    name: str
    lo: Fraction
    hi: Fraction


def parse_events(model: Model, raw: list) -> List[EventItem]:
    if not isinstance(raw, list) or not raw:
        raise ModelError("事件序列必须为非空列表")
    if len(raw) > 32:
        raise ModelError(f"事件不得超过 32 项，实际 {len(raw)}")
    items = []
    for i, e in enumerate(raw):
        try:
            name = e["event"]
            raw_lo, raw_hi = e["min"], e["max"]
            if isinstance(raw_lo, float) or isinstance(raw_hi, float):
                raise ValueError
            lo = Fraction(str(raw_lo))
            hi = Fraction(str(raw_hi))
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            raise ModelError(
                f"事件 #{i} 格式非法：需要 event/min/max（有理数，禁止浮点）"
            )
        if name not in model.events:
            raise ModelError(f"事件 #{i} 的事件类型 {name!r} 未在模型 events 中声明")
        if lo > hi:
            raise ModelError(f"事件 #{i} 的相对时刻下界 {lo} 大于上界 {hi}")
        if lo < 0:
            raise ModelError(f"事件 #{i} 的相对时刻不能为负（时间不可倒流）: {lo}")
        items.append(EventItem(name, lo, hi))
    return items


def _clock_values(point: List[Fraction], names: List[str]) -> Dict[str, str]:
    return {names[i]: str(point[i]) for i in range(len(names))}


def _blocking_guards(
    point: List[Fraction], candidates: List[Transition], names: List[str]
) -> List[dict]:
    report = []
    for t in candidates:
        violations = []
        for d, (lo, hi) in t.guard.items():
            v = point[d - 1]
            if v < lo:
                violations.append(
                    {"clock": names[d - 1], "value": str(v),
                     "bound_min": str(lo), "bound_max": str(hi),
                     "reason": "below_min"}
                )
            elif v > hi:
                violations.append(
                    {"clock": names[d - 1], "value": str(v),
                     "bound_min": str(lo), "bound_max": str(hi),
                     "reason": "above_max"}
                )
        report.append({
            "transition_id": t.tid,
            "source": t.source,
            "target": t.target,
            "guard": t.guard_view(names),
            "resets": [names[d - 1] for d in t.resets],
            "violations": violations,
            "satisfied": not violations,
        })
    return report


def _reconstruct(
    frag: Optional[Fragment],
    point: List[Fraction],
    items: List[EventItem],
    names: List[str],
    stall_event_index: Optional[int] = None,
) -> List[dict]:
    """Back-propagate a concrete clock point to a full trajectory.

    ``stall_event_index`` marks an event at which time was advanced but no
    transition could be taken (uncovered / no transition); ``point`` then is
    a valuation in the post-elapse region of ``frag``.
    """
    traj: List[dict] = []
    cur: Optional[Fragment] = frag
    q = point
    if stall_event_index is not None and cur is not None:
        item = items[stall_event_index]
        delta, before, _ = solve_delay(cur.zone, q, item.lo, item.hi)
        traj.append({
            "event_index": stall_event_index,
            "event": item.name,
            "relative_delay": str(delta),
            "interval": {"min": str(item.lo), "max": str(item.hi)},
            "location_before": cur.location,
            "stalled": True,
            "clocks_before": _clock_values(before, names),
            "clocks_at_event": _clock_values(q, names),
        })
        q = before
    while cur is not None and cur.transition is not None:
        item = items[cur.event_index]
        t = cur.transition
        z = cur.covered_zone.copy()
        # non-reset clocks keep the post-reset value; choose reset clocks
        for d in range(1, z.dim):
            if d not in t.resets:
                z = z.fix(d, q[d - 1])
        at_point = z.lex_min_witness()
        pre_zone = (
            cur.parent.zone if cur.parent is not None
            else DBM.zero_valuation(len(names))
        )
        delta, before, _ = solve_delay(pre_zone, at_point, item.lo, item.hi)
        traj.append({
            "event_index": cur.event_index,
            "event": item.name,
            "relative_delay": str(delta),
            "interval": {"min": str(item.lo), "max": str(item.hi)},
            "location_before": t.source,
            "transition_id": t.tid,
            "location_after": t.target,
            "clocks_before": _clock_values(before, names),
            "clocks_at_event": _clock_values(at_point, names),
            "clocks_after_reset": _clock_values(q, names),
        })
        q = before
        cur = cur.parent
    traj.reverse()
    return traj


def _zone_ranges(z: DBM, names: List[str]) -> Dict[str, dict]:
    return z.view(names)["clocks"]


def review(model: Model, items: List[EventItem]) -> dict:
    names = model.clock_names
    live: List[Fragment] = [
        Fragment(model.initial, DBM.zero_valuation(model.clocks), None, None, None)
    ]
    evidence: List[dict] = []

    def failure(
        kind: str, index: int, frag: Fragment, point, detail,
        stall: bool = False,
    ) -> dict:
        return {
            "verdict": VERDICT_REJECTED,
            "reason": kind,
            "earliest_event_index": index,
            "earliest_event": items[index].name,
            "detail": detail,
            "evidence": evidence,
            "witness_trajectory": _reconstruct(
                frag, point, items, names,
                stall_event_index=index if stall else None,
            ),
        }

    for i, item in enumerate(items):
        step = {
            "event_index": i,
            "event": item.name,
            "relative_interval": {"min": str(item.lo), "max": str(item.hi)},
            "fragments": [],
        }
        next_live: List[Fragment] = []
        live.sort(key=lambda f: f.location)
        failure_result = None
        for frag in live:
            ez = frag.zone.elapse(item.lo, item.hi)
            candidates = model.outgoing(frag.location, item.name)

            frag_evidence = {
                "location_before": frag.location,
                "region_before": _zone_ranges(frag.zone, names),
                "region_after_elapse": _zone_ranges(ez, names),
            }

            if ez.is_empty():
                step["fragments"].append(frag_evidence)
                evidence.append(step)
                failure_result = failure(
                    "empty_region", i, frag, frag.zone.lex_min_witness(),
                    {"message": "时间推进后可能区域为空"},
                )
                break

            if not candidates:
                point = ez.lex_min_witness()
                checked = _blocking_guards(point, candidates, names)
                frag_evidence["uncovered_region"] = _zone_ranges(ez, names)
                step["fragments"].append(frag_evidence)
                evidence.append(step)
                failure_result = failure(
                    "no_transition", i, frag, point,
                    {
                        "message": f"位置 {frag.location} 上事件 {item.name} 无可用迁移",
                        "location": frag.location,
                        "clock_values": _clock_values(point, names),
                        "blocking_guards": checked,
                    },
                    stall=True,
                )
                break

            # guard splitting: pairwise-disjoint closed boxes
            remainder: List[DBM] = [ez]
            hits: List[Tuple[Transition, List[DBM]]] = []
            for t in sorted(candidates, key=lambda x: x.tid):
                hit_parts: List[DBM] = []
                new_remainder: List[DBM] = []
                for rz in remainder:
                    inter = rz.intersect_box(t.guard)
                    if inter is not None:
                        hit_parts.append(inter)
                    new_remainder.extend(rz.subtract_box(t.guard))
                remainder = new_remainder
                if hit_parts:
                    hits.append((t, hit_parts))

            frag_evidence["enabled"] = [
                {
                    "transition_id": t.tid,
                    "target": t.target,
                    "guard": t.guard_view(names),
                    "resets": [names[d - 1] for d in t.resets],
                    "covered_parts": len(parts),
                }
                for t, parts in hits
            ]

            if remainder:
                point = remainder[0].lex_min_witness()
                checked = _blocking_guards(point, candidates, names)
                frag_evidence["uncovered_region"] = _zone_ranges(
                    remainder[0], names
                )
                step["fragments"].append(frag_evidence)
                evidence.append(step)
                failure_result = failure(
                    "uncovered", i, frag, point,
                    {
                        "message": "存在未被任何迁移闭区间守卫覆盖的可能时刻",
                        "location": frag.location,
                        "clock_values": _clock_values(point, names),
                        "blocking_guards": checked,
                    },
                    stall=True,
                )
                break

            for t, parts in hits:
                for part in parts:
                    post = part.reset(t.resets)
                    next_live.append(
                        Fragment(t.target, post, i, frag, t,
                                 elapse_zone=ez, covered_zone=part)
                    )
                    frag_evidence.setdefault("taken", []).append({
                        "transition_id": t.tid,
                        "target": t.target,
                        "region_covered": _zone_ranges(part, names),
                        "region_after_reset": _zone_ranges(post, names),
                        "resets": [names[d - 1] for d in t.resets],
                    })
            step["fragments"].append(frag_evidence)

        if failure_result is not None:
            return failure_result
        live = next_live
        evidence.append(step)

    accepting = model.accepting_set()
    live.sort(key=lambda f: f.location)
    for frag in live:
        if frag.location not in accepting:
            point = frag.zone.lex_min_witness()
            return {
                "verdict": VERDICT_REJECTED,
                "reason": "non_accepting",
                "earliest_event_index": len(items) - 1,
                "earliest_event": items[-1].name,
                "detail": {
                    "message": "事件流结束后存在未抵达终态的可能轨迹",
                    "location": frag.location,
                    "accepting": sorted(accepting),
                    "clock_values": _clock_values(point, names),
                },
                "evidence": evidence,
                "witness_trajectory": _reconstruct(frag, point, items, names),
            }

    frag = live[0]
    point = frag.zone.lex_min_witness()
    return {
        "verdict": VERDICT_FREEZE,
        "reason": "all_trajectories_deterministic_and_accepting",
        "final_locations": sorted({f.location for f in live}),
        "detail": {
            "message": "全部可能轨迹在每个事件上恰有一条迁移且最终抵达终态",
        },
        "evidence": evidence,
        "witness_trajectory": _reconstruct(frag, point, items, names),
    }


def run_review(payload: dict) -> dict:
    """Validate the model and run the review; raises ModelError on bad input."""
    model = build_model(payload)
    items = parse_events(model, payload.get("event_sequence", []))
    result = review(model, items)
    result["audit_id"] = model.audit_id
    result["model_summary"] = {
        "locations": model.locations,
        "initial": model.initial,
        "accepting": model.accepting,
        "clocks": model.clock_names,
        "transitions": len(model.transitions),
        "events": [e.name for e in items],
    }
    return result


def illegal_model_result(audit_id: Optional[str], err: Exception) -> dict:
    return {
        "verdict": VERDICT_REJECTED,
        "reason": "illegal_model",
        "earliest_event_index": None,
        "earliest_event": None,
        "audit_id": audit_id,
        "detail": {"message": str(err)},
        "evidence": [],
        "witness_trajectory": [],
    }
