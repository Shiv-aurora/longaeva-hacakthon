"""Numpy-only statistics for the lab clocks: regression, HAC, bootstrap, scoring, Sharpe, event study.

Conventions: arrays in, dataclasses or dicts out; axis 0 is time or observation.
Non-finite inputs raise unless a function says it drops them. Every random draw
comes from the caller's seed (an int or a numpy Generator); nothing is seeded
implicitly. Lags and windows are positional, so put series on a regular grid
first. Sharpe functions take per-period (daily) excess returns and annualise by
sqrt(periods_per_year). Loss functions return one loss per target unless the
name says otherwise (mae, brier_score, coverage are means).

Sources, one line per formula:
- OLS coefficients, s^2 (X'X)^-1 covariance, R-squared: standard least squares (Greene, 2012).
- HC0 heteroskedasticity-consistent covariance: White (1980).
- HAC covariance with Bartlett weights 1 - j/(L+1): Newey and West (1987).
- Default lag floor(4 (n/100)^(2/9)): rule of thumb commonly attributed to Newey and West (1994).
- Standard error of a linear combination, sqrt(w'Vw): delta method, exact for linear functions (Greene, 2012).
- Moving-block bootstrap: Kunsch (1989).
- Pairs (case-resampling) bootstrap: Efron (1979); Freedman (1981) for regression.
- Percentile interval: Efron and Tibshirani (1993).
- CRPS: Matheson and Winkler (1976); kernel form E|X-y| - E|X-X'|/2: Gneiting and Raftery (2007).
- Fair (unbiased in ensemble size) CRPS estimator: Ferro (2014).
- Closed-form CRPS of a normal forecast: Gneiting, Raftery, Westveld and Goldman (2005).
- Pinball (quantile) loss: Koenker and Bassett (1978).
- Brier score: Brier (1950).
- Sharpe ratio standard error under iid normal returns: Lo (2002); skewness and kurtosis terms: Mertens (2002).
- Probabilistic Sharpe ratio: Bailey and Lopez de Prado (2012).
- Deflated Sharpe ratio and expected maximum Sharpe over N trials: Bailey and Lopez de Prado (2014).
- Normal CDF: erfc identity. Inverse normal CDF: Acklam's rational approximation (web note, about 2003;
  stated relative error 1.15e-9) followed by two Halley steps on the erfc-based CDF.
- Student t CDF from the regularised incomplete beta continued fraction: Press, Teukolsky, Vetterling and Flannery (1992).
- Market-model abnormal returns and cumulative abnormal returns: Brown and Warner (1985); MacKinlay (1997).
- Quantiles: linear interpolation, type 7 of Hyndman and Fan (1996), the numpy default.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

EULER_GAMMA = 0.5772156649015329
OVERLAP_RULES = ("flag", "drop_later", "drop_all")
_SQRT2, _SQRT2PI = math.sqrt(2.0), math.sqrt(2.0 * math.pi)


def _rng(seed) -> np.random.Generator:
    if isinstance(seed, np.random.Generator):
        return seed
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)):
        raise ValueError("seed must be an int or a numpy Generator")
    return np.random.default_rng(int(seed))


def _vector(values, name: str) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    if v.ndim != 1 or not len(v):
        raise ValueError(f"{name} must be a non-empty 1-D array")
    if not np.isfinite(v).all():
        raise ValueError(f"{name} contains non-finite values")
    return v


def _count(value, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return int(value)


# ---------------------------------------------------------------- distributions

_erfc = np.vectorize(math.erfc, otypes=[float])


def norm_pdf(x):
    z = np.asarray(x, dtype=float)
    out = np.exp(-0.5 * z * z) / _SQRT2PI
    return float(out) if out.ndim == 0 else out


def norm_cdf(x):
    z = np.asarray(x, dtype=float)
    out = 0.5 * _erfc(-z / _SQRT2)
    return float(out) if out.ndim == 0 else out


_ACKLAM_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
    1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_ACKLAM_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
    6.680131188771972e+01, -1.328068155288572e+01)
_ACKLAM_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
    -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_ACKLAM_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00)


def _horner(coefficients, x: float) -> float:
    total = 0.0
    for c in coefficients:
        total = total * x + c
    return total


def _acklam(p: float) -> float:
    """Unrefined rational approximation; relative error about 1e-9."""
    low = 0.02425
    if p < low:
        q = math.sqrt(-2.0 * math.log(p))
        return _horner(_ACKLAM_C, q) / _horner(_ACKLAM_D + (1.0,), q)
    if p > 1.0 - low:
        q = math.sqrt(-2.0 * math.log1p(-p))
        return -_horner(_ACKLAM_C, q) / _horner(_ACKLAM_D + (1.0,), q)
    q = p - 0.5
    r = q * q
    return _horner(_ACKLAM_A, r) * q / _horner(_ACKLAM_B + (1.0,), r)


def _norm_ppf(p: float) -> float:
    if not 0.0 <= p <= 1.0:
        raise ValueError("Probability must lie in [0, 1]")
    if p in (0.0, 1.0):
        return -math.inf if p == 0.0 else math.inf
    x = _acklam(p)
    for _ in range(2):
        if abs(x) > 37.0:
            break
        u = (0.5 * math.erfc(-x / _SQRT2) - p) * _SQRT2PI * math.exp(0.5 * x * x)
        x -= u / (1.0 + 0.5 * x * u)
    return x


_norm_ppf_vec = np.vectorize(_norm_ppf, otypes=[float])


def norm_ppf(p):
    out = _norm_ppf_vec(np.asarray(p, dtype=float))
    return float(out) if out.ndim == 0 else out


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c
            c = c if abs(c) > tiny else tiny
            delta = d * c
            h *= delta
        if abs(delta - 1.0) < 3e-16:
            return h
    raise ValueError("Incomplete beta continued fraction did not converge")


def _betainc(a: float, b: float, x: float) -> float:
    if x <= 0.0 or x >= 1.0:
        return 0.0 if x <= 0.0 else 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_tail(t: float, df: float) -> float:
    """Upper-tail probability P(T > |t|)."""
    if not df > 0 or math.isnan(t):
        raise ValueError("Student t needs df > 0 and a number")
    if math.isinf(t):
        return 0.0
    return 0.5 * _betainc(0.5 * df, 0.5, df / (df + t * t))


def t_cdf(t: float, df: float) -> float:
    tail = _t_tail(float(t), float(df))
    return 1.0 - tail if t >= 0 else tail


def t_ppf(p: float, df: float) -> float:
    if not 0.0 < p < 1.0:
        raise ValueError("Probability must lie strictly inside (0, 1)")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -t_ppf(1.0 - p, df)
    lo, hi = 0.0, 1.0
    while t_cdf(hi, df) < p:
        lo, hi = hi, hi * 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if mid in (lo, hi):
            break
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def p_value(t, df: float | None = None):
    """Two-sided p-value: standard normal when df is None, Student t(df) otherwise."""
    stat = np.abs(np.asarray(t, dtype=float))
    if df is None:
        out = _erfc(stat / _SQRT2)
    else:
        out = np.vectorize(lambda v: math.nan if math.isnan(v) else 2.0 * _t_tail(v, float(df)), otypes=[float])(stat)
    return float(out) if out.ndim == 0 else out


def conf_int(estimate, se, level: float = 0.95, df: float | None = None) -> tuple:
    """Symmetric interval estimate +/- crit * se; normal critical value unless df is given."""
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie in (0, 1)")
    crit = norm_ppf(0.5 + 0.5 * level) if df is None else t_ppf(0.5 + 0.5 * level, df)
    estimate, se = np.asarray(estimate, dtype=float), np.asarray(se, dtype=float)
    lo, hi = estimate - crit * se, estimate + crit * se
    return (float(lo), float(hi)) if lo.ndim == 0 else (lo, hi)


# ---------------------------------------------------------------- regression

@dataclass(frozen=True, eq=False)
class OLSResult:
    beta: np.ndarray          # constant first when add_const
    se: np.ndarray            # classical; NaN when rank deficient or df_resid <= 0
    t: np.ndarray
    cov: np.ndarray
    resid: np.ndarray
    fitted: np.ndarray
    r2: float                 # centred if the design has a constant column, else uncentred
    sigma2: float
    n: int
    k: int
    df_resid: int
    rank: int
    rank_deficient: bool
    cond: float
    has_const: bool
    X: np.ndarray             # design actually used


@dataclass(frozen=True, eq=False)
class RobustResult:
    cov: np.ndarray
    se: np.ndarray
    t: np.ndarray
    lag: int
    small_sample: bool


def _design(X, add_const: bool) -> np.ndarray:
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X[:, None]
    if X.ndim != 2 or not X.shape[0]:
        raise ValueError("X must be a non-empty 1-D or 2-D array")
    if not np.isfinite(X).all():
        raise ValueError("X contains non-finite values")
    X = np.column_stack([np.ones(len(X)), X]) if add_const else X
    if not X.shape[1]:
        raise ValueError("Design has no columns")
    return X


def ols(X, y, add_const: bool = True) -> OLSResult:
    """Least squares by lstsq. A rank-deficient design is reported (rank_deficient=True,
    minimum-norm beta, NaN se/t/cov) rather than raised; fitted values stay valid."""
    X, y = _design(X, add_const), _vector(y, "y")
    n, k = X.shape
    if len(y) != n:
        raise ValueError("X and y have different lengths")
    beta, _, rank, singular = np.linalg.lstsq(X, y, rcond=None)
    rank = int(rank)
    fitted = X @ beta
    resid = y - fitted
    ssr, df_resid = float(resid @ resid), n - k
    has_const = bool(add_const or np.any((np.ptp(X, axis=0) == 0) & (X[0] != 0)))
    tss = float(np.sum((y - y.mean()) ** 2)) if has_const else float(y @ y)
    cov, sigma2 = np.full((k, k), np.nan), math.nan
    if rank == k and df_resid > 0:
        sigma2 = ssr / df_resid
        pinv = np.linalg.pinv(X)
        cov = sigma2 * (pinv @ pinv.T)
    se = np.sqrt(np.diag(cov))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = beta / se
    return OLSResult(beta=beta, se=se, t=t, cov=cov, resid=resid, fitted=fitted,
        r2=1.0 - ssr / tss if tss > 0 else math.nan, sigma2=sigma2, n=n, k=k, df_resid=df_resid,
        rank=rank, rank_deficient=rank < k, cond=float(singular[0] / singular[-1]) if singular[-1] > 0 else math.inf,
        has_const=has_const, X=X)


def nw_lag_default(n: int) -> int:
    """floor(4 (n/100)^(2/9))."""
    return int(math.floor(4.0 * (_count(n, "n", 1) / 100.0) ** (2.0 / 9.0)))


def hac_cov(X, resid, lag: int | None = None, small_sample: bool = False) -> np.ndarray:
    """Newey-West covariance (X'X)^-1 S (X'X)^-1 with
    S = G0 + sum_{j=1..lag} (1 - j/(lag+1)) (Gj + Gj'), Gj = sum_t u_t u_{t-j}', u_t = x_t e_t.
    X is the full design including any constant. lag=0 is White HC0. small_sample
    multiplies by n/(n-k). For overlapping h-period targets use lag >= h-1."""
    X, resid = _design(X, False), _vector(resid, "resid")
    n, k = X.shape
    if len(resid) != n or n <= k:
        raise ValueError("HAC needs n > k and residuals aligned with X")
    if np.linalg.matrix_rank(X) < k:
        raise ValueError("HAC covariance is undefined for a rank-deficient design")
    lag = nw_lag_default(n) if lag is None else _count(lag, "lag")
    if lag >= n:
        raise ValueError("lag must be smaller than n")
    u = X * resid[:, None]
    meat = u.T @ u
    for j in range(1, lag + 1):
        gamma = u[j:].T @ u[:-j]
        meat += (1.0 - j / (lag + 1.0)) * (gamma + gamma.T)
    pinv = np.linalg.pinv(X)
    bread = pinv @ pinv.T
    cov = bread @ meat @ bread
    return cov * n / (n - k) if small_sample else cov


def hc0_cov(X, resid) -> np.ndarray:
    return hac_cov(X, resid, lag=0)


def newey_west(fit: OLSResult, lag: int | None = None, small_sample: bool = False) -> RobustResult:
    """HAC standard errors and t-statistics for an ols() result. Refer t to the
    normal (p_value(t)) or, more cautiously in small samples, to t(df_resid)."""
    if fit.rank_deficient:
        raise ValueError("HAC covariance is undefined for a rank-deficient design")
    lag = nw_lag_default(fit.n) if lag is None else lag
    cov = hac_cov(fit.X, fit.resid, lag, small_sample)
    se = np.sqrt(np.diag(cov))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = fit.beta / se
    return RobustResult(cov=cov, se=se, t=t, lag=int(lag), small_sample=small_sample)


def linear_combination(beta, cov, weights) -> dict:
    """Estimate w'b with standard error sqrt(w'Vw) from the full covariance matrix."""
    beta, cov, w = np.asarray(beta, dtype=float), np.asarray(cov, dtype=float), np.asarray(weights, dtype=float)
    if beta.ndim != 1 or w.shape != beta.shape or cov.shape != (len(beta), len(beta)):
        raise ValueError("beta, cov and weights do not conform")
    estimate, variance = float(w @ beta), float(w @ cov @ w)
    se = math.sqrt(variance) if variance >= 0 else math.nan
    return {"estimate": estimate, "se": se, "t": estimate / se if se > 0 else math.nan}


# ---------------------------------------------------------------- distributed lags

def lag_matrix(x, max_lag: int, min_lag: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Columns x[t-min_lag] ... x[t-max_lag] for each kept row t; no value later than t is used.
    Rows whose lags are unavailable (t < max_lag) or non-finite are dropped. Returns
    (matrix, kept) where kept indexes the original series, so align with y[kept]."""
    x = np.asarray(x, dtype=float)
    max_lag, min_lag = _count(max_lag, "max_lag"), _count(min_lag, "min_lag")
    if x.ndim != 1 or min_lag > max_lag or max_lag >= len(x):
        raise ValueError("Need a 1-D series longer than max_lag and min_lag <= max_lag")
    rows = np.arange(max_lag, len(x))
    matrix = np.column_stack([x[rows - lag] for lag in range(min_lag, max_lag + 1)])
    keep = np.isfinite(matrix).all(axis=1)
    return matrix[keep], rows[keep]


def cumulative_lag_effects(beta, cov, lag_cols) -> dict:
    """Running sums of the lag coefficients (lag_cols in lag order) with standard
    errors sqrt(1'V1) over the matching covariance block, one entry per horizon."""
    beta, cov, cols = np.asarray(beta, dtype=float), np.asarray(cov, dtype=float), np.asarray(lag_cols, dtype=int)
    if cols.ndim != 1 or not len(cols) or cov.shape != (len(beta), len(beta)):
        raise ValueError("beta, cov and lag_cols do not conform")
    variance = np.diag(np.cumsum(np.cumsum(cov[np.ix_(cols, cols)], axis=0), axis=1))
    with np.errstate(invalid="ignore"):
        se = np.sqrt(variance)
    return {"cumulative": np.cumsum(beta[cols]), "se": se}


def distributed_lag(y, x, max_lag: int, controls=None, hac_lag: int | None = None, min_lag: int = 0) -> dict:
    """y[t] on a constant, x[t-min_lag..t-max_lag] and optional controls[t], with
    Newey-West errors. Cumulative effects use the HAC covariance."""
    y = np.asarray(y, dtype=float)
    lags, kept = lag_matrix(x, max_lag, min_lag)
    if y.ndim != 1 or len(y) != len(np.asarray(x)):
        raise ValueError("y and x must be 1-D and the same length")
    columns = [lags]
    if controls is not None:
        controls = np.asarray(controls, dtype=float)
        if len(controls) != len(y):
            raise ValueError("controls must align with y")
        columns.append(controls[kept])
    fit = ols(np.column_stack(columns), y[kept])
    robust = newey_west(fit, hac_lag)
    lag_cols = np.arange(1, 1 + lags.shape[1])
    return {"fit": fit, "hac": robust, "kept": kept, "lags": np.arange(min_lag, max_lag + 1),
        "lag_cols": lag_cols, "cumulative": cumulative_lag_effects(fit.beta, robust.cov, lag_cols)}


# ---------------------------------------------------------------- bootstrap

def _resample_inputs(data) -> tuple[list, int]:
    if isinstance(data, list) and any(isinstance(a, np.ndarray) for a in data):
        raise ValueError("Pass several arrays as a tuple, or one array with observations on axis 0")
    arrays = [np.asarray(a) for a in data] if isinstance(data, tuple) else [np.asarray(data)]
    if not arrays or any(a.ndim == 0 for a in arrays) or len({len(a) for a in arrays}) != 1 or not len(arrays[0]):
        raise ValueError("data must be a non-empty array or a tuple of arrays with equal length")
    return arrays, len(arrays[0])


def block_indices(n: int, block_len: int, n_boot: int, seed) -> np.ndarray:
    """Moving-block resampling indices, shape (n_boot, n): ceil(n/block_len) blocks
    with uniformly drawn starts in 0..n-block_len, concatenated and cut to n."""
    n, n_boot, block_len = _count(n, "n", 1), _count(n_boot, "n_boot", 1), _count(block_len, "block_len", 1)
    if block_len > n:
        raise ValueError("block_len cannot exceed the sample length")
    starts = _rng(seed).integers(0, n - block_len + 1, size=(n_boot, -(-n // block_len)))
    return (starts[:, :, None] + np.arange(block_len)).reshape(n_boot, -1)[:, :n]


def _apply(stat_fn, arrays: list, indices: np.ndarray) -> np.ndarray:
    return np.asarray([stat_fn(*(a[i] for a in arrays)) for i in indices], dtype=float)


def block_bootstrap(stat_fn, data, block_len: int, n_boot: int, seed) -> np.ndarray:
    """Bootstrap distribution of stat_fn over moving-block resamples of the rows of
    data. A tuple of equal-length arrays is resampled jointly and passed as
    stat_fn(*arrays). block_len=1 is the iid bootstrap."""
    arrays, n = _resample_inputs(data)
    return _apply(stat_fn, arrays, block_indices(n, block_len, n_boot, seed))


def pairs_bootstrap(stat_fn, data, n_boot: int, seed) -> np.ndarray:
    """Bootstrap distribution of stat_fn over rows drawn iid with replacement
    (whole rows, so y and x stay paired). For small cross-sections."""
    arrays, n = _resample_inputs(data)
    return _apply(stat_fn, arrays, _rng(seed).integers(0, n, size=(_count(n_boot, "n_boot", 1), n)))


def percentile_interval(distribution, level: float = 0.90) -> tuple:
    """Central percentile interval along axis 0 of a bootstrap distribution."""
    dist = np.asarray(distribution, dtype=float)
    if not 0.0 < level < 1.0 or not dist.size:
        raise ValueError("level must lie in (0, 1) and the distribution must be non-empty")
    if not np.isfinite(dist).all():
        raise ValueError("Bootstrap distribution contains non-finite values")
    lo, hi = np.quantile(dist, [0.5 - 0.5 * level, 0.5 + 0.5 * level], axis=0)
    return (float(lo), float(hi)) if np.ndim(lo) == 0 else (lo, hi)


# ---------------------------------------------------------------- forecast scoring

def _paired(pred, y) -> tuple[np.ndarray, np.ndarray]:
    pred, y = np.asarray(pred, dtype=float), np.asarray(y, dtype=float)
    if pred.shape != y.shape or not pred.size:
        raise ValueError("Forecasts and outcomes must be non-empty and the same shape")
    if not (np.isfinite(pred).all() and np.isfinite(y).all()):
        raise ValueError("Forecasts or outcomes contain non-finite values")
    return pred, y


def crps_ensemble(samples, y, fair: bool = True):
    """CRPS per target from ensemble members on the last axis (samples.shape == y.shape + (m,)).
    fair=True is the fair estimator mean|x_i - y| - sum_{i,j}|x_i - x_j| / (2 m (m-1)),
    unbiased for the CRPS of the distribution the members were drawn from; fair=False
    divides by 2 m^2 (CRPS of the empirical distribution, biased upward for small m).
    A single member reduces to absolute error. Members must be draws, not quantiles."""
    x, obs = np.asarray(samples, dtype=float), np.asarray(y, dtype=float)
    if x.ndim == 0 or not x.shape[-1] or x.shape[:-1] != obs.shape:
        raise ValueError("samples must have shape y.shape + (members,)")
    if not (np.isfinite(x).all() and np.isfinite(obs).all()):
        raise ValueError("samples or outcomes contain non-finite values")
    m = x.shape[-1]
    x = np.sort(x, axis=-1)
    accuracy = np.abs(x - obs[..., None]).mean(axis=-1)
    spread = (x * (2 * np.arange(m) - m + 1)).sum(axis=-1)
    out = accuracy - (spread / (m * (m - 1) if fair else m * m) if m > 1 else 0.0)
    return float(out) if out.ndim == 0 else out


def crps_normal(mu, sigma, y):
    """Closed-form CRPS of N(mu, sigma^2): sigma (z (2 Phi(z) - 1) + 2 phi(z) - 1/sqrt(pi))."""
    mu, sigma, y = (np.asarray(v, dtype=float) for v in (mu, sigma, y))
    if np.any(~(sigma > 0)):
        raise ValueError("sigma must be positive")
    z = (y - mu) / sigma
    out = sigma * (z * (2.0 * np.asarray(norm_cdf(z)) - 1.0) + 2.0 * np.asarray(norm_pdf(z)) - 1.0 / math.sqrt(math.pi))
    return float(out) if out.ndim == 0 else out


def crps_point(pred, y):
    """CRPS of a point forecast, which is the absolute error."""
    pred, y = _paired(pred, y)
    out = np.abs(pred - y)
    return float(out) if out.ndim == 0 else out


def pinball_loss(q_pred, y, tau: float):
    """Quantile loss per target: tau (y - q) if y >= q else (1 - tau)(q - y)."""
    if not 0.0 < tau < 1.0:
        raise ValueError("tau must lie in (0, 1)")
    q, y = _paired(q_pred, y)
    d = y - q
    out = np.where(d >= 0, tau * d, (tau - 1.0) * d)
    return float(out) if out.ndim == 0 else out


def coverage(lo, hi, y) -> float:
    """Share of outcomes inside [lo, hi], bounds inclusive."""
    lo, y = _paired(lo, y)
    hi, _ = _paired(hi, y)
    if np.any(lo > hi):
        raise ValueError("Interval lower bound exceeds upper bound")
    return float(np.mean((y >= lo) & (y <= hi)))


def brier_score(prob, outcome) -> float:
    """Mean squared error of probabilities against 0/1 outcomes."""
    p, o = _paired(prob, outcome)
    if np.any((p < 0) | (p > 1)) or np.any((o != 0) & (o != 1)):
        raise ValueError("Probabilities must lie in [0, 1] and outcomes must be 0 or 1")
    return float(np.mean((p - o) ** 2))


def mae(pred, y) -> float:
    pred, y = _paired(pred, y)
    return float(np.mean(np.abs(pred - y)))


def paired_loss_diff(loss_a, loss_b, n_boot: int, seed, level: float = 0.90, block_len: int = 1) -> dict:
    """Mean of loss_a - loss_b over the same targets (negative favours a) with a
    percentile bootstrap interval and n. No verdict is returned: at small n the
    interval undercovers and is descriptive only. n < 2 gives a NaN interval."""
    a, b = _vector(loss_a, "loss_a"), _vector(loss_b, "loss_b")
    if a.shape != b.shape:
        raise ValueError("Losses must cover the same targets")
    diff = a - b
    out = {"mean_diff": float(diff.mean()), "lo": math.nan, "hi": math.nan, "n": len(diff),
        "level": level, "n_boot": n_boot, "block_len": block_len}
    if len(diff) >= 2:
        out["lo"], out["hi"] = percentile_interval(block_bootstrap(np.mean, diff, block_len, n_boot, seed), level)
    return out


# ---------------------------------------------------------------- performance

@dataclass(frozen=True)
class SharpeStats:
    n: int
    mean: float
    sd: float                 # ddof=1
    skew: float               # m3 / m2^1.5, population moments
    kurt: float               # m4 / m2^2, equals 3 for a normal
    sharpe: float             # per period
    sharpe_ann: float
    se: float                 # per period
    se_ann: float
    periods_per_year: float


def sharpe_se_from_moments(sharpe: float, n: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """sqrt((1 - skew SR + (kurt - 1)/4 SR^2) / (n - 1)), per-period units, iid returns.
    With skew 0 and kurt 3 this is Lo's (1 + SR^2/2) form."""
    variance = 1.0 - skew * sharpe + 0.25 * (kurt - 1.0) * sharpe * sharpe
    return math.sqrt(variance / (n - 1)) if n > 1 and variance > 0 else math.nan


def psr_from_moments(sharpe: float, benchmark: float, n: int, skew: float = 0.0, kurt: float = 3.0) -> float:
    """Phi((SR - SR*) / se(SR)): probability that the true per-period Sharpe exceeds the benchmark."""
    se = sharpe_se_from_moments(sharpe, n, skew, kurt)
    return norm_cdf((sharpe - benchmark) / se) if se > 0 else math.nan


def sharpe_stats(returns, periods_per_year: float = 252.0) -> SharpeStats:
    """Sharpe of per-period excess returns. Zero variance gives NaN ratios. The
    standard error assumes iid returns; serial correlation is not corrected for."""
    r = _vector(returns, "returns")
    n, mean = len(r), float(r.mean())
    sd = float(r.std(ddof=1)) if n > 1 else math.nan
    m2 = float(np.mean((r - mean) ** 2))
    if not sd > 0 or not m2 > 0:
        return SharpeStats(n, mean, sd, math.nan, math.nan, math.nan, math.nan, math.nan, math.nan, periods_per_year)
    skew = float(np.mean((r - mean) ** 3)) / m2 ** 1.5
    kurt = float(np.mean((r - mean) ** 4)) / m2 ** 2
    sharpe, scale = mean / sd, math.sqrt(periods_per_year)
    se = sharpe_se_from_moments(sharpe, n, skew, kurt)
    return SharpeStats(n, mean, sd, skew, kurt, sharpe, sharpe * scale, se, se * scale, periods_per_year)


def probabilistic_sharpe(returns, benchmark_ann: float = 0.0, periods_per_year: float = 252.0) -> float:
    s = sharpe_stats(returns, periods_per_year)
    return psr_from_moments(s.sharpe, benchmark_ann / math.sqrt(periods_per_year), s.n, s.skew, s.kurt)


def expected_max_sharpe(n_trials: int, trial_sharpe_var: float) -> float:
    """sqrt(V) ((1 - g) Phi^-1(1 - 1/N) + g Phi^-1(1 - 1/(N e))), g the Euler-Mascheroni
    constant: the expected best Sharpe among N independent zero-skill trials, in the
    units of sqrt(V). One trial has no selection, so it returns 0."""
    n_trials = _count(n_trials, "n_trials", 1)
    if not trial_sharpe_var >= 0:
        raise ValueError("Variance of trial Sharpe ratios must be non-negative")
    if n_trials == 1:
        return 0.0
    return math.sqrt(trial_sharpe_var) * ((1.0 - EULER_GAMMA) * norm_ppf(1.0 - 1.0 / n_trials)
        + EULER_GAMMA * norm_ppf(1.0 - 1.0 / (n_trials * math.e)))


def deflated_sharpe(returns, n_trials: int, trial_sharpe_var: float, *, trial_var_annualised: bool,
        periods_per_year: float = 252.0) -> dict:
    """Probabilistic Sharpe ratio against the expected maximum Sharpe of n_trials
    zero-skill trials. trial_sharpe_var is the variance across the trials' Sharpe
    ratios; say whether those were annualised. n_trials should count effectively
    independent variants; counting correlated variants as independent overstates the haircut."""
    s = sharpe_stats(returns, periods_per_year)
    per_period_var = trial_sharpe_var / periods_per_year if trial_var_annualised else trial_sharpe_var
    hurdle = expected_max_sharpe(n_trials, per_period_var)
    return {"dsr": psr_from_moments(s.sharpe, hurdle, s.n, s.skew, s.kurt),
        "psr_zero": psr_from_moments(s.sharpe, 0.0, s.n, s.skew, s.kurt),
        "sharpe_ann": s.sharpe_ann, "se_ann": s.se_ann,
        "expected_max_sharpe_ann": hurdle * math.sqrt(periods_per_year), "n_trials": int(n_trials), "n": s.n}


# ---------------------------------------------------------------- event study

@dataclass(frozen=True, eq=False)
class EventStudyResult:
    days: np.ndarray              # -k .. +h
    events: np.ndarray            # kept event indices, ascending
    ar: np.ndarray                # (n_events, k+h+1) abnormal returns
    car: np.ndarray               # cumulated from day -k
    mean_ar: np.ndarray
    mean_car: np.ndarray
    se_car: np.ndarray            # cross-event sd / sqrt(n_events)
    lo: np.ndarray                # pointwise bootstrap band over events
    hi: np.ndarray
    alpha: np.ndarray
    beta: np.ndarray
    est_n: np.ndarray
    overlapping: np.ndarray       # kept event lies within k+h days of another input event
    est_contaminated: np.ndarray  # another input event's window was used in this estimation window
    dropped: list                 # [{"index": int, "reason": str}]
    n_events: int
    overlap_rule: str
    level: float
    n_boot: int


def event_window_mask(n: int, events, k: int, h: int) -> np.ndarray:
    """Boolean mask of days inside any event window [e-k, e+h]."""
    mask = np.zeros(_count(n, "n", 1), dtype=bool)
    for e in np.asarray(events, dtype=int).ravel():
        mask[max(0, e - k):max(0, e + h + 1)] = True
    return mask


def thin_events(events, span: int, rule: str) -> dict:
    """Apply an overlap rule to event indices. Two events overlap when they are at
    most `span` positions apart (span = k + h makes their event windows intersect).
    "flag": keep all and mark overlaps. "drop_later": drop any event with an earlier
    event within span, kept or not (first of each cluster survives; uses no future
    information). "drop_all": keep only events with no neighbour within span."""
    if rule not in OVERLAP_RULES:
        raise ValueError(f"overlap rule must be one of {OVERLAP_RULES}")
    raw = np.asarray(events)
    if raw.size and not np.issubdtype(raw.dtype, np.integer):
        raise ValueError("Event indices must be integers")
    ordered = np.unique(raw.astype(int))
    close = np.diff(ordered) <= _count(span, "span")
    near_earlier, near_later = np.r_[False, close][:len(ordered)], np.r_[close, False][:len(ordered)]
    overlapping = near_earlier | near_later
    drop = {"flag": np.zeros(len(ordered), dtype=bool), "drop_later": near_earlier, "drop_all": overlapping}[rule]
    return {"events": ordered, "overlapping": overlapping, "kept": ordered[~drop], "dropped": ordered[drop]}


def event_study(asset, market, events, k: int, h: int, *, overlap: str, seed, est_len: int = 120, gap: int = 0,
        min_est: int | None = None, est_exclude=None, n_boot: int = 2000, level: float = 0.90) -> EventStudyResult:
    """Market-model event study on positional daily returns.

    For event index e the model r_asset = a + b r_market is fitted on
    [e-k-gap-est_len, e-k-gap-1], which ends before the event window [e-k, e+h]
    starts. Abnormal return = actual - (a + b market); CAR cumulates from day -k.
    `overlap` is one of OVERLAP_RULES (see thin_events, span k+h) and is applied to
    every input event before feasibility checks, so an event that is itself unusable
    still counts as contaminating its neighbours. Events are dropped, with a reason,
    when the windows fall outside the sample, the event window has a missing return,
    or fewer than min_est (default est_len // 2, at least 3) estimation days remain.
    est_exclude is an optional boolean mask of days never used for estimation (for
    example event_window_mask over all known events). The band is a pointwise
    percentile interval of the mean CAR path from resampling events with
    replacement; it treats events as independent, undercovers with few events and
    is NaN with fewer than two."""
    y, x = np.asarray(asset, dtype=float), np.asarray(market, dtype=float)
    k, h, gap, est_len = _count(k, "k"), _count(h, "h"), _count(gap, "gap"), _count(est_len, "est_len", 3)
    if y.ndim != 1 or y.shape != x.shape or not len(y):
        raise ValueError("asset and market must be 1-D arrays of the same length")
    if not 0.0 < level < 1.0:
        raise ValueError("level must lie in (0, 1)")
    n, width, n_boot, rng = len(y), k + h + 1, _count(n_boot, "n_boot", 1), _rng(seed)
    min_est = max(3, est_len // 2) if min_est is None else _count(min_est, "min_est", 3)
    finite = np.isfinite(y) & np.isfinite(x)
    usable = finite.copy()
    if est_exclude is not None:
        est_exclude = np.asarray(est_exclude, dtype=bool)
        if est_exclude.shape != y.shape:
            raise ValueError("est_exclude must align with the return arrays")
        usable &= ~est_exclude
    thinned = thin_events(events, k + h, overlap)
    dropped = [{"index": int(e), "reason": "overlap"} for e in thinned["dropped"]]
    in_window = event_window_mask(n, thinned["events"], k, h)
    flagged = dict(zip(thinned["events"].tolist(), thinned["overlapping"].tolist()))
    rows = []
    for e in thinned["kept"].tolist():
        start, stop = e - k - gap - est_len, e - k - gap
        if start < 0 or e + h >= n:
            dropped.append({"index": e, "reason": "out_of_range"})
            continue
        if not finite[e - k:e + h + 1].all():
            dropped.append({"index": e, "reason": "missing_return_in_event_window"})
            continue
        use = usable[start:stop]
        if use.sum() < min_est:
            dropped.append({"index": e, "reason": "short_estimation_window"})
            continue
        fit = ols(x[start:stop][use], y[start:stop][use])
        if fit.rank_deficient:
            dropped.append({"index": e, "reason": "degenerate_estimation_window"})
            continue
        abnormal = y[e - k:e + h + 1] - fit.beta[0] - fit.beta[1] * x[e - k:e + h + 1]
        rows.append((e, abnormal, fit.beta[0], fit.beta[1], int(use.sum()), flagged[e], bool((in_window[start:stop] & use).any())))
    m = len(rows)
    ar = np.array([r[1] for r in rows]).reshape(m, width)
    car = np.cumsum(ar, axis=1)
    blank = np.full(width, np.nan)
    mean_ar, mean_car = (ar.mean(axis=0), car.mean(axis=0)) if m else (blank, blank.copy())
    se_car, lo, hi = blank.copy(), blank.copy(), blank.copy()
    if m >= 2:
        se_car = car.std(axis=0, ddof=1) / math.sqrt(m)
        draws = rng.integers(0, m, size=(n_boot, m))
        lo, hi = np.quantile(car[draws].mean(axis=1), [0.5 - 0.5 * level, 0.5 + 0.5 * level], axis=0)
    return EventStudyResult(days=np.arange(-k, h + 1), events=np.array([r[0] for r in rows], dtype=int), ar=ar, car=car,
        mean_ar=mean_ar, mean_car=mean_car, se_car=se_car, lo=lo, hi=hi,
        alpha=np.array([r[2] for r in rows], dtype=float), beta=np.array([r[3] for r in rows], dtype=float),
        est_n=np.array([r[4] for r in rows], dtype=int), overlapping=np.array([r[5] for r in rows], dtype=bool),
        est_contaminated=np.array([r[6] for r in rows], dtype=bool), dropped=sorted(dropped, key=lambda d: d["index"]),
        n_events=m, overlap_rule=overlap, level=level, n_boot=n_boot)


# ---------------------------------------------------------------- past-only beta

def _past_beta(asset, market, window: int | None, min_obs: int) -> dict:
    y, x = np.asarray(asset, dtype=float), np.asarray(market, dtype=float)
    if y.ndim != 1 or y.shape != x.shape:
        raise ValueError("asset and market must be 1-D arrays of the same length")
    finite = np.isfinite(y) & np.isfinite(x)
    n = len(y)
    beta, alpha, n_obs = np.full(n, np.nan), np.full(n, np.nan), np.zeros(n, dtype=int)
    for t in range(n):
        start = 0 if window is None else max(0, t - window)
        use = finite[start:t]
        n_obs[t] = int(use.sum())
        if n_obs[t] < min_obs:
            continue
        xs, ys = x[start:t][use], y[start:t][use]
        dx = xs - xs.mean()
        sxx = float(dx @ dx)
        if sxx > 0:
            beta[t] = float(dx @ (ys - ys.mean())) / sxx
            alpha[t] = ys.mean() - beta[t] * xs.mean()
    return {"beta": beta, "alpha": alpha, "n_obs": n_obs}


def rolling_beta(asset, market, window: int, min_obs: int | None = None) -> dict:
    """beta[t], alpha[t] from OLS of asset on market over days t-window .. t-1 only,
    so the value at t can be applied to day t without look-ahead. NaN until min_obs
    (default window) finite pairs are available; non-finite days are skipped."""
    window = _count(window, "window", 2)
    return _past_beta(asset, market, window, window if min_obs is None else _count(min_obs, "min_obs", 2))


def expanding_beta(asset, market, min_obs: int) -> dict:
    """As rolling_beta, but over all days 0 .. t-1."""
    return _past_beta(asset, market, None, _count(min_obs, "min_obs", 2))


# ---------------------------------------------------------------- utilities

def quantiles(values, qs=(0.1, 0.5, 0.9), axis: int = 0) -> np.ndarray:
    v = np.asarray(values, dtype=float)
    if not v.size or not np.isfinite(v).all():
        raise ValueError("values must be non-empty and finite")
    return np.quantile(v, np.asarray(qs, dtype=float), axis=axis)


def winsorize(values, lower_q: float = 0.01, upper_q: float = 0.99, ref=None) -> np.ndarray:
    """Clip values at the lower_q and upper_q quantiles of `ref` (default: the values
    themselves). Pass the in-sample data as ref to keep later data out of the bounds."""
    v = np.asarray(values, dtype=float)
    base = v if ref is None else np.asarray(ref, dtype=float)
    base = base[np.isfinite(base)]
    if not 0.0 <= lower_q < upper_q <= 1.0 or not base.size:
        raise ValueError("Need 0 <= lower_q < upper_q <= 1 and a non-empty reference sample")
    lo, hi = np.quantile(base, [lower_q, upper_q])
    return np.clip(v, lo, hi)


def standardize(values, mean: float, sd: float) -> np.ndarray:
    """(values - mean) / sd with the caller's in-sample mean and sd; nothing is estimated here."""
    if not (math.isfinite(mean) and math.isfinite(sd) and sd > 0):
        raise ValueError("mean must be finite and sd finite and positive")
    return (np.asarray(values, dtype=float) - mean) / sd
