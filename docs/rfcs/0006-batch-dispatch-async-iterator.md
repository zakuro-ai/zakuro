# RFC 0006 — Batch dispatch: async iterator API

- **Status:** Accepted (2026-05)
- **Closes:** [#137](https://github.com/zakuro-ai/zakuro/issues/137)
- **Depends on:** existing `AdaptiveCompute` allocator, RFC 0001 (wire format for batched envelopes)

## Context

Today a user wanting to dispatch N calls writes:

```python
results = [my_fn.to(adaptive)(x) for x in inputs]
```

Synchronous, sequential, sub-optimal — the allocator can route each call but can't overlap them. Customers asking "run inference on 100k rows" hit a single-call-at-a-time bottleneck.

[#137](https://github.com/zakuro-ai/zakuro/issues/137) proposed `adaptive.map(fn, iterable)` to give the allocator the whole batch and let it pipeline. The user picked the **async iterator** API shape over a synchronous bulk-blocking call.

## Decision

**`adaptive.map(fn, iterable)` returns an `AsyncIterator[Result]` that yields results as workers complete them, with bounded in-flight concurrency.**

Two surface forms:

```python
# Default: yields in completion order
async for result in adaptive.map(my_fn, inputs):
    handle(result)

# Order-preserving: yields in input order, blocks the slowest pipe
async for index, result in adaptive.map.enumerate(my_fn, inputs):
    assert results_by_index[index] is None
    results_by_index[index] = result
```

The synchronous variant `adaptive.map_sync(fn, inputs) -> list[Result]` is a thin `asyncio.run(_collect(adaptive.map(...)))` wrapper for the calling-from-sync-code case.

## Implementation

### Core loop

```python
async def map(
    self,
    fn: Fn,
    inputs: Iterable[Any] | AsyncIterable[Any],
    *,
    max_in_flight: int = 32,
    on_error: Literal["raise", "yield", "skip"] = "raise",
) -> AsyncIterator[Any]:
    """
    Dispatch `fn` over `inputs` across the adaptive worker pool.

    Yields results as workers complete them. Backpressure is bounded by
    `max_in_flight` — the input iterator is pulled lazily so an unbounded
    generator works.

    on_error:
        "raise" (default) — first failure raises after pending tasks drain
        "yield"           — failed items are yielded as `Failure(exc, input)`
                            values; the iterator keeps going
        "skip"            — failed items are silently dropped
    """
    pending: set[asyncio.Task] = set()
    input_iter = aiter(_as_async(inputs))
    exhausted = False

    while pending or not exhausted:
        # Refill the pipe
        while not exhausted and len(pending) < max_in_flight:
            try:
                x = await anext(input_iter)
            except StopAsyncIteration:
                exhausted = True
                break
            pending.add(asyncio.create_task(self._dispatch_one(fn, x)))

        if not pending:
            return

        done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            yield await _handle_one(task, on_error)
```

### `enumerate` variant

```python
async def enumerate(
    self,
    fn: Fn,
    inputs: Iterable[Any] | AsyncIterable[Any],
    *,
    max_in_flight: int = 32,
    on_error: Literal["raise", "yield", "skip"] = "raise",
) -> AsyncIterator[tuple[int, Any]]:
    """Same as `map`, but tags each result with its input index."""
    async def _tagged():
        async for i, x in _aenumerate(_as_async(inputs)):
            yield (i, x)
    async for index, x in _tagged():
        result = await self._dispatch_one(fn, x)
        yield (index, result)
```

Yields in completion order with the index attached. Callers wanting strict input-order assembly do it on their side (`sort by index`) — cheaper than blocking the slow lane to preserve order.

### Backpressure

The `max_in_flight` knob caps concurrent dispatches. Default 32; tunable per call. The input iterator is pulled lazily, so an unbounded generator (e.g. tailing a Kafka topic) works without buffering the full source.

When a worker is saturated, `AdaptiveCompute.pick_worker()` already returns the *next* best target by EMA-tracked latency, so backpressure surfaces as redistribution before it becomes a queue.

### Cancellation

`asyncio.CancelledError` from the consumer (e.g. caller breaks out of the `async for`) cancels every pending dispatch task with `Task.cancel()`. The worker receives a `cancel` RPC over the QUIC stream (best-effort — already-running jobs may complete but their result is dropped).

```python
try:
    async for result in adaptive.map(my_fn, inputs):
        if found_what_we_wanted(result):
            break
except* Exception as eg:
    # both group + cancellation propagate
    ...
```

### Sync facade

```python
def map_sync(
    self,
    fn: Fn,
    inputs: Iterable[Any],
    *,
    max_in_flight: int = 32,
    on_error: Literal["raise", "yield", "skip"] = "raise",
) -> list[Any]:
    """Calling-from-sync-code convenience. Materialises the full list."""
    async def _collect():
        return [r async for r in self.map(
            fn, inputs, max_in_flight=max_in_flight, on_error=on_error
        )]
    return asyncio.run(_collect())
```

## Sample usage

```python
import zakuro as zk

@zk.fn
def embed(text: str) -> list[float]:
    ...

adaptive = zk.AdaptiveCompute(workers=[w.compute() for w in pool])

# Streamed, completion order, fail-fast
async for vec in adaptive.map(embed, open("corpus.txt")):
    write_to_index(vec)

# Order-preserving, soft-fail (failed items become `Failure`)
async for i, result in adaptive.map.enumerate(
    embed, sentences, on_error="yield"
):
    if isinstance(result, Failure):
        bad_indices.append(i)
        continue
    embeddings[i] = result
```

## Rejected alternatives

| Option | Why rejected |
|---|---|
| Synchronous bulk API `adaptive.map(fn, iterable) -> list[Result]` | Blocks until the whole batch completes — no streaming, no progress visibility, no early-break. The async iterator subsumes the sync case via `map_sync`. |
| Callback-based (`adaptive.map(fn, iter, on_result=cb)`) | Mixes control flow; harder to compose with `asyncio.gather` / `as_completed`. Async iterators are the idiomatic Python form. |
| Return a `concurrent.futures.Future`-list | Forces the caller to deal with futures concurrency; doesn't compose with `async for`; reinvents asyncio. |
| `ray.put` / `ray.get` shape | Ties us to Ray's object-store semantics. We don't have an object store at the broker layer. |

## Implementation plan

1. **Land the API in `zakuro/adaptive.py`** alongside the existing single-dispatch `pick_worker`. No worker-side changes (each input is still a single `/execute` call).
2. **Add `tests/test_adaptive_map.py`** with:
   - happy path: 100 items × 3 workers, completion-order yields
   - `enumerate` preserves input indexing
   - `on_error="yield"` produces `Failure` values for raising inputs
   - cancellation propagates to in-flight workers
   - `max_in_flight` is respected (peak concurrency observed)
3. **Document on the public surface** ([`docs/STABILITY.md`](https://github.com/zakuro-ai/zakuro/blob/master/docs/STABILITY.md) — adds `AdaptiveCompute.map` / `.enumerate` / `.map_sync` to the "Stable" table from v0.5).
4. **Update the README** quick-start to include a streaming example.
5. **Notebook**: add `notebooks/batch_dispatch.ipynb` showing the three variants on a real workload (e.g. 1000-row embedding job).

## Open questions for implementation time

- **Default `max_in_flight`.** 32 is a guess. Should probably default to `2 × n_workers` after warmup; pick once the bench harness has data.
- **Result-side ordering for `enumerate`.** Whether to also offer `enumerate(..., strict=True)` that buffers until the next contiguous index can be emitted. Adds memory pressure; defer until a customer asks.
- **Streaming aggregation.** A `reduce(fn, batch)` variant that runs the reducer on the broker rather than re-aggregating client-side. Out of scope for v1.
- **Per-input timeout.** The single-dispatch path already supports a timeout via the plan; for batches we just propagate that timeout to each. Whether to add a *batch-wide* deadline (e.g. "stop pulling new inputs after 30 s") needs a customer ask.
