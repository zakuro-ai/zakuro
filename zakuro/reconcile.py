"""What a Task was charged, against what its balance actually did.

A distributed ledger can disagree with itself. A broker answers with what it
charged; the account it charged against lives somewhere else on the mesh, on
whichever broker holds that user's shard. Those two numbers are produced by
different machines and there was nothing in the SDK that ever compared them,
so a batch could be billed twice, or not at all, and look identical from here.

The comparison is **free**. Every `/execute/batch` reply already carries both
halves:

    X-Zakuro-Cost                 what this batch cost
    X-Zakuro-Credits-Remaining    what the account holds now

So the balance before a batch is `remaining + cost`, and reconciling a whole
Task costs zero extra requests -- which is the only reason to do it by default
on a network where the round trip is the expensive part.

`reconcile_every` adds sampling *during* a long Task. That one does cost a
request per tick, so it is opt-in and priced per Task, never per job.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["Charge", "Reconciliation", "BalancePoller", "dashboard_balance", "settled_balance"]

#: Credits are floats over HTTP. Anything under this is representation noise,
#: not a real disagreement.
_EPSILON = 1e-6


@dataclass(frozen=True)
class Charge:
    """One broker's account of one batch, taken from the reply headers."""

    node_name: str
    batch_id: str
    jobs: int
    cost: float
    #: The account balance after this batch, as that broker saw it. `None`
    #: from a broker too old to send the header -- reconciliation then reports
    #: itself unavailable rather than inventing a number.
    remaining: float | None
    service_ms: float | None
    at: float
    #: Which worker ran the batch (`X-Zakuro-Worker`), to check a pinned Task
    #: against :attr:`NodeInfo.workers`. `None` from a broker that does not
    #: say -- placement is then unknown, not assumed.
    worker: str | None = None
    #: The ``zc://node-…`` the batch was sent to.
    node_id: str = ""


@dataclass
class Reconciliation:
    """Whether the charges add up to the money that actually moved."""

    charges: list[Charge] = field(default_factory=list)
    #: `(at, balance)` readings taken during the run, when `reconcile_every`
    #: asked for them. Empty otherwise, which is the normal case.
    samples: list[tuple[float, float]] = field(default_factory=list)

    @property
    def charged(self) -> float:
        """What the brokers said they took, summed."""
        return sum(c.cost for c in self.charges)

    @property
    def jobs(self) -> int:
        return sum(c.jobs for c in self.charges)

    #: An authoritative `(before, after)` balance pair, read from ONE broker
    #: either side of the run. Set when `run(reconcile=True)`, which is the
    #: default: it is the only way to check a Task that spanned brokers.
    authoritative: tuple[float, float] | None = None

    def _reported(self) -> list[Charge]:
        return [c for c in self.charges if c.remaining is not None]

    @property
    def views(self) -> dict[str, float]:
        """What each broker said the account holds, by node."""
        return {c.node_name: (c.remaining or 0.0) for c in self._reported()}

    @property
    def balance_spread(self) -> float | None:
        """How far the brokers disagree about the balance. `None` if < 2 said.

        Not a fault on its own. Credits are sharded: one broker owns a user's
        ledger and the rest hold forwarded or cached views, so at any instant
        they legitimately differ. Measured on a live 9-broker mesh, three
        distinct balances at the same moment, 0.685 zkcr apart.

        It is reported because it bounds how much any cross-broker arithmetic
        on these numbers can be trusted — which is to say, not at all.
        """
        values = list(self.views.values())
        return max(values) - min(values) if len(values) > 1 else None

    def _single_source(self) -> str | None:
        """The one node every reported charge came from, if there is one."""
        names = {c.node_name for c in self._reported()}
        return next(iter(names)) if len(names) == 1 else None

    @property
    def balance_before(self) -> float | None:
        """The opening balance, from a source that can actually be compared.

        An authoritative reading if one was taken. Failing that, a *single*
        broker's own series, where `remaining + cost` on its earliest batch is
        the balance before it.

        Never a maximum across brokers. That was the first version of this,
        and it is unsound: the readings come from different ledgers' views of
        the same account, so subtracting one from another measures shard skew
        rather than spend. On a 7-node Task it reported 0.0015 zkcr of drift
        that had not happened.
        """
        if self.authoritative is not None:
            return self.authoritative[0]
        source = self._single_source()
        if source is None:
            return None
        mine = [c for c in self._reported() if c.node_name == source]
        return max((c.remaining or 0.0) + c.cost for c in mine)

    @property
    def balance_after(self) -> float | None:
        if self.authoritative is not None:
            return self.authoritative[1]
        source = self._single_source()
        if source is None:
            return None
        mine = [c for c in self._reported() if c.node_name == source]
        return min(c.remaining or 0.0 for c in mine)

    @property
    def observed(self) -> float | None:
        """How far the balance actually moved, where that is knowable."""
        before, after = self.balance_before, self.balance_after
        if before is None or after is None:
            return None
        return before - after

    @property
    def drift(self) -> float | None:
        """Observed movement minus the charges. Zero is the healthy answer.

        Positive: the account lost more than the brokers admit to taking.
        Negative: the brokers claim to have taken more than actually left.
        `None`: the Task spanned brokers and no authoritative reading was
        taken, so there is no comparable pair — say so rather than subtract
        two different ledgers' opinions.
        """
        observed = self.observed
        return None if observed is None else observed - self.charged

    @property
    def agrees(self) -> bool | None:
        """`None` when nothing comparable was reported, so nothing can be said."""
        drift = self.drift
        return None if drift is None else abs(drift) <= _EPSILON

    def __str__(self) -> str:  # pragma: no cover - display only
        head = (
            f"{self.jobs} job(s) over {len(self.charges)} node batch(es): "
            f"charged {self.charged:.6f} zkcr"
        )
        drift, observed = self.drift, self.observed
        if drift is None or observed is None:
            spread = self.balance_spread
            if spread is not None:
                return (
                    f"{head}; spanned {len(self.views)} brokers whose balances differ by "
                    f"{spread:.6f} zkcr, so they cannot be compared — "
                    f"use run(reconcile=True) for an authoritative reading"
                )
            return f"{head}; balance not reported, nothing to reconcile against"
        if abs(drift) <= _EPSILON:
            return f"{head}, balance moved {observed:.6f} — agrees"
        why = (
            "account lost more than was charged"
            if drift > 0
            else "brokers claim more than actually left the account"
        )
        return f"{head}, balance moved {observed:.6f} — DRIFT {drift:+.6f} zkcr ({why})"


class BalancePoller:
    """Samples the account balance on a timer while a Task runs.

    Only for a Task long enough that waiting for the end is too late. It costs
    one request per tick against a single broker -- not one per node, and never
    one per job -- because the balance is one number wherever you ask.

    A failed read is skipped, not raised: reconciliation is an observation of
    the run, and it must never be the thing that fails it.
    """

    def __init__(self, read: Callable[[], float | None], every: float) -> None:
        self._read = read
        self._every = max(1.0, float(every))
        self._samples: list[tuple[float, float]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> BalancePoller:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        # Wait first: at t=0 the Task has not spent anything yet, so a sample
        # there only records the balance the first reply already gives away.
        while not self._stop.wait(self._every):
            try:
                value = self._read()
            except Exception:
                continue
            if value is not None:
                self._samples.append((time.time(), value))

    @property
    def samples(self) -> list[tuple[float, float]]:
        return list(self._samples)


def dashboard_balance(timeout: float = 20.0) -> float | None:
    """What the DASHBOARD says the account holds. The only authority there is.

    A broker's `/me` is not one. Credits are sharded and each broker caches
    its view, so on a live 9-broker mesh four different balances were reported
    at the same instant, 0.685 zkcr apart, and none of them moved for 45s
    after a Task that had been charged for. The dashboard's number did.

    `None` when there is no api_url, no key, no account id, or the route
    refuses — reconciliation then reports itself unavailable, which is the
    only honest answer when the authority cannot be reached.
    """
    import httpx

    from zakuro.mesh import account_id, api_key, api_url

    base, key, uid = api_url(), api_key(), account_id()
    if not (base and key and uid):
        return None
    from zakuro.security import ensure_secure_for_key

    try:
        # Best-effort: a cleartext URL means the key is withheld (not sent) and
        # reconciliation reports itself unavailable.
        ensure_secure_for_key(base)
        reply = httpx.get(
            f"{base}/api/broker/balance/{uid}",
            headers={"X-Broker-Api-Key": key},
            timeout=timeout,
        )
        if reply.status_code != 200:
            return None
        return float(reply.json().get("credits_balance"))
    except Exception:
        return None


def settled_balance(
    before: float,
    expected: float,
    deadline: float = 60.0,
    poll: float = 6.0,
) -> float | None:
    """The dashboard balance once it has caught up with `expected`, or the
    last reading at `deadline`.

    Brokers commit locally and forward to the dashboard in batches, so reading
    it the instant a Task finishes reads it too early — on the measured mesh
    the charge appeared after about 14s.

    Waiting for the number to *stop moving* was the first version of this and
    it is unreliable: run two Tasks back to back and the second one's opening
    reading absorbs the first one's late debit, which made a Task that had
    lost 0.002 zkcr report `agrees` and blamed the shortfall on the innocent
    one. So the wait is goal-directed instead — it returns as soon as the
    balance has moved by what the brokers said they took, and otherwise waits
    out the deadline and reports what it actually saw.

    That makes a correct Task confirm quickly and an under-debited one pay the
    full deadline, which is the right way round.

    **It cannot be exact on a shared account.** Anything else spending these
    credits during the window lands in this measurement. Reconciliation
    localises a discrepancy to a Task, it does not prove one.
    """
    import time as _time

    started = _time.monotonic()
    last = dashboard_balance()
    while True:
        if last is not None and (before - last) >= expected - _EPSILON:
            return last
        if _time.monotonic() - started >= deadline:
            return last
        _time.sleep(poll)
        current = dashboard_balance()
        if current is not None:
            last = current
