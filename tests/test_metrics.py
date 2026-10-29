import pytest

from vc_crm.metrics import allocate, xirr


def test_xirr_doubling_over_one_year():
    assert xirr([("2025-01-01", -100), ("2026-01-01", 200)]) == pytest.approx(1.0, abs=1e-3)


def test_xirr_loss():
    assert xirr([("2024-01-01", -100), ("2026-01-01", 50)]) == pytest.approx(-0.2929, abs=1e-3)


def test_xirr_undefined_without_sign_change():
    assert xirr([("2024-01-01", -100), ("2025-01-01", -50)]) is None
    assert xirr([("2024-01-01", -100)]) is None


def test_xirr_very_high_short_term_return():
    r = xirr([("2025-01-01", -100), ("2025-03-01", 300)])
    assert r == pytest.approx(3 ** (365 / 59) - 1, rel=1e-4)


def test_allocate_sums_exactly_to_the_cent():
    out = allocate(100.00, {1: 1, 2: 1, 3: 1})
    assert sum(out.values()) == pytest.approx(100.00)
    assert sorted(out.values()) == [33.33, 33.33, 33.34]


def test_allocate_pro_rata():
    out = allocate(1_000_000, {10: 7_500_000, 11: 2_500_000})
    assert out == {10: 750_000.0, 11: 250_000.0}


def test_allocate_rejects_zero_weights():
    with pytest.raises(ValueError):
        allocate(10, {1: 0})
