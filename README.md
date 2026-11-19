<p align="center">
  <img alt="Zakuro" src="imgs/zakuro-banner.png">
</p>

<h3 align="center">Run Python functions on a pool of workers that routes around slow and dead machines.</h3>

<p align="center">
  <a href="https://pypi.org/project/zakuro-ai/"><img alt="PyPI" src="https://img.shields.io/pypi/v/zakuro-ai?color=blue"></a>
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="License" src="https://img.shields.io/github/license/zakuro-ai/zakuro.svg?color=blue">
</p>

<p align="center">
  <a href="https://docs.zakuro-ai.com"><b>Documentation</b></a> •
  <a href="https://docs.zakuro-ai.com/start/quickstart">Quickstart</a> •
  <a href="https://docs.zakuro-ai.com/reference/python-sdk">API reference</a> •
  <a href="CHANGELOG.md">Changelog</a>
</p>

---

Decorate a function, hand Zakuro a pool of workers, and each call goes to the worker that should finish it soonest. Zakuro learns from every call, resends a call when its worker dies, and stops sending work to machines that are slow or gone. Your code doesn't change.

## Try it in one command

```bash
git clone https://github.com/zakuro-ai/zakuro && cd zakuro
uv run demo.py
```

[`uv`](https://docs.astral.sh/uv/getting-started/installation/) installs the published `zakuro-ai` package into a throwaway environment and starts three workers on your machine. They count the primes below 100,000,000 in 200 chunks, and a third of the way in one worker is killed with no warning. On an Apple-silicon Mac it takes 6 s from `git clone` to this, install included (17 s on a 2017 Intel laptop):

```
   50/200 [######..................]   calls sent: w0  21   w1  19   w2  25
  !! killing worker-1 (pid 41675) mid-job

   75/200 [#########...............]   calls sent: w0  31   w1  29   w2  35
  100/200 [############............]   calls sent: w0  46   w1  37 (down)   w2  49
  ...
  200/200 [########################]   calls sent: w0 103   w1  37 (down)   w2 100

  primes below 100,000,000: 5,761,455  (known answer 5,761,455) -- correct
  200/200 calls succeeded, 0 lost; 10 call(s) hit the dead worker and were resent elsewhere
  worker-1 marked suspended by the health probe; finished in 3.1 s
```

No GPU, no Docker, no account.

## Use it

```bash
pip install 'zakuro-ai[worker]'
```

```python
import zakuro as zk

@zk.fn
def square(x: int) -> int:
    return x * x

workers = [zk.Worker.spawn() for _ in range(3)]          # three workers on this machine
pool = zk.AdaptiveCompute(workers=[w.compute(verify=False) for w in workers],
                          max_dispatch_retries=2)        # a failed call is resent to another worker
print(square.to(pool)(7))                                # → 49, on whichever worker is fastest
```

To run across machines, install `zc`, the Zakuro broker and CLI, from crates.io (the crate is `zc2`; it installs the `zc` command). It needs a Rust toolchain ([rustup.rs](https://rustup.rs)) and a C compiler/linker (`build-essential` on Debian/Ubuntu, Xcode Command Line Tools on macOS):

```bash
cargo install zc2 --locked
zc login                               # sign in in the browser
sudo -E "$(command -v zc)" connect     # join the mesh
```

Run `zc login` as yourself, not with `sudo`: it also gives this machine its own mesh identity. Joining the mesh brings up a WireGuard tunnel, so it needs `wireguard-tools` (apt or Homebrew) and root. `-E` keeps your sign-in, and the full path is there because `sudo` does not look in `~/.cargo/bin`.

Then address a machine on the mesh by its node, not its IP. `zk.nodes()` lists them, and the address survives the machine moving:

```python
square.to(zk.Compute(uri="zc://node-dd715b7f0a1c2e34", verify=False))(7)
```

A node with no free worker of its own forwards the call to another machine, which is paid, so give it a budget in yen: `square.to(zk.Compute(uri="zc://node-dd715b7f0a1c2e34", verify=False, processor_options={"budget": 10}))(7)`. Without one the broker answers `BUDGET_REQUIRED`.

Everything else is in the **[documentation](https://docs.zakuro-ai.com)**: [install options](https://docs.zakuro-ai.com/install/zakuro), [concepts](https://docs.zakuro-ai.com/zakuro/concepts), [choosing a transport](https://docs.zakuro-ai.com/guides/choosing-a-transport), [the zc broker](https://docs.zakuro-ai.com/zc/broker) and the [Python API reference](https://docs.zakuro-ai.com/reference/python-sdk). For ML training on top of Zakuro, see [Sakura](https://github.com/zakuro-ai/sakura).

---

<p align="center">
  If Zakuro is useful to you, a ⭐ helps other people find it, and it's the easiest way to follow new releases.<br>
  <a href="CONTRIBUTING.md">Contributing</a> • <a href="SECURITY.md">Security</a> • <a href="LICENSE">BSD-3-Clause</a>
</p>
