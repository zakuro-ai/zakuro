"""ZAK-0019: opt-in decision log for the Task dispatch path."""

from __future__ import annotations

import json
import struct

import cloudpickle
import httpx
import pytest

import zakuro as zk
from zakuro.mesh import NodeInfo
from zakuro.replay import format_report


def _node(i: int) -> NodeInfo:
    return NodeInfo(
        endpoint=f"192.0.2.{i}:9000", node_id=f"zc://node-{i}", name=f"n{i}", workers={f"w{i}"}
    )


def _fn(x):
    return x


class _Client:
    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, content=None, headers=None):
        return self.handler(url, content, headers)


def _ids(frame):
    n = struct.unpack_from("<I", frame, 4)[0]
    off, out = 8, []
    for _ in range(n):
        jid, rl = struct.unpack_from("<II", frame, off)
        pl = struct.unpack_from("<I", frame, off + 8 + rl)[0]
        out.append(jid)
        off += 12 + rl + pl
    return out


def _reply(content, cost="0.5"):
    ok = cloudpickle.dumps("ok")
    body = b""
    for job_id in _ids(content):
        body += struct.pack("<I", job_id) + bytes([0]) + struct.pack("<I", len(ok)) + ok
    return httpx.Response(
        200,
        content=body,
        headers={"X-Zakuro-Cost": cost, "X-Zakuro-Service-Ms": "12", "X-Zakuro-Worker": "w"},
    )


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setattr("zakuro.mesh.api_key", lambda: "sk-SECRET-KEY")


def _run(monkeypatch, task, handler):
    monkeypatch.setattr(httpx, "Client", lambda **kw: _Client(handler))
    return task.run(nodes=[_node(1), _node(2), _node(3)])


def _task(n=6):
    t = zk.Task(placement="even")
    t.extend(_fn, [(i,) for i in range(n)])
    return t


def test_k_rows_for_n_jobs_over_k_nodes_and_replay(tmp_path, monkeypatch, secret):
    log = tmp_path / "d.jsonl"
    t = _task(6)
    t.enable_decision_log(str(log))
    _run(monkeypatch, t, lambda url, c, h: _reply(c))
    rows = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(rows) == 3
    assert {r["kind"] for r in rows} == {"task_batch"}
    assert sum(r["jobs"] for r in rows) == 6
    for k in (
        "node_id",
        "worker",
        "jobs",
        "cost",
        "remaining",
        "service_ms",
        "placement",
        "python",
        "outcome",
    ):
        assert k in rows[0]
    assert all(r["outcome"] == "ok" for r in rows)
    s = zk.replay_decisions(log)
    assert s["task_batches"]["batches"] == 3
    assert s["task_batches"]["jobs"] == 6
    assert s["task_batches"]["cost"] == pytest.approx(1.5)
    assert len(s["task_batches"]["nodes"]) == 3
    assert s["total"] == 0


def test_failures_are_logged_and_replayed(tmp_path, monkeypatch, secret):
    log = tmp_path / "d.jsonl"
    t = _task(3)
    t.enable_decision_log(str(log))

    def handler(url, c, h):
        if "192.0.2.2" in url:
            raise httpx.ConnectError("down")
        return _reply(c)

    _run(monkeypatch, t, handler)
    s = zk.replay_decisions(log)
    assert s["task_batches"]["failures"] == 1
    bad = [n for n in s["task_batches"]["nodes"] if n["failures"]]
    assert bad[0]["node_id"] == "zc://node-2"
    assert "unreachable" in log.read_text()


def test_no_key_or_payload_in_rows(tmp_path, monkeypatch, secret):
    log = tmp_path / "d.jsonl"
    t = zk.Task(placement="even")
    t.extend(_fn, [("PAYLOAD-MARKER-xyz",)] * 3)
    t.enable_decision_log(str(log))
    _run(monkeypatch, t, lambda url, c, h: _reply(c))
    text = log.read_text()
    assert text
    assert "sk-SECRET-KEY" not in text
    assert "PAYLOAD-MARKER" not in text


def test_disabled_by_default_does_no_io(tmp_path, monkeypatch, secret):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ZAKURO_DECISION_LOG", raising=False)
    t = _task(3)
    _run(monkeypatch, t, lambda url, c, h: _reply(c))
    assert t._log_path is None
    assert list(tmp_path.iterdir()) == []


def test_legacy_rows_without_kind_are_adaptive(tmp_path):
    log = tmp_path / "d.jsonl"
    log.write_text(
        json.dumps({"t": 1.0, "fn": "f", "picked": 0, "ok": True})
        + "\n"
        + json.dumps(
            {
                "kind": "task_batch",
                "t": 2.0,
                "node_id": "n",
                "jobs": 2,
                "ok_jobs": 2,
                "cost": 0.1,
                "outcome": "ok",
            }
        )
        + "\n"
    )
    s = zk.replay_decisions(log)
    assert s["total"] == 1 and s["ok"] == 1
    assert s["task_batches"]["jobs"] == 2
    assert "task batches" in format_report(s)
