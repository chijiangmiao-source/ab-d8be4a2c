"""Timed-automaton model for the propellant isolation interlock review."""

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Optional, Tuple

from .dbm import guard_intersection_witness


@dataclass
class Transition:
    tid: int
    source: str
    target: str
    event: str
    guard: Dict[int, Tuple[Fraction, Fraction]]  # clock index -> [lo, hi]
    resets: List[int]

    def guard_view(self, clock_names: List[str]) -> dict:
        out = {}
        for d, (lo, hi) in self.guard.items():
            out[clock_names[d - 1]] = {"min": str(lo), "max": str(hi)}
        return out


@dataclass
class Model:
    audit_id: str
    locations: List[str]
    initial: str
    accepting: List[str]
    clock_names: List[str]
    events: List[str]
    transitions: List[Transition]
    errors: List[str] = field(default_factory=list)

    @property
    def clocks(self) -> int:
        return len(self.clock_names)

    def outgoing(self, loc: str, event: str) -> List[Transition]:
        return [
            t for t in self.transitions
            if t.source == loc and t.event == event
        ]

    def accepting_set(self) -> set:
        return set(self.accepting)


def _frac(x, what: str) -> Fraction:
    try:
        if isinstance(x, bool):
            raise ValueError
        if isinstance(x, int):
            return Fraction(x)
        if isinstance(x, float):
            raise ValueError
        return Fraction(str(x))
    except (ValueError, ZeroDivisionError, TypeError):
        raise ModelError(f"{what} 不是合法有理数（禁止浮点，请用整数或分数字符串）: {x!r}")


class ModelError(Exception):
    pass


MAX_LOCATIONS = 8
MAX_CLOCKS = 4
MAX_TRANSITIONS = 16
MAX_EVENTS = 32


def build_model(payload: dict) -> Model:
    errors: List[str] = []

    def need(key, typ):
        v = payload.get(key)
        if not isinstance(v, typ):
            raise ModelError(f"字段 {key!r} 缺失或类型错误（应为 {typ.__name__}）")
        return v

    audit_id = need("audit_id", str)
    if not audit_id.strip():
        raise ModelError("audit_id 不能为空")
    locations = need("locations", list)
    clock_names = need("clocks", list)
    raw_events = need("events", list)
    raw_trans = need("transitions", list)

    if not (1 <= len(locations) <= MAX_LOCATIONS):
        errors.append(f"位置数量必须在 1..{MAX_LOCATIONS} 之间，实际 {len(locations)}")
    if len(set(locations)) != len(locations):
        errors.append("位置名称存在重复")
    if not (1 <= len(clock_names) <= MAX_CLOCKS):
        errors.append(f"时钟数量必须在 1..{MAX_CLOCKS} 之间，实际 {len(clock_names)}")
    if len(set(clock_names)) != len(clock_names):
        errors.append("时钟名称存在重复")
    if not (1 <= len(raw_events) <= MAX_EVENTS):
        errors.append(f"事件流必须在 1..{MAX_EVENTS} 项之间，实际 {len(raw_events)}")
    if len(raw_trans) > MAX_TRANSITIONS:
        errors.append(f"迁移数量不得超过 {MAX_TRANSITIONS}，实际 {len(raw_trans)}")
    if len(set(raw_events)) != len(raw_events):
        errors.append("事件流中存在重复事件类型")

    initial = payload.get("initial")
    if initial not in locations:
        errors.append(f"初始位置 {initial!r} 不在位置集合中")
    accepting = payload.get("accepting", [])
    if not isinstance(accepting, list) or not accepting:
        errors.append("accepting 必须为非空列表")
    else:
        for loc in accepting:
            if loc not in locations:
                errors.append(f"终态 {loc!r} 不在位置集合中")

    loc_set, clock_set = set(locations), set(clock_names)
    event_set = set(raw_events)

    transitions: List[Transition] = []
    for idx, rt in enumerate(raw_trans):
        prefix = f"迁移 #{idx}"
        try:
            src = rt["source"]
            dst = rt["target"]
            ev = rt["event"]
        except (KeyError, TypeError):
            errors.append(f"{prefix}: 缺少 source/target/event")
            continue
        if src not in loc_set:
            errors.append(f"{prefix}: 源位置 {src!r} 不存在")
        if dst not in loc_set:
            errors.append(f"{prefix}: 目标位置 {dst!r} 不存在")
        if ev not in event_set:
            errors.append(f"{prefix}: 事件 {ev!r} 不在事件流中")
        guard: Dict[int, Tuple[Fraction, Fraction]] = {}
        for g in rt.get("guard", []) or []:
            try:
                cname, lo_s, hi_s = g["clock"], g["min"], g["max"]
            except (KeyError, TypeError):
                errors.append(f"{prefix}: 守卫项必须含 clock/min/max")
                continue
            if cname not in clock_set:
                errors.append(f"{prefix}: 守卫引用未知时钟 {cname!r}")
                continue
            ci = clock_names.index(cname) + 1
            if ci in guard:
                errors.append(f"{prefix}: 时钟 {cname} 的守卫重复出现")
                continue
            lo, hi = _frac(lo_s, f"{prefix} 守卫 {cname} min"), _frac(
                hi_s, f"{prefix} 守卫 {cname} max"
            )
            if lo > hi:
                errors.append(f"{prefix}: 守卫 {cname} 下界 {lo} 大于上界 {hi}")
            if lo < 0:
                errors.append(f"{prefix}: 守卫 {cname} 下界不能为负: {lo}")
            guard[ci] = (lo, hi)
        resets = []
        for cname in rt.get("resets", []) or []:
            if cname not in clock_set:
                errors.append(f"{prefix}: 复位引用未知时钟 {cname!r}")
                continue
            resets.append(clock_names.index(cname) + 1)
        transitions.append(
            Transition(idx, src, dst, ev, guard, sorted(set(resets)))
        )

    if errors:
        raise ModelError("；".join(errors))

    model = Model(
        audit_id=audit_id,
        locations=locations,
        initial=initial,
        accepting=accepting,
        clock_names=clock_names,
        events=list(raw_events),
        transitions=transitions,
    )
    _check_guard_disjointness(model)
    return model


def _check_guard_disjointness(model: Model) -> None:
    """Guards of transitions on the same location+event must not overlap."""
    groups: Dict[Tuple[str, str], List[Transition]] = {}
    for t in model.transitions:
        groups.setdefault((t.source, t.event), []).append(t)
    errors: List[str] = []
    for (loc, ev), ts in groups.items():
        for a in range(len(ts)):
            for b in range(a + 1, len(ts)):
                t1, t2 = ts[a], ts[b]
                # unspecified clocks are unconstrained; feed partial guards
                wit = guard_intersection_witness(
                    model.clocks, t1.guard, t2.guard
                )
                if wit is not None:
                    vals = {
                        model.clock_names[d - 1]: str(wit[d - 1])
                        for d in sorted(set(t1.guard) | set(t2.guard))
                    }
                    errors.append(
                        f"位置 {loc!r} 事件 {ev!r} 的迁移 #{t1.tid} 与 #{t2.tid} "
                        f"守卫闭区间重叠，反例时钟取值 {vals}"
                    )
    if errors:
        raise ModelError("；".join(errors))
