"""Difference Bounded Matrix zones over exact rationals.

A zone is the convex set of clock valuations described by difference
constraints ``x_i - x_j ~ b`` where ``~`` is ``<=`` or ``<``.  Index 0 is
the always-zero reference clock.  Every bound is an exact :class:`Fraction`
(or ``None`` for infinity) together with a strictness flag.
"""

from fractions import Fraction
from typing import Dict, List, Optional, Tuple

Num = Fraction


class Bound:
    __slots__ = ("v", "s")

    def __init__(self, v: Optional[Fraction], s: bool = False):
        self.v = v
        self.s = s

    def __eq__(self, other: "Bound") -> bool:
        return self.v == other.v and self.s == other.s

    def __lt__(self, other: "Bound") -> bool:
        if self.v is None:
            return False
        if other.v is None:
            return True
        if self.v != other.v:
            return self.v < other.v
        # equal values: strict bound is tighter
        return self.s and not other.s

    def __le__(self, other: "Bound") -> bool:
        return self == other or self < other

    def __add__(self, other: "Bound") -> "Bound":
        if self.v is None or other.v is None:
            return INF
        return Bound(self.v + other.v, self.s or other.s)

    def __neg__(self) -> "Bound":
        if self.v is None:
            return INF
        return Bound(-self.v, self.s)

    def __repr__(self) -> str:
        if self.v is None:
            return "<inf>"
        return ("<" if self.s else "<=") + str(self.v)


INF = Bound(None)
ZERO = Bound(Fraction(0), False)


class DBM:
    """Canonical zone (Floyd-Warshall closed form when ``canon`` is run)."""

    def __init__(self, clocks: int, raw: Optional[List[List[Bound]]] = None):
        self.n = clocks
        self.dim = clocks + 1
        if raw is None:
            m = [[INF] * self.dim for _ in range(self.dim)]
            for i in range(self.dim):
                m[i][i] = ZERO
            self.m = m
        else:
            self.m = raw

    # -- constructors -----------------------------------------------------

    @classmethod
    def zero_valuation(cls, clocks: int) -> "DBM":
        """All clocks equal to 0 (the initial valuation)."""
        z = cls(clocks)
        for i in range(1, z.dim):
            z.m[i][0] = ZERO
            z.m[0][i] = ZERO
        return z

    def copy(self) -> "DBM":
        return DBM(self.n, [row[:] for row in self.m])

    # -- mutation ---------------------------------------------------------

    def tighten(self, i: int, j: int, b: Bound) -> bool:
        if b < self.m[i][j]:
            self.m[i][j] = b
            return True
        return False

    def add_box(self, guard: Dict[int, Tuple[Fraction, Fraction]]) -> "DBM":
        z = self.copy()
        for d, (lo, hi) in guard.items():
            z.tighten(d, 0, Bound(hi, False))
            z.tighten(0, d, Bound(-lo, False))
        z.canon()
        return z

    def canon(self) -> "DBM":
        """Floyd-Warshall shortest-path closure."""
        m, d = self.m, self.dim
        for k in range(d):
            mk = m[k]
            for i in range(d):
                mik = m[i][k]
                if mik.v is None:
                    continue
                mi = m[i]
                for j in range(d):
                    if mk[j].v is None:
                        continue
                    cand = mik + mk[j]
                    if cand < mi[j]:
                        mi[j] = cand
        return self

    def is_empty(self) -> bool:
        for i in range(self.dim):
            if self.m[i][i] < ZERO:
                return True
        return False

    def elapse(self, lo: Fraction, hi: Fraction) -> "DBM":
        """Bounded time progression: every clock grows by delta in [lo, hi].

        With entry bounds ``L_i <= x_i <= U_i`` the image keeps the pairwise
        differences of the source zone and widens each absolute range by the
        shared delay window.
        """
        z = self.copy()
        for i in range(1, self.dim):
            upper = self.m[i][0]
            lower = self.m[0][i]  # -L_i
            if upper.v is not None:
                z.m[i][0] = Bound(upper.v + hi, upper.s)
            if lower.v is not None:
                z.m[0][i] = Bound(lower.v - lo, lower.s)
        return z.canon()

    def reset(self, clocks) -> "DBM":
        """Reset the given clock indices (1-based) to zero."""
        s = self.m
        z = self.copy()
        for i in clocks:
            z.m[i][0] = ZERO
            z.m[0][i] = ZERO
            for j in range(1, self.dim):
                if j == i:
                    continue
                z.m[i][j] = s[0][j]
                z.m[j][i] = s[j][0]
        return z.canon()

    def fix(self, idx: int, value: Fraction) -> "DBM":
        z = self.copy()
        z.tighten(idx, 0, Bound(value, False))
        z.tighten(0, idx, Bound(-value, False))
        return z.canon()

    # -- witnesses --------------------------------------------------------

    def clock_range(self, i: int) -> Tuple[Bound, Bound]:
        """Return (lower, upper) bounds on x_i."""
        # x0 - xi <= m[0][i]  =>  xi >= -m[0][i]
        lo = -self.m[0][i]
        hi = self.m[i][0]
        return lo, hi

    def lex_min_witness(self) -> Optional[List[Fraction]]:
        """Smallest lexicographic rational point in the zone.

        Zones produced by the verifier are bounded in every clock dimension;
        when the minimum is an unattained strict face we take the midpoint
        with the (finite) upper bound, which is always a valid substitute.
        """
        if self.is_empty():
            return None
        z = self.copy()
        point: List[Fraction] = []
        for i in range(1, self.dim):
            lb, ub = z.clock_range(i)
            if lb.v is None:
                v = Fraction(0)
            elif not lb.s:
                v = lb.v
            else:
                if ub.v is None:
                    return None  # uncovered open face without finite upper bound
                v = (lb.v + ub.v) / 2
            point.append(v)
            z = z.fix(i, v)
            if z.is_empty():
                return None
        return point

    # -- set difference against a guard box -------------------------------

    def intersect_box(
        self, guard: Dict[int, Tuple[Fraction, Fraction]]
    ) -> Optional["DBM"]:
        z = self.add_box(guard)
        return None if z.is_empty() else z

    def subtract_box(
        self, guard: Dict[int, Tuple[Fraction, Fraction]]
    ) -> List["DBM"]:
        """Partition ``self \\ guard`` into at most ``2*len(guard)`` zones."""
        inside = self.add_box(guard)
        if inside.is_empty():
            return [self]
        core = self.copy()
        outside: List[DBM] = []
        for d, (lo, hi) in guard.items():
            low = core.copy()
            low.tighten(d, 0, Bound(lo, True))  # x_d < lo
            low.canon()
            if not low.is_empty():
                outside.append(low)
            high = core.copy()
            high.tighten(0, d, Bound(-hi, True))  # x_d > hi
            high.canon()
            if not high.is_empty():
                outside.append(high)
            core.tighten(d, 0, Bound(hi, False))
            core.tighten(0, d, Bound(-lo, False))
            core.canon()
            if core.is_empty():
                break
        return outside

    # -- rendering --------------------------------------------------------

    def view(self, names: List[str]) -> dict:
        clocks = {}
        for i in range(1, self.dim):
            lb, ub = self.clock_range(i)
            clocks[names[i - 1]] = {
                "min": None if lb.v is None else str(lb.v),
                "min_strict": lb.s,
                "max": None if ub.v is None else str(ub.v),
                "max_strict": ub.s,
            }
        labels = ["0"] + names
        constraints = []
        for i in range(self.dim):
            for j in range(self.dim):
                if i == j or self.m[i][j].v is None:
                    continue
                b = self.m[i][j]
                constraints.append(
                    f"{labels[i]} - {labels[j]} {'<' if b.s else '<='} {b.v}"
                )
        return {"clocks": clocks, "constraints": constraints}


def nonnegative_zone(clocks: int) -> DBM:
    """Zone where every clock satisfies x_i >= 0."""
    z = DBM(clocks)
    for i in range(1, z.dim):
        z.m[0][i] = ZERO
    return z


def guard_intersection_witness(
    clocks: int,
    g1: Dict[int, Tuple[Fraction, Fraction]],
    g2: Dict[int, Tuple[Fraction, Fraction]],
) -> Optional[List[Fraction]]:
    """Lex-smallest valuation satisfying both guard boxes, if any.

    Clocks absent from a guard are unconstrained except for nonnegativity.
    """
    z = nonnegative_zone(clocks)
    z = z.add_box(g1).add_box(g2)
    if z.is_empty():
        return None
    return z.lex_min_witness()


def solve_delay(
    pre: DBM,
    point: List[Fraction],
    lo: Fraction,
    hi: Fraction,
) -> Tuple[Fraction, List[Fraction], List[Fraction]]:
    """Find a concrete delta in [lo, hi] carrying pre-zone to ``point``.

    Returns ``(delta, clocks_before, clocks_at_event)``; the witness point is
    known to lie in ``pre.elapse(lo, hi)`` so an interval always exists.
    """
    d_lo, d_hi, lo_strict, hi_strict = lo, hi, False, False

    def raise_lower(v: Fraction, strict: bool):
        nonlocal d_lo, lo_strict
        if v > d_lo or (v == d_lo and strict and not lo_strict):
            d_lo, lo_strict = v, strict

    def lower_upper(v: Fraction, strict: bool):
        nonlocal d_hi, hi_strict
        if v < d_hi or (v == d_hi and strict and not hi_strict):
            d_hi, hi_strict = v, strict

    m = pre.m
    for i in range(pre.dim):
        for j in range(pre.dim):
            b = m[i][j]
            if b.v is None or i == j:
                continue
            # x_k = v_k - delta (x0 == 0)
            if i >= 1 and j >= 1:
                expr = point[i - 1] - point[j - 1]
                if b.s:
                    assert expr < b.v, (expr, b)
                else:
                    assert expr <= b.v, (expr, b)
            elif i >= 1 and j == 0:
                # v_i - delta <= b  =>  delta >= v_i - b
                raise_lower(point[i - 1] - b.v, b.s)
            else:  # i == 0, j >= 1: delta - v_j <= b => delta <= v_j + b
                lower_upper(point[j - 1] + b.v, b.s)

    if not lo_strict:
        delta = d_lo
    else:
        delta = (d_lo + d_hi) / 2
    before = [v - delta for v in point]
    return delta, before, point
