"""Dixon-Coles goal model (also used, without the low-score correction, for corners).

log lambda_home = mu + home + attack[h] + defence[a]
log lambda_away = mu        + attack[a] + defence[h]

Fitted by time-weighted Poisson maximum likelihood (weight exp(-XI * age in days),
small L2 penalty for identifiability and for teams with few matches), then the
Dixon-Coles rho by a one-dimensional search with the rates held fixed.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.stats import poisson

XI = 0.0019     # time decay per day (Dixon & Coles' value, half-life about a year)
L2 = 0.01
MAX_GOALS = 10


@dataclass
class Fit:
    teams: dict[str, int]
    mu: float
    home: float
    attack: np.ndarray
    defence: np.ndarray
    rho: float = 0.0

    def rates(self, home: str, away: str) -> tuple[float, float]:
        h, a = self.teams[home], self.teams[away]
        lam = np.exp(self.mu + self.home + self.attack[h] + self.defence[a])
        mu = np.exp(self.mu + self.attack[a] + self.defence[h])
        return float(lam), float(mu)


def _tau(x, y, lam, mu, rho):
    t = np.ones_like(lam, dtype=float)
    t = np.where((x == 0) & (y == 0), 1 - lam * mu * rho, t)
    t = np.where((x == 0) & (y == 1), 1 + lam * rho, t)
    t = np.where((x == 1) & (y == 0), 1 + mu * rho, t)
    t = np.where((x == 1) & (y == 1), 1 - rho, t)
    return t


def fit(home: list[str], away: list[str], hg, ag, age_days, dixon_coles: bool = True) -> Fit:
    teams = {t: i for i, t in enumerate(sorted(set(home) | set(away)))}
    n = len(teams)
    hi = np.array([teams[t] for t in home])
    ai = np.array([teams[t] for t in away])
    hg, ag = np.asarray(hg, float), np.asarray(ag, float)
    w = np.exp(-XI * np.asarray(age_days, float))

    def nll(p):
        mu, h, att, dfc = p[0], p[1], p[2:2 + n], p[2 + n:]
        ll1 = mu + h + att[hi] + dfc[ai]
        ll2 = mu + att[ai] + dfc[hi]
        l1, l2 = np.exp(ll1), np.exp(ll2)
        f = -np.sum(w * (hg * ll1 - l1 + ag * ll2 - l2)) + L2 * (att @ att + dfc @ dfc)
        g1, g2 = -w * (hg - l1), -w * (ag - l2)
        grad = np.empty_like(p)
        grad[0] = g1.sum() + g2.sum()
        grad[1] = g1.sum()
        grad[2:2 + n] = np.bincount(hi, g1, n) + np.bincount(ai, g2, n) + 2 * L2 * att
        grad[2 + n:] = np.bincount(ai, g1, n) + np.bincount(hi, g2, n) + 2 * L2 * dfc
        return f, grad

    p0 = np.zeros(2 + 2 * n)
    p0[0] = np.log(max((hg.mean() + ag.mean()) / 2, 0.1))
    res = minimize(nll, p0, jac=True, method='L-BFGS-B')
    p = res.x
    out = Fit(teams, float(p[0]), float(p[1]), p[2:2 + n], p[2 + n:])
    if dixon_coles:
        l1 = np.exp(out.mu + out.home + out.attack[hi] + out.defence[ai])
        l2 = np.exp(out.mu + out.attack[ai] + out.defence[hi])
        low = (hg <= 1) & (ag <= 1)

        def rho_nll(r):
            t = _tau(hg[low], ag[low], l1[low], l2[low], r)
            return -np.sum(w[low] * np.log(np.clip(t, 1e-9, None)))

        out.rho = float(minimize_scalar(rho_nll, bounds=(-0.2, 0.2), method='bounded').x)
    return out


def score_matrix(lam: float, mu: float, rho: float = 0.0, k: int = MAX_GOALS) -> np.ndarray:
    g = np.arange(k + 1)
    m = np.outer(poisson.pmf(g, lam), poisson.pmf(g, mu))
    m[0, 0] *= 1 - lam * mu * rho
    m[0, 1] *= 1 + lam * rho
    m[1, 0] *= 1 + mu * rho
    m[1, 1] *= 1 - rho
    return m / m.sum()


def markets(m: np.ndarray) -> dict[str, float]:
    """Probabilities for the markets the signals cover, from one score matrix."""
    i, j = np.indices(m.shape)
    tot = i + j
    out = {'H': float(m[i > j].sum()), 'D': float(np.trace(m)), 'A': float(m[i < j].sum()),
           'BTTS_Y': float(m[(i > 0) & (j > 0)].sum())}
    out['BTTS_N'] = 1 - out['BTTS_Y']
    for line in (0.5, 1.5, 2.5, 3.5, 4.5):
        out[f'O{line}'] = float(m[tot > line].sum())
        out[f'U{line}'] = 1 - out[f'O{line}']
    return out


def total_over(lam: float, mu: float, line: float) -> float:
    """P(home + away > line) for independent Poisson counts (corners)."""
    return float(poisson.sf(np.floor(line), lam + mu))
