"""How a Task divides its jobs between nodes.

Round-robin is the right default and the wrong steady state. A mesh is not
uniform -- a 4090 node and a Pascal node are both "a node" -- and a Task waits
for all of its batches, so an even split makes every Task as slow as its
slowest member. Handing the fast node more work is worth real wall clock.

The constraint is that planning must stay **free**. Asking each node how fast
it is would spend, in round trips, exactly what batching saved. So nothing is
asked: every batch reply already reports `X-Zakuro-Service-Ms`, and the client
already knows how long it waited. Placement is learned from the batches a
process has already run and paid for.

Which leaves the usual danger of any feedback loop -- a node that draws a slow
batch early gets less work, so it is measured less, so it never recovers. Two
things prevent that: rates are an EWMA rather than a verdict on one sample,
and every node in the plan keeps at least one job whenever there are jobs to
spare. A node is never written off, only weighted.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

__all__ = ["Observations", "OBSERVED", "allocate", "deal"]

#: Weight of the newest sample. Low enough that one unlucky batch does not
#: rewrite a node's reputation, high enough to follow a machine that has
#: genuinely changed -- picked up a GPU job, lost a link, been restarted.
_ALPHA = 0.3


@dataclass
class NodeRate:
    """What a node has been seen to do, in jobs per second."""

    jobs_per_second: float
    samples: int


class Observations:
    """Per-node throughput, learned from batches already dispatched.

    Process-level and shared between Tasks on purpose: the second Task of a
    session should not have to rediscover what the first one measured.
    """

    def __init__(self) -> None:
        self._rates: dict[str, NodeRate] = {}
        self._lock = threading.Lock()

    def record(self, node_id: str, jobs: int, seconds: float) -> None:
        """Fold one batch into a node's rate."""
        if jobs <= 0 or seconds <= 0:
            return
        observed = jobs / seconds
        with self._lock:
            prior = self._rates.get(node_id)
            if prior is None:
                self._rates[node_id] = NodeRate(observed, 1)
            else:
                blended = (1 - _ALPHA) * prior.jobs_per_second + _ALPHA * observed
                self._rates[node_id] = NodeRate(blended, prior.samples + 1)

    def rate(self, node_id: str) -> float | None:
        with self._lock:
            found = self._rates.get(node_id)
        return found.jobs_per_second if found else None

    def reset(self) -> None:
        with self._lock:
            self._rates.clear()

    def weights(self, node_ids: list[str]) -> list[float] | None:
        """Relative shares for these nodes, or `None` when nothing is known.

        `None` rather than a list of ones, so the caller can fall back to a
        plain even split instead of pretending an even split is a measurement.

        A node nobody has measured yet is given the mean of those that have
        been. Guessing it is fast would flood it; guessing it is slow would
        starve it of the work that would prove otherwise.
        """
        with self._lock:
            known = [self._rates[n].jobs_per_second for n in node_ids if n in self._rates]
        if not known:
            return None
        mean = sum(known) / len(known)
        with self._lock:
            return [self._rates[n].jobs_per_second if n in self._rates else mean for n in node_ids]


#: The rates this process has learned. Shared by every Task.
OBSERVED = Observations()


def allocate(n_jobs: int, n_nodes: int, weights: list[float] | None = None) -> list[int]:
    """Split `n_jobs` across `n_nodes`, in proportion to `weights`.

    `weights` of `None` means nothing has been measured yet, and the split is
    even -- exactly what round-robin gave. The node count is passed separately
    precisely so that case still knows how many ways to divide.

    Largest-remainder, so the split is exact (the parts always sum to
    `n_jobs`) and deterministic for the same inputs -- a plan you can read,
    re-read and get the same answer from.

    Every node keeps at least one job while there are enough to go round.
    A node with no work produces no measurement, and a node that produces no
    measurement never gets work again.
    """
    count = n_nodes
    if count <= 0 or n_jobs <= 0:
        return [0] * max(count, 0)
    if weights is None or len(weights) != count or sum(w for w in weights if w > 0) <= 0:
        # Nothing measured: an even split, which is what round-robin gives.
        return [n_jobs // count + (1 if i < n_jobs % count else 0) for i in range(count)]

    floor_each = 1 if n_jobs >= count else 0
    spare = n_jobs - floor_each * count
    total = sum(max(w, 0.0) for w in weights)
    exact = [max(w, 0.0) / total * spare for w in weights]
    shares = [int(e) for e in exact]

    # Hand out what rounding dropped, biggest fractional part first; ties by
    # node order so the result never depends on dict iteration.
    left = spare - sum(shares)
    order = sorted(range(count), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in order[:left]:
        shares[i] += 1
    return [s + floor_each for s in shares]


def deal(n_jobs: int, shares: list[int]) -> list[list[int]]:
    """Hand out job indices so each node gets `shares[i]` of them, interleaved.

    Interleaved rather than sliced into blocks. Jobs are very often generated
    in order -- `extend(f, [(i,) for i in range(n)])` -- and when cost tracks
    the input, contiguous blocks hand one node the whole expensive tail. That
    turns an even split into an uneven one and it would not show up in any
    count of jobs or requests.

    Deterministic: the same shares always deal the same way.
    """
    count = len(shares)
    out: list[list[int]] = [[] for _ in range(count)]
    if count == 0:
        return out
    left = list(shares)
    node = 0
    for i in range(min(n_jobs, sum(shares))):
        spins = 0
        while left[node] == 0 and spins <= count:
            node = (node + 1) % count
            spins += 1
        if left[node] == 0:
            break  # nothing left to give anywhere
        out[node].append(i)
        left[node] -= 1
        node = (node + 1) % count
    return out
