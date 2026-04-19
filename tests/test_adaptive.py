"""Tests for zk.AdaptiveCompute — Adam-style multi-worker allocator."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from zakuro import AdaptiveCompute, Compute


def _fast_worker() -> Compute:
    return Compute(host="fast.local", port=3960, verify=False)


def _slow_worker() -> Compute:
    return Compute(host="slow.local", port=3960, verify=False)


class TestConstruction:
    def test_rejects_empty_workers(self) -> None:
        with pytest.raises(ValueError, match="at least one worker"):
            AdaptiveCompute(workers=[])

    def test_rejects_bad_beta(self) -> None:
        with pytest.raises(ValueError, match="beta1, beta2"):
            AdaptiveCompute(workers=[_fast_worker()], beta1=1.0)


class TestEMA:
    def test_first_call_bias_correct(self) -> None:
        """After one observation, bias-corrected EMA equals the observation."""
        ac = AdaptiveCompute(workers=[_fast_worker()], beta1=0.9)
        ac._update_ema(ac._stats[0], 0.5)
        assert abs(ac.stats()[0]["latency_ema"] - 0.5) < 1e-9

    def test_ema_pulls_toward_new_samples(self) -> None:
        ac = AdaptiveCompute(workers=[_fast_worker()], beta1=0.9)
        for _ in range(100):
            ac._update_ema(ac._stats[0], 1.0)
        ema_before = ac.stats()[0]["latency_ema"]
        for _ in range(30):
            ac._update_ema(ac._stats[0], 5.0)
        ema_after = ac.stats()[0]["latency_ema"]
        assert ema_before < ema_after < 5.0

    def test_variance_tracks_changes(self) -> None:
        ac = AdaptiveCompute(workers=[_fast_worker()], beta2=0.5)
        # Alternating fast/slow samples → high variance.
        for x in [0.1, 2.0, 0.1, 2.0, 0.1, 2.0]:
            ac._update_ema(ac._stats[0], x)
        assert ac.stats()[0]["latency_var"] > 0.1


class TestPicker:
    def test_greedy_prefers_fewer_queued(self) -> None:
        ac = AdaptiveCompute(
            workers=[_fast_worker(), _slow_worker()],
            beta1=0.9,
            softmax_temperature=0.0,
            initial_latency=1.0,
        )
        ac._stats[0].queue = 5  # first worker busy
        ac._stats[1].queue = 0
        assert ac.pick() == 1  # picks idle worker

    def test_greedy_prefers_faster_observed(self) -> None:
        ac = AdaptiveCompute(
            workers=[_fast_worker(), _slow_worker()],
            beta1=0.9,
            softmax_temperature=0.0,
        )
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.1)
            ac._update_ema(ac._stats[1], 5.0)
        picks = [ac.pick() for _ in range(10)]
        assert picks == [0] * 10

    def test_softmax_eventually_explores(self) -> None:
        ac = AdaptiveCompute(
            workers=[_fast_worker(), _slow_worker()],
            beta1=0.9,
            softmax_temperature=1.0,
        )
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.5)
            ac._update_ema(ac._stats[1], 1.0)
        picks = [ac.pick() for _ in range(2000)]
        # Fast worker strongly preferred but exploration must be nonzero.
        assert picks.count(0) > picks.count(1)
        assert picks.count(1) > 50


class TestBackpressure:
    def test_no_backpressure_when_idle(self) -> None:
        ac = AdaptiveCompute(
            workers=[_fast_worker()],
            backpressure_threshold=10.0,
            initial_latency=0.1,
        )
        assert not ac.is_backpressured()

    def test_backpressure_when_queues_deep(self) -> None:
        ac = AdaptiveCompute(
            workers=[_slow_worker()],
            backpressure_threshold=10.0,
            initial_latency=1.0,
        )
        for _ in range(20):
            ac._update_ema(ac._stats[0], 3.0)
        ac._stats[0].queue = 10
        assert ac.is_backpressured()


class TestNodeLifecycle:
    def test_add_worker_seeds_with_mesh_median(self) -> None:
        """A new worker's EMA should start at the mesh median, not at zero."""
        ac = AdaptiveCompute(
            workers=[_fast_worker(), _slow_worker()],
            beta1=0.9,
            initial_latency=5.0,
        )
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.5)
            ac._update_ema(ac._stats[1], 1.5)
        # Median of {~0.5, ~1.5} ≈ 1.0.
        new_idx = ac.add_worker(Compute(host="newcomer.local", verify=False))
        stats = ac.stats()[new_idx]
        assert 0.4 < stats["latency_ema"] < 2.0

    def test_remove_worker(self) -> None:
        ac = AdaptiveCompute(
            workers=[_fast_worker(), _slow_worker()],
        )
        dropped = ac.remove_worker(0)
        assert dropped.host == "fast.local"
        assert len(ac.workers) == 1

    def test_cannot_remove_last_worker(self) -> None:
        ac = AdaptiveCompute(workers=[_fast_worker()])
        with pytest.raises(ValueError, match="last worker"):
            ac.remove_worker(0)


class TestWarmup:
    def test_warmup_seeds_stats(self) -> None:
        """warmup should run probes and update per-worker EMAs."""
        # Single-worker adaptive; the worker has no URI/host → standalone
        # fallback runs the probe in-process (fast + reliable for tests).
        ac = AdaptiveCompute(
            workers=[Compute(cpus=1)],
            initial_latency=10.0,  # deliberately bad prior to see warmup fix it
        )
        with patch("zakuro.standalone.detect_backend", return_value=None):
            report = ac.warmup(rounds=3, timeout=5.0, verbose=False)

        assert report["workers"][0]["ok"]
        assert report["workers"][0]["rounds_succeeded"] == 3
        # Stats are seeded from real measurements, not the 10.0 bootstrap.
        assert ac.stats()[0]["step"] == 3
        assert ac.stats()[0]["latency_ema"] < 1.0

    def test_warmup_sets_backpressure_from_p95(self) -> None:
        ac = AdaptiveCompute(
            workers=[Compute(cpus=1)],
            backpressure_threshold=999.0,
        )
        with patch("zakuro.standalone.detect_backend", return_value=None):
            report = ac.warmup(rounds=4, verbose=False)
        assert report["applied_backpressure"] is True
        # Should now be ~1.5× observed p95, not the 999 we started with.
        assert ac.backpressure_threshold < 5.0
        assert (
            abs(ac.backpressure_threshold - 1.5 * report["workers"][0]["latency_p95"])
            < 1e-6
        )

    def test_warmup_ejects_unreachable_worker(self) -> None:
        """A worker whose probe raises every round should be dropped."""
        good = Compute(cpus=1)  # standalone-ok
        # Bad worker: explicit URI → verify at construction probe-reachable
        # won't match (unroutable). Use verify=False to delay the failure to
        # the warmup probe itself.
        bad = Compute(uri="quic://192.0.2.1:4444", verify=False)
        ac = AdaptiveCompute(workers=[good, bad])

        with patch("zakuro.standalone.detect_backend", return_value=None):
            report = ac.warmup(rounds=2, timeout=2.0, verbose=False)
        # good remains; bad gets ejected.
        assert len(ac.workers) == 1
        assert report["ejected"] == [1]
        assert any(r["ok"] is False for r in report["workers"])


class TestDispatch:
    def test_dispatch_times_and_records(self) -> None:
        """AdaptiveCompute.dispatch should run the fn and update stats."""
        import zakuro as zk

        @zk.fn
        def add(a: int, b: int) -> int:
            return a + b

        # Use a single-worker adaptive; the worker uses standalone fallback
        # (Compute with no uri/host) so dispatch runs locally.
        ac = AdaptiveCompute(
            workers=[Compute(cpus=1)],
            beta1=0.5,
            initial_latency=0.0001,
        )
        with patch("zakuro.standalone.detect_backend", return_value=None):
            result = add.to(ac)(2, 3)
        assert result == 5
        stats = ac.stats()[0]
        assert stats["step"] == 1
        assert stats["queue"] == 0
        assert stats["last_latency"] is not None
        assert stats["last_latency"] > 0

    def test_dispatch_increments_failures_on_exception(self) -> None:
        import zakuro as zk

        @zk.fn
        def boom() -> None:
            raise ValueError("nope")

        ac = AdaptiveCompute(
            workers=[Compute(cpus=1)],
            initial_latency=0.0001,
        )
        with patch("zakuro.standalone.detect_backend", return_value=None):
            with pytest.raises(ValueError, match="nope"):
                boom.to(ac)()
        assert ac.stats()[0]["failures"] == 1
