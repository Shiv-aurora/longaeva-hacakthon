import math

import numpy as np
import pytest

from app.lab import stats as S


def ar1(rng, shape, rho, burn=200):
    """AR(1) paths along the last axis with unit-variance innovations."""
    shape = (shape,) if isinstance(shape, int) else tuple(shape)
    noise = rng.normal(size=shape[:-1] + (shape[-1] + burn,))
    out = np.empty_like(noise)
    out[..., 0] = noise[..., 0]
    for t in range(1, noise.shape[-1]):
        out[..., t] = rho * out[..., t - 1] + noise[..., t]
    return out[..., burn:]


def simpson(f, a, b, steps=20000):
    x = np.linspace(a, b, steps + 1)
    w = np.ones(steps + 1)
    w[1:-1:2], w[2:-1:2] = 4.0, 2.0
    return float((f(x) * w).sum() * (b - a) / steps / 3.0)


# ---------------------------------------------------------------- distributions

def test_normal_cdf_known_values_and_independent_quadrature():
    assert S.norm_cdf(0.0) == 0.5
    assert S.norm_cdf(1.0) == pytest.approx(0.8413447460685429, abs=1e-15)
    assert S.norm_cdf(2.0) == pytest.approx(0.9772498680518208, abs=1e-15)
    assert S.norm_cdf(-1.96) == pytest.approx(0.024997895148220435, abs=1e-15)
    assert S.norm_cdf(3.0) == pytest.approx(0.9986501019683699, abs=1e-15)
    assert S.norm_cdf(-6.0) == pytest.approx(9.8658765e-10, rel=1e-7)
    density = lambda z: np.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    for x in (-2.5, -0.3, 0.7, 1.645, 3.2):
        assert S.norm_cdf(x) == pytest.approx(0.5 + simpson(density, 0.0, x), abs=1e-12)
    values = S.norm_cdf(np.array([-1.0, 0.0, 1.0]))
    assert values.shape == (3,) and values[0] + values[2] == pytest.approx(1.0)
    assert S.norm_pdf(0.0) == pytest.approx(1 / math.sqrt(2 * math.pi))


def test_normal_inverse_known_values_and_round_trip():
    known = {0.5: 0.0, 0.75: 0.6744897501960817, 0.9: 1.2815515655446004, 0.95: 1.6448536269514722,
        0.975: 1.959963984540054, 0.99: 2.3263478740408408, 0.999: 3.090232306167813}
    for p, x in known.items():
        assert S.norm_ppf(p) == pytest.approx(x, abs=1e-12)
        assert S.norm_ppf(1 - p) == pytest.approx(-x, abs=1e-11)
    assert S.norm_ppf(S.norm_cdf(1.0)) == pytest.approx(1.0, abs=1e-12)
    probabilities = np.concatenate([10.0 ** -np.arange(1, 13), np.linspace(0.02, 0.98, 49), 1 - 10.0 ** -np.arange(2, 9)])
    assert np.allclose(S.norm_cdf(S.norm_ppf(probabilities)), probabilities, rtol=1e-12, atol=0)
    points = np.linspace(-8.0, 4.0, 49)
    assert np.allclose(S.norm_ppf(S.norm_cdf(points)), points, atol=1e-9)
    assert S.norm_ppf(0.0) == -math.inf and S.norm_ppf(1.0) == math.inf
    with pytest.raises(ValueError):
        S.norm_ppf(1.5)


def test_rational_approximation_alone_is_accurate_before_refinement():
    # Guards the tabulated coefficients: the refinement step would hide a wrong digit.
    for p in np.concatenate([10.0 ** -np.arange(2, 11), np.linspace(0.01, 0.99, 197)]):
        exact = S.norm_ppf(p)
        assert abs(S._acklam(p) - exact) <= 2e-9 * max(1.0, abs(exact))


def test_student_t_against_closed_forms_and_quadrature():
    for t in (-7.0, -1.3, 0.0, 0.4, 1.0, 2.5, 40.0):
        assert S.t_cdf(t, 1) == pytest.approx(0.5 + math.atan(t) / math.pi, abs=1e-13)
        assert S.t_cdf(t, 2) == pytest.approx(0.5 + t / (2 * math.sqrt(2 + t * t)), abs=1e-13)
    for df in (3, 5, 10, 27.5):
        scale = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
        density = lambda z: scale * (1 + z * z / df) ** (-(df + 1) / 2)
        for t in (0.5, 2.0, 3.5):
            assert S.t_cdf(t, df) == pytest.approx(0.5 + simpson(density, 0.0, t), abs=1e-11)
            assert S.t_cdf(-t, df) == pytest.approx(1 - S.t_cdf(t, df), abs=1e-14)
    assert S.t_cdf(1.3, 1e7) == pytest.approx(S.norm_cdf(1.3), abs=1e-6)
    assert S.t_ppf(0.975, 1) == pytest.approx(math.tan(math.pi * 0.475), rel=1e-12)
    assert S.t_ppf(0.975, 2) == pytest.approx(0.95 / math.sqrt(2 * 0.975 * 0.025), rel=1e-12)
    assert S.t_ppf(0.975, 10) == pytest.approx(2.228138852, abs=1e-8)
    assert S.t_ppf(0.975, 5) == pytest.approx(2.570581836, abs=1e-8)
    assert S.t_ppf(0.025, 30) == pytest.approx(-2.042272456, abs=1e-8)
    assert S.t_cdf(S.t_ppf(0.9, 7), 7) == pytest.approx(0.9, abs=1e-13)


def test_p_values_and_intervals():
    assert S.p_value(1.959963984540054) == pytest.approx(0.05, abs=1e-12)
    assert S.p_value(-1.959963984540054) == pytest.approx(0.05, abs=1e-12)
    assert S.p_value(1.0, df=1) == pytest.approx(0.5, abs=1e-13)
    assert S.p_value(math.tan(math.pi * 0.475), df=1) == pytest.approx(0.05, abs=1e-12)
    assert S.p_value(2.0, df=8) > S.p_value(2.0)
    assert np.allclose(S.p_value(np.array([0.0, 1.959963984540054])), [1.0, 0.05])
    lo, hi = S.conf_int(1.0, 2.0, level=0.95)
    assert lo == pytest.approx(1 - 2 * 1.959963984540054) and hi == pytest.approx(1 + 2 * 1.959963984540054)
    lo_t, hi_t = S.conf_int(np.array([0.0, 1.0]), np.array([1.0, 1.0]), level=0.95, df=10)
    assert np.allclose(hi_t - lo_t, 2 * 2.228138852, atol=1e-7)


# ---------------------------------------------------------------- regression

def test_ols_recovers_exact_linear_system():
    rng = np.random.default_rng(0)
    X = rng.normal(size=(30, 2))
    fit = S.ols(X, 2.0 + 3.0 * X[:, 0] - X[:, 1])
    assert np.allclose(fit.beta, [2.0, 3.0, -1.0], atol=1e-12)
    assert np.allclose(fit.resid, 0.0, atol=1e-12) and fit.r2 == pytest.approx(1.0)
    assert (fit.n, fit.k, fit.df_resid, fit.rank, fit.rank_deficient, fit.has_const) == (30, 3, 27, 3, False, True)
    assert np.all(fit.se < 1e-12) and fit.X.shape == (30, 3)


def test_ols_matches_hand_computed_simple_regression():
    fit = S.ols([0.0, 1.0, 2.0, 3.0], [1.0, 3.0, 2.0, 5.0])
    assert np.allclose(fit.beta, [1.1, 1.1])
    assert np.allclose(fit.resid, [-0.1, 0.8, -1.3, 0.6])
    assert fit.sigma2 == pytest.approx(1.35) and fit.df_resid == 2
    assert np.allclose(fit.se, [math.sqrt(0.945), math.sqrt(0.27)])
    assert np.allclose(fit.cov, 1.35 * np.array([[0.7, -0.3], [-0.3, 0.2]]))
    assert fit.r2 == pytest.approx(1 - 2.7 / 8.75)
    assert np.allclose(fit.t, fit.beta / fit.se)


def test_ols_without_constant_uses_uncentred_r_squared():
    fit = S.ols([1.0, 2.0, 3.0], [2.0, 4.0, 7.0], add_const=False)
    assert fit.beta[0] == pytest.approx(31 / 14) and not fit.has_const
    assert fit.se[0] == pytest.approx(math.sqrt(5 / 392))
    assert fit.r2 == pytest.approx(1 - (5 / 14) / 69)
    own_constant = S.ols(np.column_stack([np.ones(4), [0.0, 1.0, 2.0, 3.0]]), [1.0, 3.0, 2.0, 5.0], add_const=False)
    assert own_constant.has_const and own_constant.r2 == pytest.approx(1 - 2.7 / 8.75)


def test_ols_reports_rank_deficiency_instead_of_inventing_errors():
    x = np.arange(8.0)
    fit = S.ols(np.column_stack([x, 2 * x]), 1.0 + x)
    assert fit.rank_deficient and fit.rank == 2 and fit.k == 3
    assert np.isnan(fit.se).all() and np.isnan(fit.cov).all() and np.isnan(fit.t).all()
    assert np.allclose(fit.fitted, 1.0 + x)
    with pytest.raises(ValueError):
        S.newey_west(fit)
    saturated = S.ols([0.0, 1.0], [1.0, 2.0])
    assert not saturated.rank_deficient and saturated.df_resid == 0 and np.isnan(saturated.se).all()
    with pytest.raises(ValueError):
        S.ols([1.0, np.nan, 3.0], [1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        S.ols([1.0, 2.0, 3.0], [1.0, 2.0])


def test_hac_with_zero_lags_equals_white_computed_independently():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(80, 2))
    y = 1.0 + X @ np.array([0.5, -0.2]) + rng.normal(size=80) * (1 + np.abs(X[:, 0]))
    fit = S.ols(X, y)
    design = np.column_stack([np.ones(80), X])
    inverse = np.linalg.inv(design.T @ design)
    white = inverse @ (design.T @ np.diag(fit.resid ** 2) @ design) @ inverse
    robust = S.newey_west(fit, lag=0)
    assert np.allclose(robust.cov, white, rtol=1e-10, atol=0)
    assert np.allclose(S.hc0_cov(fit.X, fit.resid), white, rtol=1e-10, atol=0)
    assert np.allclose(robust.se, np.sqrt(np.diag(white))) and np.allclose(robust.t, fit.beta / robust.se)
    assert robust.lag == 0
    assert np.allclose(S.newey_west(fit, lag=0, small_sample=True).cov, white * 80 / 77)


def test_hac_matches_brute_force_bartlett_double_sum():
    rng = np.random.default_rng(2)
    n, lag = 40, 3
    X = rng.normal(size=(n, 2))
    fit = S.ols(X, rng.normal(size=n) + X[:, 0])
    meat = np.zeros((3, 3))
    for t in range(n):
        for s in range(n):
            if abs(t - s) <= lag:
                meat += (1 - abs(t - s) / (lag + 1)) * fit.resid[t] * fit.resid[s] * np.outer(fit.X[t], fit.X[s])
    inverse = np.linalg.inv(fit.X.T @ fit.X)
    assert np.allclose(S.newey_west(fit, lag=lag).cov, inverse @ meat @ inverse, rtol=1e-10, atol=0)
    assert np.all(np.linalg.eigvalsh(S.newey_west(fit, lag=lag).cov) > 0)


def test_hac_default_lag_rule_and_argument_checks():
    assert [S.nw_lag_default(n) for n in (28, 100, 250, 1000, 2500)] == [3, 4, 4, 6, 8]
    rng = np.random.default_rng(3)
    fit = S.ols(rng.normal(size=250), rng.normal(size=250))
    assert S.newey_west(fit).lag == 4
    assert np.allclose(S.newey_west(fit).cov, S.hac_cov(fit.X, fit.resid, lag=4))
    for bad in (-1, 250, 1.5):
        with pytest.raises(ValueError):
            S.hac_cov(fit.X, fit.resid, lag=bad)


def test_hac_exceeds_classical_under_ar1_errors_and_tracks_true_sampling_error():
    rng = np.random.default_rng(4)
    x, errors = ar1(rng, 2000, 0.7), ar1(rng, 2000, 0.7)
    fit = S.ols(x, 1.0 + 0.5 * x + errors)
    assert S.newey_west(fit).se[1] > 1.3 * fit.se[1]

    # Mean of an AR(1): the long-run variance is known, so the size of the correction can be checked.
    paths = 1.0 + ar1(rng, (300, 400), 0.6)
    estimates, classical, robust = [], [], []
    for path in paths:
        fit = S.ols(np.empty((400, 0)), path)
        estimates.append(fit.beta[0]); classical.append(fit.se[0]); robust.append(S.newey_west(fit, lag=12).se[0])
    truth = np.std(estimates, ddof=1)
    assert truth == pytest.approx(math.sqrt(6.25 / 400), rel=0.12)
    assert np.mean(classical) / truth < 0.6
    assert 0.8 < np.mean(robust) / truth < 1.05


# ---------------------------------------------------------------- distributed lags

def test_lag_matrix_drops_incomplete_rows_and_never_looks_ahead():
    matrix, kept = S.lag_matrix(np.arange(6.0), 2)
    assert np.array_equal(kept, [2, 3, 4, 5])
    assert np.array_equal(matrix, [[2, 1, 0], [3, 2, 1], [4, 3, 2], [5, 4, 3]])
    lagged_only, kept_lagged = S.lag_matrix(np.arange(6.0), 2, min_lag=1)
    assert np.array_equal(lagged_only, [[1, 0], [2, 1], [3, 2], [4, 3]]) and np.array_equal(kept_lagged, kept)

    gappy = np.arange(6.0)
    gappy[3] = np.nan
    matrix, kept = S.lag_matrix(gappy, 1)
    assert np.array_equal(kept, [1, 2, 5]) and np.array_equal(matrix, [[1, 0], [2, 1], [5, 4]])

    rng = np.random.default_rng(5)
    x = rng.normal(size=50)
    changed = x.copy()
    changed[31:] += 100.0
    before, kept = S.lag_matrix(x, 4)
    after, _ = S.lag_matrix(changed, 4)
    assert np.array_equal(before[kept <= 30], after[kept <= 30])
    for row, t in zip(before, kept):
        assert np.array_equal(row, x[t - 4:t + 1][::-1])
    with pytest.raises(ValueError):
        S.lag_matrix(np.arange(3.0), 3)


def test_cumulative_lag_standard_error_equals_reparameterised_regression():
    rng = np.random.default_rng(6)
    n = 300
    x = ar1(rng, n, 0.5)
    noise = ar1(rng, n, 0.4)
    y = np.full(n, np.nan)
    y[2:] = 0.5 + 0.4 * x[2:] + 0.3 * x[1:-1] + 0.2 * x[:-2] + noise[2:]
    lags, kept = S.lag_matrix(x, 2)
    fit = S.ols(lags, y[kept])
    robust = S.newey_west(fit, lag=4)
    classical = S.cumulative_lag_effects(fit.beta, fit.cov, [1, 2, 3])
    hac = S.cumulative_lag_effects(fit.beta, robust.cov, [1, 2, 3])
    assert np.allclose(classical["cumulative"], np.cumsum(fit.beta[1:]))
    assert classical["se"][0] == pytest.approx(fit.se[1]) and hac["se"][0] == pytest.approx(robust.se[1])

    # y = a + (b0+b1+b2) x_t + b1 (x_{t-1} - x_t) + b2 (x_{t-2} - x_t): the long-run effect is one coefficient.
    total = S.ols(np.column_stack([lags[:, 0], lags[:, 1] - lags[:, 0], lags[:, 2] - lags[:, 0]]), y[kept])
    assert classical["cumulative"][2] == pytest.approx(total.beta[1])
    assert classical["se"][2] == pytest.approx(total.se[1], rel=1e-9)
    assert hac["se"][2] == pytest.approx(S.newey_west(total, lag=4).se[1], rel=1e-9)
    # y = a + (b0+b1) x_t + b1 (x_{t-1} - x_t) + b2 x_{t-2}
    partial = S.ols(np.column_stack([lags[:, 0], lags[:, 1] - lags[:, 0], lags[:, 2]]), y[kept])
    assert classical["cumulative"][1] == pytest.approx(partial.beta[1])
    assert classical["se"][1] == pytest.approx(partial.se[1], rel=1e-9)
    assert hac["se"][1] == pytest.approx(S.newey_west(partial, lag=4).se[1], rel=1e-9)

    combo = S.linear_combination(fit.beta, fit.cov, [0, 1, 1, 1])
    assert combo["estimate"] == pytest.approx(total.beta[1]) and combo["se"] == pytest.approx(total.se[1], rel=1e-9)
    assert combo["t"] == pytest.approx(total.t[1], rel=1e-9)
    difference = S.linear_combination(fit.beta, fit.cov, [0, 1, -1, 0])
    assert difference["se"] == pytest.approx(math.sqrt(fit.cov[1, 1] + fit.cov[2, 2] - 2 * fit.cov[1, 2]))

    bundle = S.distributed_lag(y, x, 2, hac_lag=4)
    assert np.array_equal(bundle["kept"], kept) and np.array_equal(bundle["lag_cols"], [1, 2, 3])
    assert np.allclose(bundle["cumulative"]["se"], hac["se"]) and np.array_equal(bundle["lags"], [0, 1, 2])
    assert bundle["cumulative"]["cumulative"][2] == pytest.approx(0.9, abs=4 * hac["se"][2])
    trend = np.arange(n) / n
    with_control = S.distributed_lag(y, x, 2, controls=trend, hac_lag=4)
    assert with_control["fit"].k == 5 and np.allclose(with_control["fit"].X[:, 4], trend[kept])


# ---------------------------------------------------------------- bootstrap

def test_block_indices_are_contiguous_seeded_and_degenerate_at_full_length():
    indices = S.block_indices(10, 4, 200, seed=7)
    assert indices.shape == (200, 10) and indices.min() >= 0 and indices.max() <= 9
    assert np.all(np.diff(indices[:, :8].reshape(200, 2, 4), axis=2) == 1)
    assert np.all(np.diff(indices[:, 8:], axis=1) == 1)
    assert np.array_equal(indices, S.block_indices(10, 4, 200, seed=7))
    assert not np.array_equal(indices, S.block_indices(10, 4, 200, seed=8))
    assert set(np.unique(S.block_indices(10, 4, 500, seed=1)[:, 0])) == set(range(7))
    assert np.all(S.block_bootstrap(np.mean, np.arange(10.0), 10, 50, seed=1) == 4.5)
    generator = np.random.default_rng(3)
    first, second = S.block_bootstrap(np.mean, np.arange(10.0), 2, 20, generator), S.block_bootstrap(np.mean, np.arange(10.0), 2, 20, generator)
    assert first.shape == (20,) and not np.array_equal(first, second)
    for bad_seed in (None, 1.5, True):
        with pytest.raises(ValueError):
            S.block_bootstrap(np.mean, np.arange(10.0), 2, 20, bad_seed)
    with pytest.raises(ValueError):
        S.block_bootstrap(np.mean, np.arange(10.0), 11, 20, 1)


def test_block_bootstrap_captures_serial_dependence_that_iid_resampling_misses():
    rng = np.random.default_rng(8)
    series = ar1(rng, 2000, 0.6)
    truth = math.sqrt(6.25 / 2000)
    blocked = S.block_bootstrap(np.mean, series, 50, 500, seed=9).std(ddof=1)
    independent = S.block_bootstrap(np.mean, series, 1, 500, seed=9).std(ddof=1)
    assert independent == pytest.approx(series.std(ddof=1) / math.sqrt(2000), rel=0.1)
    assert blocked > 1.3 * independent
    assert 0.65 * truth < blocked < 1.35 * truth


def test_pairs_bootstrap_keeps_rows_together():
    rng = np.random.default_rng(10)
    x = rng.normal(size=25)
    y = 1.0 + 2.0 * x + rng.normal(size=25) * 0.3
    grid = np.arange(25.0)
    exact = np.column_stack([grid, 2 * grid + 1])
    assert np.all(S.pairs_bootstrap(lambda d: np.max(np.abs(d[:, 1] - 2 * d[:, 0] - 1)), exact, 200, seed=1) == 0)
    assert np.all(S.block_bootstrap(lambda a, b: np.max(np.abs(b - 2 * a - 1)), (grid, 2 * grid + 1), 5, 200, seed=1) == 0)
    draws = S.pairs_bootstrap(lambda d: d[:, 0], exact, 300, seed=1)
    assert draws.shape == (300, 25) and set(np.unique(draws)) == set(grid) and len(np.unique(draws[0])) < 25
    slopes = S.pairs_bootstrap(lambda a, b: S.ols(a, b).beta, (x, y), 400, seed=2)
    assert slopes.shape == (400, 2)
    lo, hi = S.percentile_interval(slopes, 0.9)
    assert lo.shape == (2,) and lo[1] < 2.0 < hi[1] and lo[0] < 1.0 < hi[0]
    assert np.array_equal(slopes, S.pairs_bootstrap(lambda a, b: S.ols(a, b).beta, (x, y), 400, seed=2))
    with pytest.raises(ValueError):
        S.pairs_bootstrap(np.mean, (x, y[:-1]), 10, seed=1)
    with pytest.raises(ValueError):
        S.pairs_bootstrap(lambda a, b: 0.0, [x, y], 10, seed=1)


def test_percentile_interval_on_known_distribution():
    assert S.percentile_interval(np.arange(101.0), 0.9) == pytest.approx((5.0, 95.0))
    assert S.percentile_interval(np.arange(101.0), 0.5) == pytest.approx((25.0, 75.0))
    lo, hi = S.percentile_interval(np.column_stack([np.arange(101.0), 2 * np.arange(101.0)]), 0.8)
    assert np.allclose(lo, [10, 20]) and np.allclose(hi, [90, 180])
    with pytest.raises(ValueError):
        S.percentile_interval([1.0, np.nan], 0.9)
    with pytest.raises(ValueError):
        S.percentile_interval([1.0, 2.0], 1.0)


# ---------------------------------------------------------------- forecast scoring

def test_crps_hand_computed_and_single_member_is_absolute_error():
    assert S.crps_ensemble([0.0, 2.0], 1.0, fair=False) == pytest.approx(0.5)
    assert S.crps_ensemble([0.0, 2.0], 1.0) == pytest.approx(0.0)
    assert S.crps_ensemble([2.0, 0.0, 5.0], 1.0) == pytest.approx(2.0 - 10.0 / 6.0)
    assert S.crps_ensemble([2.0, 0.0, 5.0], 1.0, fair=False) == pytest.approx(2.0 - 10.0 / 9.0)
    assert S.crps_ensemble([3.5], 1.0) == pytest.approx(2.5) and S.crps_ensemble([3.5], 1.0, fair=False) == pytest.approx(2.5)
    assert S.crps_point(3.5, 1.0) == pytest.approx(2.5)
    assert np.allclose(S.crps_point([1.0, 4.0], [2.0, 2.5]), [1.0, 1.5])
    batch = S.crps_ensemble(np.array([[0.0, 2.0], [1.0, 1.0]]), np.array([1.0, 3.0]), fair=False)
    assert np.allclose(batch, [0.5, 2.0])
    with pytest.raises(ValueError):
        S.crps_ensemble(np.zeros((2, 3)), np.zeros(3))


def test_crps_of_large_normal_ensemble_matches_closed_form():
    rng = np.random.default_rng(12)
    mu, sigma = 0.3, 1.7
    draws = rng.normal(mu, sigma, 200_000)
    assert S.crps_normal(0.0, 1.0, 0.0) == pytest.approx((math.sqrt(2) - 1) / math.sqrt(math.pi))
    for y in (-2.0, 0.3, 1.1, 6.0):
        exact = S.crps_normal(mu, sigma, y)
        assert S.crps_ensemble(draws, y) == pytest.approx(exact, abs=0.01)
        # Independent of the kernel form: CRPS is the integral of (F(x) - 1{x >= y})^2.
        brier_integral = simpson(lambda x: S.norm_cdf((x - mu) / sigma) ** 2, y - 30, y, 4000) \
            + simpson(lambda x: (S.norm_cdf((x - mu) / sigma) - 1.0) ** 2, y, y + 30, 4000)
        assert exact == pytest.approx(brier_integral, abs=1e-8)
    assert np.allclose(S.crps_normal(np.zeros(2), np.ones(2), np.array([0.0, 1.0])), [S.crps_normal(0, 1, 0), S.crps_normal(0, 1, 1)])


def test_fair_crps_is_unbiased_for_small_ensembles_and_standard_is_not():
    rng = np.random.default_rng(13)
    members, y = 3, 0.5
    ensembles = rng.normal(size=(40_000, members))
    targets = np.full(40_000, y)
    fair = S.crps_ensemble(ensembles, targets).mean()
    standard = S.crps_ensemble(ensembles, targets, fair=False).mean()
    exact = S.crps_normal(0.0, 1.0, y)
    assert fair == pytest.approx(exact, abs=0.01)
    assert standard - fair == pytest.approx(1 / (members * math.sqrt(math.pi)), abs=0.005)


def test_pinball_loss_known_values_and_link_to_crps():
    assert S.pinball_loss(1.0, 3.0, 0.9) == pytest.approx(1.8)
    assert S.pinball_loss(3.0, 1.0, 0.9) == pytest.approx(0.2)
    assert np.allclose(S.pinball_loss([1.0, 3.0, 2.0], [3.0, 1.0, 2.0], 0.5), [1.0, 1.0, 0.0])
    with pytest.raises(ValueError):
        S.pinball_loss(1.0, 2.0, 1.0)
    # CRPS of an empirical distribution is twice the mean pinball loss of its order statistics at (i - 0.5) / m.
    rng = np.random.default_rng(14)
    members = np.sort(rng.normal(size=200))
    levels = (np.arange(200) + 0.5) / 200
    for y in (-0.4, 1.3):
        doubled = 2 * np.mean([S.pinball_loss(q, y, tau) for q, tau in zip(members, levels)])
        assert S.crps_ensemble(members, y, fair=False) == pytest.approx(doubled, rel=1e-12)


def test_coverage_brier_and_mae_known_cases():
    assert S.coverage(np.zeros(4), np.ones(4), [0.5, 2.0, -1.0, 1.0]) == pytest.approx(0.5)
    assert S.coverage([0.0, 0.0], [1.0, 1.0], [0.0, 1.0]) == 1.0
    with pytest.raises(ValueError):
        S.coverage([1.0], [0.0], [0.5])
    assert S.brier_score([0.9, 0.2], [1, 0]) == pytest.approx(0.025)
    assert S.brier_score([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    with pytest.raises(ValueError):
        S.brier_score([1.2], [1])
    with pytest.raises(ValueError):
        S.brier_score([0.2], [0.5])
    assert S.mae([1.0, 2.0, 5.0], [2.0, 2.0, 1.0]) == pytest.approx(5 / 3)
    with pytest.raises(ValueError):
        S.mae([1.0, 2.0], [1.0])


def test_paired_comparison_returns_interval_and_count_not_a_verdict():
    base = np.array([1.0, 2.0, 3.0, 4.0])
    constant = S.paired_loss_diff(base + 0.5, base, 500, seed=1)
    assert set(constant) == {"mean_diff", "lo", "hi", "n", "level", "n_boot", "block_len"}
    assert constant["mean_diff"] == pytest.approx(0.5) and constant["n"] == 4
    assert constant["lo"] == pytest.approx(0.5) and constant["hi"] == pytest.approx(0.5)

    single = S.paired_loss_diff([1.0], [3.0], 100, seed=1)
    assert single["mean_diff"] == -2.0 and single["n"] == 1 and math.isnan(single["lo"]) and math.isnan(single["hi"])

    rng = np.random.default_rng(15)
    a, b = rng.normal(1.0, 1.0, 12), rng.normal(1.0, 1.0, 12)
    noisy = S.paired_loss_diff(a, b, 2000, seed=3)
    assert noisy["lo"] < noisy["mean_diff"] < noisy["hi"] and noisy["n"] == 12
    assert noisy == S.paired_loss_diff(a, b, 2000, seed=3)
    # iid bootstrap of a mean: the interval half-width should be near 1.645 standard errors.
    standard_error = (a - b).std(ddof=1) / math.sqrt(12)
    assert (noisy["hi"] - noisy["lo"]) / 2 == pytest.approx(1.645 * standard_error * math.sqrt(11 / 12), rel=0.1)
    with pytest.raises(ValueError):
        S.paired_loss_diff([1.0, 2.0], [1.0], 10, seed=1)


# ---------------------------------------------------------------- performance

def test_sharpe_statistics_on_a_two_point_series_by_hand():
    returns = np.tile([0.02, -0.01], 50)
    stats = S.sharpe_stats(returns)
    sd = 0.015 * math.sqrt(100 / 99)
    assert stats.n == 100 and stats.mean == pytest.approx(0.005) and stats.sd == pytest.approx(sd)
    assert stats.skew == pytest.approx(0.0, abs=1e-12) and stats.kurt == pytest.approx(1.0)
    assert stats.sharpe == pytest.approx(0.005 / sd) and stats.sharpe_ann == pytest.approx(0.005 / sd * math.sqrt(252))
    assert stats.se == pytest.approx(1 / math.sqrt(99)) and stats.se_ann == pytest.approx(math.sqrt(252 / 99))
    assert S.probabilistic_sharpe(returns) == pytest.approx(S.norm_cdf(3.3))
    assert S.probabilistic_sharpe(returns, benchmark_ann=stats.sharpe_ann) == pytest.approx(0.5)
    assert S.sharpe_stats(returns, periods_per_year=12).sharpe_ann == pytest.approx(0.005 / sd * math.sqrt(12))
    flat = S.sharpe_stats(np.zeros(20))
    assert math.isnan(flat.sharpe) and math.isnan(flat.se) and math.isnan(S.probabilistic_sharpe(np.zeros(20)))


def test_sharpe_standard_error_matches_simulated_sampling_error():
    rng = np.random.default_rng(16)
    n = 400
    normal = rng.normal(0.1, 1.0, size=(3000, n))
    estimates = normal.mean(axis=1) / normal.std(axis=1, ddof=1)
    assert S.sharpe_se_from_moments(0.1, n) == pytest.approx(math.sqrt(1.005 / 399))
    assert estimates.std(ddof=1) == pytest.approx(S.sharpe_se_from_moments(0.1, n), rel=0.05)
    assert np.mean([S.sharpe_stats(row).se for row in normal[:200]]) == pytest.approx(estimates.std(ddof=1), rel=0.05)

    # Unit exponential shifted to Sharpe 0.1: skew 2, kurtosis 9, so positive skew narrows the error.
    skewed = rng.exponential(1.0, size=(4000, n)) - 0.9
    estimates = skewed.mean(axis=1) / skewed.std(axis=1, ddof=1)
    adjusted = S.sharpe_se_from_moments(0.1, n, skew=2.0, kurt=9.0)
    assert adjusted == pytest.approx(math.sqrt(0.82 / 399))
    assert estimates.std(ddof=1) == pytest.approx(adjusted, rel=0.05)
    assert abs(estimates.std(ddof=1) - adjusted) < abs(estimates.std(ddof=1) - S.sharpe_se_from_moments(0.1, n))


def test_expected_maximum_sharpe_formula_against_simulation_and_published_example():
    assert S.expected_max_sharpe(1, 0.5) == 0.0
    by_hand = (1 - 0.5772156649015329) * S.norm_ppf(0.99) + 0.5772156649015329 * S.norm_ppf(1 - 1 / (100 * math.e))
    assert S.expected_max_sharpe(100, 1.0) == pytest.approx(by_hand)
    assert S.expected_max_sharpe(100, 4.0) == pytest.approx(2 * by_hand)
    rng = np.random.default_rng(17)
    for trials in (10, 100):
        simulated = rng.normal(size=(20_000, trials)).max(axis=1).mean()
        assert S.expected_max_sharpe(trials, 1.0) == pytest.approx(simulated, abs=0.05)
    # Worked example as recalled from Bailey and Lopez de Prado (2014): annualised Sharpe 2.5 on 1250
    # daily observations (250 a year), skewness -3, kurtosis 10, 100 trials with annualised Sharpe
    # variance 0.5, giving a hurdle near 0.1132 per day and a deflated Sharpe ratio near 0.90.
    hurdle = S.expected_max_sharpe(100, 0.5 / 250)
    assert hurdle == pytest.approx(0.1132, abs=2e-4)
    assert S.psr_from_moments(2.5 / math.sqrt(250), hurdle, 1250, skew=-3.0, kurt=10.0) == pytest.approx(0.9004, abs=1e-3)
    with pytest.raises(ValueError):
        S.expected_max_sharpe(0, 1.0)


def test_deflated_sharpe_falls_as_trials_increase():
    rng = np.random.default_rng(18)
    returns = rng.normal(0.001, 0.01, 750)
    results = [S.deflated_sharpe(returns, trials, 0.5, trial_var_annualised=True) for trials in (1, 2, 5, 20, 100, 1000)]
    values = [r["dsr"] for r in results]
    assert all(later < earlier for earlier, later in zip(values, values[1:]))
    assert values[0] == pytest.approx(S.probabilistic_sharpe(returns)) and results[0]["expected_max_sharpe_ann"] == 0.0
    assert all(r["psr_zero"] == pytest.approx(values[0]) for r in results)
    assert results[4]["expected_max_sharpe_ann"] == pytest.approx(S.expected_max_sharpe(100, 0.5))
    assert results[4]["n_trials"] == 100 and results[4]["n"] == 750
    per_day = S.deflated_sharpe(returns, 100, 0.5 / 252, trial_var_annualised=False)
    assert per_day["dsr"] == pytest.approx(results[4]["dsr"])
    wider = S.deflated_sharpe(returns, 100, 2.0, trial_var_annualised=True)
    assert wider["dsr"] < results[4]["dsr"]
    with pytest.raises(TypeError):
        S.deflated_sharpe(returns, 100, 0.5)


# ---------------------------------------------------------------- event study

def market_data(seed, n=6000, beta=1.3):
    rng = np.random.default_rng(seed)
    market = rng.normal(0.0003, 0.01, n)
    return 0.0001 + beta * market + rng.normal(0.0, 0.008, n), market


def test_event_study_recovers_injected_abnormal_return_and_placebo_shows_none():
    asset, market = market_data(19)
    events = 300 + 140 * np.arange(40)
    asset[events] += 0.02
    asset[events + 1] += 0.01
    result = S.event_study(asset, market, events, 5, 5, overlap="drop_all", seed=5, est_len=120)
    day = {int(d): i for i, d in enumerate(result.days)}
    noise = 0.008 / math.sqrt(40)
    assert result.n_events == 40 and result.dropped == [] and np.array_equal(result.events, events)
    assert np.array_equal(result.days, np.arange(-5, 6)) and result.ar.shape == result.car.shape == (40, 11)
    assert not result.overlapping.any() and not result.est_contaminated.any() and np.all(result.est_n == 120)
    assert result.beta.mean() == pytest.approx(1.3, abs=0.05)
    assert result.mean_ar[day[0]] == pytest.approx(0.02, abs=4 * noise)
    assert result.mean_ar[day[1]] == pytest.approx(0.01, abs=4 * noise)
    assert abs(result.mean_car[day[-1]]) < 4 * result.se_car[day[-1]]
    assert result.mean_car[day[5]] == pytest.approx(0.03, abs=4 * result.se_car[day[5]])
    assert np.all(result.lo[day[0]:] > 0.01) and np.all(result.hi[day[1]:] > 0.025)
    assert np.all(result.lo <= result.mean_car) and np.all(result.mean_car <= result.hi)
    assert np.allclose(result.car, np.cumsum(result.ar, axis=1)) and np.allclose(result.mean_car, result.car.mean(axis=0))
    # Band half-width should be near 1.645 cross-event standard errors.
    assert np.allclose((result.hi - result.lo) / 2, 1.645 * result.se_car, rtol=0.2)
    again = S.event_study(asset, market, events, 5, 5, overlap="drop_all", seed=5, est_len=120)
    assert np.array_equal(again.lo, result.lo) and np.array_equal(again.hi, result.hi)

    placebo_dates = events + 70
    contaminated = S.event_study(asset, market, placebo_dates, 5, 5, overlap="drop_all", seed=5, est_len=120)
    assert not contaminated.est_contaminated.any()  # flag only knows the events it was given
    placebo = S.event_study(asset, market, placebo_dates, 5, 5, overlap="drop_all", seed=5, est_len=120,
        est_exclude=S.event_window_mask(len(asset), events, 5, 5))
    assert placebo.n_events == 40 and np.all(placebo.est_n == 109)
    assert np.all(np.abs(placebo.mean_car) < 3 * placebo.se_car)
    assert np.all(np.abs(placebo.mean_car) < 0.015) and abs(placebo.mean_ar[day[0]]) < 4 * noise


def test_event_study_estimation_window_ends_before_event_window():
    asset, market = market_data(20, n=600)
    base = S.event_study(asset, market, [300], 4, 6, overlap="flag", seed=1, est_len=100)
    assert base.n_events == 1 and np.isnan(base.lo).all() and np.isnan(base.se_car).all()
    slope, intercept = np.polyfit(market[196:296], asset[196:296], 1)
    assert base.beta[0] == pytest.approx(slope) and base.alpha[0] == pytest.approx(intercept)
    assert np.allclose(base.ar[0], asset[296:307] - intercept - slope * market[296:307])

    inside = asset.copy()
    inside[296:307] += 0.5
    shifted = S.event_study(inside, market, [300], 4, 6, overlap="flag", seed=1, est_len=100)
    assert shifted.alpha[0] == base.alpha[0] and shifted.beta[0] == base.beta[0]
    assert np.allclose(shifted.ar[0], base.ar[0] + 0.5)

    before = asset.copy()
    before[293:296] += 0.5
    assert S.event_study(before, market, [300], 4, 6, overlap="flag", seed=1, est_len=100).alpha[0] != base.alpha[0]
    gapped = S.event_study(asset, market, [300], 4, 6, overlap="flag", seed=1, est_len=100, gap=3)
    gapped_shift = S.event_study(before, market, [300], 4, 6, overlap="flag", seed=1, est_len=100, gap=3)
    assert gapped.alpha[0] == gapped_shift.alpha[0] and gapped.alpha[0] != base.alpha[0]


def test_event_overlap_rules_are_explicit():
    events = [300, 103, 100, 200, 302, 304, 200]
    flagged = S.thin_events(events, 4, "flag")
    assert np.array_equal(flagged["events"], [100, 103, 200, 300, 302, 304])
    assert np.array_equal(flagged["overlapping"], [True, True, False, True, True, True])
    assert np.array_equal(flagged["kept"], flagged["events"]) and len(flagged["dropped"]) == 0
    assert np.array_equal(S.thin_events(events, 4, "drop_later")["kept"], [100, 200, 300])
    assert np.array_equal(S.thin_events(events, 4, "drop_all")["kept"], [200])
    assert np.array_equal(S.thin_events([100, 105], 4, "drop_all")["kept"], [100, 105])
    assert np.array_equal(S.thin_events([100, 104, 108], 4, "drop_later")["kept"], [100])
    with pytest.raises(ValueError):
        S.thin_events(events, 4, "largest")
    with pytest.raises(ValueError):
        S.thin_events([100.5], 4, "flag")

    asset, market = market_data(21, n=500)
    kept_all = S.event_study(asset, market, events, 2, 2, overlap="flag", seed=1, est_len=60)
    assert np.array_equal(kept_all.events, [100, 103, 200, 300, 302, 304])
    assert np.array_equal(kept_all.overlapping, [True, True, False, True, True, True])
    # 103, 302 and 304 are estimated on windows that contain an earlier event's days.
    assert np.array_equal(kept_all.est_contaminated, [False, True, False, False, True, True])
    first =S.event_study(asset, market, events, 2, 2, overlap="drop_later", seed=1, est_len=60)
    assert np.array_equal(first.events, [100, 200, 300])
    assert first.dropped == [{"index": i, "reason": "overlap"} for i in (103, 302, 304)]
    isolated = S.event_study(asset, market, events, 2, 2, overlap="drop_all", seed=1, est_len=60)
    assert np.array_equal(isolated.events, [200]) and len(isolated.dropped) == 5
    with pytest.raises(TypeError):
        S.event_study(asset, market, events, 2, 2, seed=1)
    with pytest.raises(ValueError):
        S.event_study(asset, market, events, 2, 2, overlap="none", seed=1)


def test_event_study_reports_dropped_events_and_estimation_contamination():
    asset, market = market_data(22, n=500)
    asset[250] = np.nan
    result = S.event_study(asset, market, [10, 150, 200, 249, 498], 2, 2, overlap="flag", seed=1, est_len=60)
    assert np.array_equal(result.events, [150, 200])
    assert result.dropped == [{"index": 10, "reason": "out_of_range"},
        {"index": 249, "reason": "missing_return_in_event_window"}, {"index": 498, "reason": "out_of_range"}]
    assert np.array_equal(result.est_contaminated, [False, True]) and np.array_equal(result.est_n, [60, 60])

    cleaned = S.event_study(asset, market, [150, 200, 300], 2, 2, overlap="flag", seed=1, est_len=60,
        est_exclude=S.event_window_mask(500, [150, 200, 300], 2, 2))
    # 200 loses the five days of 150's window; 300 loses only the missing return on day 250.
    assert np.array_equal(cleaned.est_n, [60, 55, 59]) and not cleaned.est_contaminated.any()
    strict = S.event_study(asset, market, [150, 200, 300], 2, 2, overlap="flag", seed=1, est_len=60, min_est=60)
    assert np.array_equal(strict.events, [150, 200])
    assert strict.dropped == [{"index": 300, "reason": "short_estimation_window"}]
    empty = S.event_study(asset, market, [], 2, 2, overlap="drop_all", seed=1)
    assert empty.n_events == 0 and empty.ar.shape == (0, 5) and np.isnan(empty.mean_car).all()


# ---------------------------------------------------------------- past-only beta

def test_rolling_beta_uses_only_past_data():
    rng = np.random.default_rng(23)
    market = rng.normal(size=300)
    asset = 0.5 * market + 0.5 * rng.normal(size=300)
    base = S.rolling_beta(asset, market, 60)
    assert np.isnan(base["beta"][:60]).all() and np.isfinite(base["beta"][60:]).all()
    assert np.array_equal(base["n_obs"][:61], np.arange(61)) and np.all(base["n_obs"][60:] == 60)
    for t in (60, 150, 299):
        slope, intercept = np.polyfit(market[t - 60:t], asset[t - 60:t], 1)
        assert base["beta"][t] == pytest.approx(slope) and base["alpha"][t] == pytest.approx(intercept)

    future_asset, future_market = asset.copy(), market.copy()
    future_asset[200:] += 5.0
    future_market[200:] *= -3.0
    changed = S.rolling_beta(future_asset, future_market, 60)
    assert np.array_equal(base["beta"][:201], changed["beta"][:201], equal_nan=True)
    assert np.array_equal(base["alpha"][:201], changed["alpha"][:201], equal_nan=True)
    assert not np.isclose(base["beta"][201], changed["beta"][201])

    exact = S.rolling_beta(1.0 + 2.0 * market, market, 20)
    assert np.allclose(exact["beta"][20:], 2.0) and np.allclose(exact["alpha"][20:], 1.0)
    early = S.rolling_beta(asset, market, 60, min_obs=10)
    assert np.isnan(early["beta"][:10]).all() and np.isfinite(early["beta"][10:]).all()
    assert early["beta"][30] == pytest.approx(np.polyfit(market[:30], asset[:30], 1)[0])

    gappy = market.copy()
    gappy[100] = np.nan
    skipped = S.rolling_beta(asset, gappy, 60, min_obs=50)
    assert skipped["n_obs"][130] == 59 and skipped["n_obs"][161] == 60
    keep = np.arange(70, 130) != 100
    assert skipped["beta"][130] == pytest.approx(np.polyfit(market[70:130][keep], asset[70:130][keep], 1)[0])


def test_expanding_beta_uses_only_past_data():
    rng = np.random.default_rng(24)
    market = rng.normal(size=200)
    asset = 1.2 * market + rng.normal(size=200)
    base = S.expanding_beta(asset, market, 20)
    assert np.isnan(base["beta"][:20]).all() and np.isfinite(base["beta"][20:]).all()
    for t in (20, 77, 199):
        assert base["beta"][t] == pytest.approx(np.polyfit(market[:t], asset[:t], 1)[0])
    future = asset.copy()
    future[120:] -= 9.0
    changed = S.expanding_beta(future, market, 20)
    assert np.array_equal(base["beta"][:121], changed["beta"][:121], equal_nan=True)
    assert not np.isclose(base["beta"][121], changed["beta"][121])


# ---------------------------------------------------------------- utilities

def test_winsorize_standardize_and_quantiles():
    values = np.arange(101.0)
    clipped = S.winsorize(values, 0.05, 0.95)
    assert clipped.min() == pytest.approx(5.0) and clipped.max() == pytest.approx(95.0) and np.array_equal(clipped[5:96], values[5:96])
    assert np.array_equal(S.winsorize([-100.0, 0.0, 100.0], 0.0, 1.0, ref=[-1.0, 1.0]), [-1.0, 0.0, 1.0])
    with_gap = S.winsorize([np.nan, -50.0, 50.0], 0.0, 1.0, ref=[-1.0, np.nan, 1.0])
    assert np.isnan(with_gap[0]) and np.array_equal(with_gap[1:], [-1.0, 1.0])
    with pytest.raises(ValueError):
        S.winsorize(values, 0.9, 0.1)

    assert np.array_equal(S.standardize([1.0, 2.0, 3.0], 2.0, 0.5), [-2.0, 0.0, 2.0])
    for bad in (0.0, -1.0, math.nan):
        with pytest.raises(ValueError):
            S.standardize([1.0, 2.0], 0.0, bad)

    assert np.allclose(S.quantiles(np.arange(11.0)), [1.0, 5.0, 9.0])
    assert np.allclose(S.quantiles([1.0, 2.0, 3.0, 4.0], [0.25, 0.5]), [1.75, 2.5])
    assert S.quantiles(np.column_stack([np.arange(11.0), np.arange(11.0) * 2])).shape == (3, 2)
    with pytest.raises(ValueError):
        S.quantiles([1.0, np.nan])
