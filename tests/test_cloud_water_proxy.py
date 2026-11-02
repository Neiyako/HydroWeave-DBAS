import numpy as np

from dbas.analysis.cloud_water_proxy import (
    error_metrics, formula_audit, precipitable_water, relative_humidity,
    sub_ground_mask, trapezoid_weights, weighted_mean,
)


def test_trapezoid_and_pw_constant_profile():
    p = np.array([100., 200., 300.])
    assert np.allclose(trapezoid_weights(p), [50., 100., 50.])
    assert np.isclose(precipitable_water(np.ones((1, 3)) * .001, p), 2.03946, rtol=1e-4)


def test_relative_humidity_ice_differs_cold():
    q = np.array([.001])
    t = np.array([243.15])
    p = np.array([500.])
    assert relative_humidity(q, t, p, "ice")[0] > relative_humidity(q, t, p, "water")[0]


def test_ground_mask_tolerance_and_weighted_mean():
    q = np.array([[[[1.]], [[1.0005]], [[2.]], [[3.]]]])
    ok, surf = sub_ground_mask(q)
    assert surf.shape == (1, 1, 1)
    assert ok.shape == q.shape
    assert np.isclose(weighted_mean(np.array([1., 3.]), np.array([1., 1.])), 2.)


def test_error_metrics_and_audit():
    m = error_metrics(np.array([1., 2., np.nan]), np.array([2., 2., 4.]))
    assert m["n"] == 2 and np.isclose(m["bias"], .5)
    a = formula_audit({"q": 1, "t": 1, "level": 1})
    assert a["PW"]["status"] == "available"
    assert a["GCWR"]["status"] == "INSUFFICIENT_EVIDENCE"
