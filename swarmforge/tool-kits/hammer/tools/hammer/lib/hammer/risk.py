"""Valutazione del rischio secondo la Risk Policy di Johner RM-TPL-05.

Probabilità totale Pt = P1 × P2 × P3; la categoria di probabilità dipende da
soglie su Pt; la classe è "P<categoria>S<gravità>"; l'accettabilità si legge
dalla matrice. Il rischio residuo usa Pt × fattore di riduzione.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .errors import HammerError

CATEGORIES = 5


@dataclass(frozen=True)
class RiskPolicy:
    thresholds: tuple[float, ...]          # inizio delle categorie 2..5 su Pt
    probability_labels: tuple[str, ...]    # categorie 1..5
    severity_labels: tuple[str, ...]       # gravità 1..5
    acceptable: dict[int, tuple[bool, ...]]  # categoria -> accettabile per gravità 1..5

    def category(self, probability: float) -> int:
        return 1 + sum(probability >= threshold for threshold in self.thresholds)

    def is_acceptable(self, category: int, severity: int) -> bool:
        return self.acceptable[category][severity - 1]


def thresholds_from_uses(uses_over_lifetime: float, frequent_from: float) -> tuple[float, ...]:
    """Soglie equidistanti in scala logaritmica fra 1/usi e frequent_from (RM-TPL-05)."""
    lower = 1 / uses_over_lifetime
    if not 0 < lower < frequent_from <= 1:
        raise HammerError("risk: serve 1/uses_over_lifetime < frequent_from <= 1")
    step = (math.log10(frequent_from) - math.log10(lower)) / (CATEGORIES - 2)
    return tuple(lower * 10 ** (step * i) for i in range(CATEGORIES - 1))


def parse_risk_policy(data: dict, source: str) -> RiskPolicy:
    where = f"{source}: [risk]"
    if "probability_thresholds" in data:
        thresholds = tuple(float(t) for t in data["probability_thresholds"])
    elif "uses_over_lifetime" in data and "frequent_from" in data:
        thresholds = thresholds_from_uses(float(data["uses_over_lifetime"]), float(data["frequent_from"]))
    else:
        raise HammerError(f"{where}: servono uses_over_lifetime e frequent_from, oppure probability_thresholds")
    if len(thresholds) != CATEGORIES - 1 or list(thresholds) != sorted(thresholds):
        raise HammerError(f"{where}: servono {CATEGORIES - 1} soglie crescenti")

    probability = tuple(data.get("probability", ()))
    severity = tuple(data.get("severity", ()))
    if len(probability) != CATEGORIES or len(severity) != CATEGORIES:
        raise HammerError(f"{where}: probability e severity devono avere {CATEGORIES} voci")

    acceptable: dict[int, tuple[bool, ...]] = {}
    for category in range(1, CATEGORIES + 1):
        row = data.get("acceptable", {}).get(f"P{category}")
        if row is None or len(row) != CATEGORIES or not all(isinstance(v, bool) for v in row):
            raise HammerError(f"{where}: acceptable.P{category} deve avere {CATEGORIES} valori true/false")
        acceptable[category] = tuple(row)
    return RiskPolicy(thresholds, probability, severity, acceptable)


@dataclass(frozen=True)
class Assessment:
    total_probability: float
    category: int
    severity: int
    acceptable: bool

    @property
    def risk_class(self) -> str:
        return f"P{self.category}S{self.severity}"

    def as_dict(self, policy: RiskPolicy) -> dict:
        return {
            "total_probability": self.total_probability,
            "probability": f"{self.category} - {policy.probability_labels[self.category - 1]}",
            "severity": f"{self.severity} - {policy.severity_labels[self.severity - 1]}",
            "risk_class": self.risk_class,
            "acceptable": self.acceptable,
        }


@dataclass(frozen=True)
class RiskEvaluation:
    initial: Assessment
    residual: Assessment | None   # None se non è indicato un fattore di riduzione

    def as_dict(self, policy: RiskPolicy) -> dict:
        return {
            "initial": self.initial.as_dict(policy),
            "residual": self.residual.as_dict(policy) if self.residual else None,
        }


def _assess(policy: RiskPolicy, probability: float, severity: int) -> Assessment:
    category = policy.category(probability)
    return Assessment(probability, category, severity, policy.is_acceptable(category, severity))


def evaluate(policy: RiskPolicy, fields: dict) -> RiskEvaluation | None:
    """Valuta un elemento RISK; None se mancano P1, P2, P3 o la gravità o non sono validi."""
    try:
        p1, p2, p3 = (float(fields[name]) for name in ("p1", "p2", "p3"))
        severity = int(fields["severity"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (all(0 <= p <= 1 for p in (p1, p2, p3)) and 1 <= severity <= CATEGORIES):
        return None
    total = p1 * p2 * p3
    initial = _assess(policy, total, severity)

    residual = None
    if fields.get("reduction_factor") is not None:
        try:
            factor = float(fields["reduction_factor"])
            residual_severity = int(fields.get("residual_severity", severity))
        except (TypeError, ValueError):
            factor, residual_severity = -1.0, 0
        if 0 <= factor <= 1 and 1 <= residual_severity <= CATEGORIES:
            residual = _assess(policy, total * factor, residual_severity)
    return RiskEvaluation(initial, residual)
