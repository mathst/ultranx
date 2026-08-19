"""Testes do estimador de tempo restante, com relógio falso."""

from __future__ import annotations

import pytest

from ultranx.core.progress import RateEstimator, format_duration, format_rate


class FakeClock:
    """Relógio controlado pelo teste: nada aqui depende de tempo real."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def test_first_sample_cannot_estimate(clock: FakeClock):
    estimator = RateEstimator(clock=clock)
    assert estimator.update(0, 1000) is None
    assert estimator.rate is None


def test_estimates_after_steady_progress(clock: FakeClock):
    """100 unidades/s com 1000 no total: aos 200 faltam ~800 -> ~8 s."""
    estimator = RateEstimator(clock=clock)
    estimator.update(0, 1000)
    for done in (100, 200):
        clock.advance(1.0)
        eta = estimator.update(done, 1000)

    assert estimator.rate == pytest.approx(100.0, rel=0.01)
    assert eta == pytest.approx(8.0, rel=0.01)


def test_no_estimate_without_total(clock: FakeClock):
    estimator = RateEstimator(clock=clock)
    estimator.update(0, None)
    clock.advance(1.0)
    assert estimator.update(500, None) is None


def test_no_estimate_below_minimum_progress(clock: FakeClock):
    """No primeiro 1% qualquer conta erra por ordens de magnitude."""
    estimator = RateEstimator(clock=clock)
    estimator.update(0, 100_000)
    clock.advance(1.0)
    assert estimator.update(500, 100_000) is None  # 0,5% do total


def test_samples_too_close_together_are_ignored(clock: FakeClock):
    """Chunk vindo de cache não deve inflar a vazão medida."""
    estimator = RateEstimator(clock=clock)
    estimator.update(0, 1000)
    clock.advance(0.05)
    estimator.update(500, 1000)
    assert estimator.rate is None


def test_rate_is_smoothed_not_replaced(clock: FakeClock):
    """Queda brusca de vazão move a estimativa aos poucos, sem pular."""
    estimator = RateEstimator(clock=clock)
    estimator.update(0, 10_000)
    clock.advance(1.0)
    estimator.update(1000, 10_000)  # 1000/s
    assert estimator.rate == pytest.approx(1000.0)

    clock.advance(1.0)
    estimator.update(1100, 10_000)  # amostra de 100/s
    # 1000 * 0.75 + 100 * 0.25 = 775
    assert estimator.rate == pytest.approx(775.0)


def test_completed_stage_estimates_zero(clock: FakeClock):
    estimator = RateEstimator(clock=clock)
    estimator.update(0, 1000)
    clock.advance(1.0)
    assert estimator.update(1000, 1000) == 0.0


def test_elapsed_tracks_stage_duration(clock: FakeClock):
    estimator = RateEstimator(clock=clock)
    assert estimator.elapsed() == 0.0
    estimator.update(0, 100)
    clock.advance(12.5)
    assert estimator.elapsed() == pytest.approx(12.5)


def test_reset_clears_state(clock: FakeClock):
    estimator = RateEstimator(clock=clock)
    estimator.update(0, 1000)
    clock.advance(1.0)
    estimator.update(500, 1000)

    estimator.reset()

    assert estimator.rate is None
    assert estimator.elapsed() == 0.0
    assert estimator.update(500, 1000) is None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "calculando…"),
        (0, "poucos segundos"),
        (5, "poucos segundos"),
        (45, "45 s"),
        (61, "2 min"),  # arredonda para cima: prometer menos
        (120, "2 min"),
        (3600, "1 h"),
        (4320, "1 h 12 min"),
        (-10, "poucos segundos"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_rate():
    assert format_rate(2 * 1024 * 1024) == "2.0 MB/s"
    assert format_rate(None) == ""
    assert format_rate(0) == ""
    assert format_rate(12.0, unit="itens/s") == "12.0 itens/s"
