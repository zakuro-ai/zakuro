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
        assert abs(ac.backpressure_threshold - 1.5 * report["workers"][0]["latency_p95"]) < 1e-6

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


class TestDriftDetection:
    def test_stable_worker_not_drifted(self) -> None:
        """Consistent latencies keep drift_factor at 1.0."""
        ac = AdaptiveCompute(
            workers=[_fast_worker()],
            beta1=0.8,
            beta_slow=0.99,
            drift_threshold=2.0,
        )
        for _ in range(50):
            ac._update_ema(ac._stats[0], 0.1)
        assert ac.stats()[0]["drift_factor"] == 1.0

    def test_sustained_slowdown_marks_drift(self) -> None:
        """A worker whose fast EMA climbs past drift_threshold × baseline
        should be soft-demoted."""
        ac = AdaptiveCompute(
            workers=[_fast_worker()],
            beta1=0.7,  # responsive fast EMA
            beta_slow=0.95,  # slower baseline
            drift_threshold=1.5,
            drift_penalty=5.0,
        )
        # Establish baseline.
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.1)
        assert ac.stats()[0]["drift_factor"] == 1.0

        # Inject 5× slowdown; baseline lags behind so ratio crosses 1.5.
        for _ in range(15):
            ac._update_ema(ac._stats[0], 0.5)
        stats = ac.stats()[0]
        assert stats["drift_factor"] == 5.0
        # Expected time in picker = (queue+1) * ema * drift_factor → huge.
        assert ac._expected_times_locked()[0] > 1.0

    def test_recovery_clears_drift(self) -> None:
        """Returning to baseline should clear the demotion."""
        ac = AdaptiveCompute(
            workers=[_fast_worker()],
            beta1=0.7,
            beta_slow=0.95,
            drift_threshold=1.5,
            drift_recovery_threshold=1.1,
        )
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.1)
        for _ in range(15):
            ac._update_ema(ac._stats[0], 0.5)
        assert ac.stats()[0]["drift_factor"] == 5.0

        # Return to fast latencies.
        for _ in range(60):
            ac._update_ema(ac._stats[0], 0.1)
        assert ac.stats()[0]["drift_factor"] == 1.0

    def test_drift_deflects_picks(self) -> None:
        """In a 2-worker pool, only the drifted side is skipped."""
        ac = AdaptiveCompute(
            workers=[_fast_worker(), _slow_worker()],
            beta1=0.7,
            beta_slow=0.95,
            drift_threshold=1.5,
        )
        # Baseline both: worker 0 fast, worker 1 fast too.
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.1)
            ac._update_ema(ac._stats[1], 0.1)
        # Drift worker 0.
        for _ in range(15):
            ac._update_ema(ac._stats[0], 1.0)

        picks = [ac.pick() for _ in range(50)]
        # All picks should go to the healthy worker 1.
        assert picks.count(1) == 50


class TestHealthProbe:
    def test_probe_miss_marks_strike(self) -> None:
        """A probe that fails (unreachable host) increments health_strikes."""
        # 192.0.2.1 is TEST-NET-1, guaranteed unroutable → /health fails.
        compute = Compute(host="192.0.2.1", port=3960, verify=False)
        ac = AdaptiveCompute(workers=[compute])
        ac._probe_timeout = 0.5  # don't sit for the HTTP default.

        results = ac.probe_once()
        assert results[0]["ok"] is False
        stats = ac.stats()[0]
        assert stats["health_strikes"] == 1
        assert stats["suspended"] is False  # only one strike so far

    def test_suspension_after_max_strikes(self) -> None:
        bad = Compute(host="192.0.2.1", port=3960, verify=False)
        good = _fast_worker()
        ac = AdaptiveCompute(workers=[bad, good])
        ac._probe_timeout = 0.5
        ac._max_strikes = 2

        def fake_probe(compute):
            if compute is bad:
                return (False, "unreachable", None)
            return (True, None, 0.005)

        with patch.object(AdaptiveCompute, "_probe", side_effect=fake_probe):
            ac.probe_once()
            ac.probe_once()
        assert ac.stats()[0]["suspended"] is True
        assert ac.stats()[1]["suspended"] is False
        # Suspended worker is sent to inf in the picker, so all traffic
        # goes to the healthy worker.
        for _ in range(20):
            assert ac.pick() == 1

    def test_recovery_unsuspends(self) -> None:
        """A successful probe after suspension should unsuspend and reset strikes."""
        ac = AdaptiveCompute(workers=[_fast_worker()])
        ac._stats[0].suspended = True
        ac._stats[0].health_strikes = 5
        with patch.object(
            AdaptiveCompute,
            "_probe",
            return_value=(True, None, 0.012),
        ):
            ac.probe_once()
        stats = ac.stats()[0]
        assert stats["suspended"] is False
        assert stats["health_strikes"] == 0

    def test_start_and_stop_probe_thread(self) -> None:
        """start_health_probes spawns a thread; stop_health_probes reaps it."""
        compute = Compute(host="127.0.0.1", port=3960, verify=False)
        ac = AdaptiveCompute(workers=[compute])
        ac.start_health_probes(interval=0.1, timeout=0.1, max_strikes=5)
        assert ac._probe_thread is not None and ac._probe_thread.is_alive()
        time.sleep(0.3)  # let it probe at least once
        ac.stop_health_probes(timeout=1.0)
        assert ac._probe_thread is None


class TestPriceAware:
    def test_zero_coef_ignores_price(self) -> None:
        a = Compute(host="a.local", verify=False, price_per_hour=10.0)
        b = Compute(host="b.local", verify=False, price_per_hour=0.1)
        ac = AdaptiveCompute(workers=[a, b], cost_coefficient=0.0)
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.01)
            ac._update_ema(ac._stats[1], 0.01)
        picks = [ac.pick() for _ in range(20)]
        # Latencies equal, no cost weighting → first-index argmin tie-break.
        assert all(p == 0 for p in picks)

    def test_positive_coef_prefers_cheaper(self) -> None:
        expensive = Compute(host="a.local", verify=False, price_per_hour=10.0)
        cheap = Compute(host="b.local", verify=False, price_per_hour=0.1)
        ac = AdaptiveCompute(
            workers=[expensive, cheap],
            cost_coefficient=0.5,
            softmax_temperature=0.0,
        )
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.01)
            ac._update_ema(ac._stats[1], 0.01)
        # Equal latency + cost-aware → always picks the cheap worker.
        picks = [ac.pick() for _ in range(20)]
        assert all(p == 1 for p in picks)


class TestTopologyHints:
    def test_same_region_wins_on_tie(self) -> None:
        """Equal latency, differing region → picker prefers local region."""
        local = Compute(host="a.local", verify=False, region="us-west")
        remote = Compute(host="b.local", verify=False, region="us-east")
        ac = AdaptiveCompute(
            workers=[remote, local],  # put the remote at idx 0
            local_region="us-west",
            softmax_temperature=0.0,
        )
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.01)
            ac._update_ema(ac._stats[1], 0.01)
        # Despite idx 0 being the first-index tiebreaker, the local worker
        # (idx 1) wins because the region penalty stretches remote's
        # expected time.
        assert all(ac.pick() == 1 for _ in range(20))

    def test_faster_remote_still_wins(self) -> None:
        """A much-faster remote should beat a slow local — penalty is soft."""
        slow_local = Compute(host="a.local", verify=False, region="us-west")
        fast_remote = Compute(host="b.local", verify=False, region="us-east")
        ac = AdaptiveCompute(
            workers=[slow_local, fast_remote],
            local_region="us-west",
            softmax_temperature=0.0,
            region_penalty=1.10,
        )
        # 10× latency difference dwarfs the 10 % region penalty.
        for _ in range(30):
            ac._update_ema(ac._stats[0], 1.0)
            ac._update_ema(ac._stats[1], 0.01)
        assert all(ac.pick() == 1 for _ in range(20))

    def test_no_local_region_is_a_noop(self) -> None:
        """Absent local_region, topology tags on workers are ignored."""
        a = Compute(host="a.local", verify=False, region="r1")
        b = Compute(host="b.local", verify=False, region="r2")
        ac = AdaptiveCompute(workers=[a, b], softmax_temperature=0.0)
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.01)
            ac._update_ema(ac._stats[1], 0.01)
        assert all(ac.pick() == 0 for _ in range(20))

    def test_rack_preference_within_region(self) -> None:
        """Same region, differing rack → prefer same-rack on tie."""
        cross_rack = Compute(host="a.local", verify=False, region="r", rack="R2")
        same_rack = Compute(host="b.local", verify=False, region="r", rack="R1")
        ac = AdaptiveCompute(
            workers=[cross_rack, same_rack],
            local_region="r",
            local_rack="R1",
            softmax_temperature=0.0,
        )
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.01)
            ac._update_ema(ac._stats[1], 0.01)
        assert all(ac.pick() == 1 for _ in range(20))

    def test_env_var_seeds_local_region(self, monkeypatch) -> None:
        """$ZAKURO_REGION populates local_region when not passed explicitly."""
        monkeypatch.setenv("ZAKURO_REGION", "us-west")
        local = Compute(host="a.local", verify=False, region="us-west")
        remote = Compute(host="b.local", verify=False, region="us-east")
        ac = AdaptiveCompute(workers=[remote, local], softmax_temperature=0.0)
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.01)
            ac._update_ema(ac._stats[1], 0.01)
        assert all(ac.pick() == 1 for _ in range(20))


class TestBandwidthAware:
    def test_picker_prefers_higher_bandwidth_for_big_payloads(self) -> None:
        """Same latency, different bandwidth: big payloads pick the fatter pipe."""
        a, b = _fast_worker(), _slow_worker()
        ac = AdaptiveCompute(workers=[a, b], softmax_temperature=0.0)
        # Warm both workers to the same latency.
        for _ in range(30):
            ac._update_ema(ac._stats[0], 0.01)
            ac._update_ema(ac._stats[1], 0.01)
        # Worker 0 is 1 Gbps, worker 1 is 10 Mbps.
        ac._stats[0].bandwidth_bps = 1e9
        ac._stats[1].bandwidth_bps = 1e7

        # Small payload: latency dominates, either worker is fine (we expect
        # worker 0 as tiebreaker).
        assert ac.pick(payload_bytes=1_000) == 0
        # Large payload: 100 MiB → worker 1's transfer time = ~80 s, worker 0
        # = ~0.8 s. Worker 0 wins by a mile.
        assert ac.pick(payload_bytes=100 * 1024 * 1024) == 0

    def test_warmup_seeds_bandwidth_estimate(self, tmp_path) -> None:
        """warmup should attach a bandwidth_bps estimate when probing."""
        from unittest.mock import patch as _patch

        ac = AdaptiveCompute(workers=[Compute(cpus=1)])
        with _patch("zakuro.standalone.detect_backend", return_value=None):
            report = ac.warmup(
                rounds=3,
                bandwidth_probe_bytes=64 * 1024,
                verbose=False,
            )
        assert report["workers"][0]["ok"] is True
        # In standalone the "network" is a no-op — transfer should look
        # very fast, so bandwidth estimate should be positive (finite) and
        # large (standalone has no real wire).
        bw = report["workers"][0]["bandwidth_bps"]
        assert bw is not None
        assert bw > 1e4  # at least 10 KB/s plausibly in-process


class TestDecisionLog:
    def test_log_write_enabled(self, tmp_path) -> None:
        """Every dispatch should append one JSON line with prediction + outcome."""
        import json as _json

        import zakuro as zk

        @zk.fn
        def identity(x):
            return x

        log_path = tmp_path / "allocator.jsonl"
        ac = AdaptiveCompute(workers=[Compute(cpus=1)], initial_latency=0.0001)
        ac.enable_decision_log(str(log_path))

        with patch("zakuro.standalone.detect_backend", return_value=None):
            for i in range(5):
                identity.to(ac)(i)

        # Block until the background writer has drained — the JSONL file
        # is only guaranteed populated after this. Hot dispatch path never
        # calls this; only tests + replay-tool wrappers do.
        ac.flush_decision_log()
        rows = [_json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert len(rows) == 5
        for r in rows:
            assert r["fn"] == "identity"
            assert r["picked"] == 0
            assert r["ok"] is True
            assert r["actual_secs"] > 0
            assert "expected_secs" in r
            assert r["error"] is None

    def test_log_disabled_does_not_write(self, tmp_path) -> None:
        import zakuro as zk

        @zk.fn
        def noop():
            return None

        log_path = tmp_path / "should-not-exist.jsonl"
        ac = AdaptiveCompute(workers=[Compute(cpus=1)])

        with patch("zakuro.standalone.detect_backend", return_value=None):
            noop.to(ac)()

        assert not log_path.exists()


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
        with (
            patch("zakuro.standalone.detect_backend", return_value=None),
            pytest.raises(ValueError, match="nope"),
        ):
            boom.to(ac)()
        assert ac.stats()[0]["failures"] == 1


class _ScriptedFn:
    """Minimal stand-in for ``zakuro.fn.Fn`` honouring the contract that
    :meth:`AdaptiveCompute.dispatch` relies on: ``_func.__name__``, ``.to()``,
    and ``_execute_single_compute()``. Each call consumes the next scripted
    outcome — ``("ok", value)`` returns it, ``("fail", exc)`` raises it — and
    records which Compute it was pointed at, so tests can assert worker choice.
    """

    def __init__(self, outcomes: list[tuple[str, object]]) -> None:
        self._outcomes = list(outcomes)
        self._compute: object = None
        self.targets: list[object] = []

        def _f() -> None:  # pragma: no cover - only its __name__ is read
            ...

        _f.__name__ = "scripted"
        self._func = _f

    def to(self, compute: object) -> _ScriptedFn:
        self._compute = compute
        return self

    def _execute_single_compute(self, *args: object, **kwargs: object) -> object:
        self.targets.append(self._compute)
        kind, payload = self._outcomes.pop(0)
        if kind == "fail":
            raise payload  # type: ignore[misc]
        return payload


class TestKnobValidation:
    def test_rejects_negative_retries(self) -> None:
        with pytest.raises(ValueError, match="max_dispatch_retries"):
            AdaptiveCompute(workers=[_fast_worker()], max_dispatch_retries=-1)

    def test_rejects_negative_eject(self) -> None:
        with pytest.raises(ValueError, match="eject_after_failures"):
            AdaptiveCompute(workers=[_fast_worker()], eject_after_failures=-1)


class TestSeed:
    def test_same_seed_same_pick_sequence(self) -> None:
        workers = [_fast_worker(), _slow_worker(), _fast_worker()]
        a = AdaptiveCompute(workers=workers, softmax_temperature=1.0, seed=7)
        b = AdaptiveCompute(workers=workers, softmax_temperature=1.0, seed=7)
        assert [a._pick_locked() for _ in range(50)] == [b._pick_locked() for _ in range(50)]

    def test_different_seed_diverges(self) -> None:
        workers = [_fast_worker(), _slow_worker(), _fast_worker()]
        a = AdaptiveCompute(workers=workers, softmax_temperature=1.0, seed=1)
        b = AdaptiveCompute(workers=workers, softmax_temperature=1.0, seed=2)
        assert [a._pick_locked() for _ in range(50)] != [b._pick_locked() for _ in range(50)]


class TestDispatchRetry:
    def _ac(self, n: int, **kw: object) -> AdaptiveCompute:
        workers = [Compute(host=f"w{i}.local", port=3960, verify=False) for i in range(n)]
        return AdaptiveCompute(workers=workers, softmax_temperature=0.0, initial_latency=1.0, **kw)

    def test_retry_lands_on_a_different_worker(self) -> None:
        ac = self._ac(2, max_dispatch_retries=1)
        fn = _ScriptedFn([("fail", RuntimeError("worker0 down")), ("ok", 99)])
        assert ac.dispatch(fn, (), {}) == 99
        # First attempt → worker 0 (greedy tie-break), retry excludes it → worker 1.
        assert ac.workers.index(fn.targets[0]) == 0  # type: ignore[arg-type]
        assert ac.workers.index(fn.targets[1]) == 1  # type: ignore[arg-type]
        assert ac.stats()[0]["failures"] == 1
        assert ac.stats()[1]["failures"] == 0

    def test_raises_last_exception_when_retries_exhausted(self) -> None:
        ac = self._ac(2, max_dispatch_retries=1)
        last = RuntimeError("second")
        fn = _ScriptedFn([("fail", RuntimeError("first")), ("fail", last)])
        with pytest.raises(RuntimeError, match="second"):
            ac.dispatch(fn, (), {})
        assert ac.stats()[0]["failures"] == 1
        assert ac.stats()[1]["failures"] == 1

    def test_no_retry_by_default(self) -> None:
        ac = self._ac(2)  # max_dispatch_retries=0
        fn = _ScriptedFn([("fail", ValueError("once"))])
        with pytest.raises(ValueError, match="once"):
            ac.dispatch(fn, (), {})
        assert len(fn.targets) == 1  # single attempt only


class TestDispatchEject:
    def _ac(self, n: int, **kw: object) -> AdaptiveCompute:
        workers = [Compute(host=f"w{i}.local", port=3960, verify=False) for i in range(n)]
        return AdaptiveCompute(workers=workers, softmax_temperature=0.0, initial_latency=1.0, **kw)

    def test_suspends_after_consecutive_failures(self) -> None:
        ac = self._ac(1, eject_after_failures=2)
        for _ in range(2):
            fn = _ScriptedFn([("fail", RuntimeError("boom"))])
            with pytest.raises(RuntimeError):
                ac.dispatch(fn, (), {})
        assert ac.stats()[0]["suspended"] is True

    def test_success_resets_consecutive_failures(self) -> None:
        ac = self._ac(1, eject_after_failures=2)
        fn = _ScriptedFn([("fail", RuntimeError("boom"))])
        with pytest.raises(RuntimeError):
            ac.dispatch(fn, (), {})
        ac.dispatch(_ScriptedFn([("ok", 1)]), (), {})  # resets the streak
        fn = _ScriptedFn([("fail", RuntimeError("boom"))])
        with pytest.raises(RuntimeError):
            ac.dispatch(fn, (), {})
        # Two non-consecutive failures must not eject.
        assert ac.stats()[0]["suspended"] is False

    def test_default_never_suspends_on_dispatch_failure(self) -> None:
        ac = self._ac(1)  # eject_after_failures=0
        for _ in range(5):
            with pytest.raises(RuntimeError):
                ac.dispatch(_ScriptedFn([("fail", RuntimeError("boom"))]), (), {})
        assert ac.stats()[0]["suspended"] is False
