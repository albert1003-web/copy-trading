"""Small statistics shared by the analytics stages."""

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class Sample:
    n: int
    mean: float
    var: float  # sample variance (0 when n < 2)


def sample(values: list[float]) -> Sample:
    return Sample(len(values), statistics.fmean(values), statistics.variance(values) if len(values) > 1 else 0.0)


def shrink(groups: dict[str, Sample]) -> dict[str, float]:
    """Empirical-Bayes means: each group's mean pulled toward the pooled mean by its noise.

    shrunk = mu + tau2 / (tau2 + s2 / n) * (mean - mu), where mu is the pooled mean, s2 the pooled within-group
    variance, and tau2 the between-group variance (method of moments, floored at 0). A group of 3 lucky trades
    lands near mu; a group of hundreds keeps most of its own mean. One group (or none) is returned as is."""
    if len(groups) < 2:
        return {key: g.mean for key, g in groups.items()}
    total = sum(g.n for g in groups.values())
    mu = sum(g.n * g.mean for g in groups.values()) / total
    dof = sum(g.n - 1 for g in groups.values())
    s2 = sum((g.n - 1) * g.var for g in groups.values()) / dof if dof > 0 else 0.0
    means = [g.mean for g in groups.values()]
    tau2 = max(statistics.variance(means) - statistics.fmean(s2 / g.n for g in groups.values()), 0.0)
    shrunk = {}
    for key, g in groups.items():
        noise = s2 / g.n
        weight = tau2 / (tau2 + noise) if tau2 + noise > 0 else 1.0
        shrunk[key] = mu + weight * (g.mean - mu)
    return shrunk
