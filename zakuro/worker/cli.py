"""Entry point for the ``zakuro-worker`` CLI.

Kept light-weight: fastapi / uvicorn / aioquic are imported lazily only after
we know which transport the user asked for, so ``zakuro-worker --help`` works
even in partially-installed environments.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TypedDict


class _SSLKwargs(TypedDict, total=False):
    """uvicorn TLS keyword arguments, typed to match ``uvicorn.run``.

    ``total=False`` because the whole block is absent on the plaintext path.
    """

    ssl_certfile: str
    ssl_keyfile: str
    ssl_ca_certs: str
    ssl_cert_reqs: int


# Single source of truth for the "you need the worker extra" guidance so the
# HTTP and QUIC paths print identical, actionable instructions. We deliberately
# show BOTH install paths: a pip-installed wheel uses the extra, a source
# checkout (git clone) uses `uv sync --extra worker`.
_WORKER_EXTRA_HINT = (
    "Install it with one of:\n"
    "  pip install 'zakuro-ai[worker]'      # installed from PyPI/wheel\n"
    "  uv sync --extra worker               # from a source checkout (git clone)\n"
    "  uv pip install '.[worker]'           # source checkout without uv project sync"
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Zakuro Worker Server",
        epilog=(
            "The worker requires the `[worker]` extra (fastapi, uvicorn, psutil, "
            "aioquic). Core `pip install zakuro-ai` ships the client only; run "
            "`pip install 'zakuro-ai[worker]'` or `uv sync --extra worker` before "
            "starting a worker."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("ZAKURO_HOST", "0.0.0.0"),
        help="Bind address (default: 0.0.0.0 or $ZAKURO_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port. Default 3960 for --transport http, 4433 for quic.",
    )
    parser.add_argument(
        "--worker-name",
        default=None,
        help="Override ZAKURO_WORKER_NAME.",
    )
    parser.add_argument(
        "--transport",
        choices=("http", "quic"),
        default=os.environ.get("ZAKURO_TRANSPORT", "http"),
        help="Transport protocol. 'quic' drops FastAPI/uvicorn in favour of aioquic.",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    if args.worker_name:
        os.environ["ZAKURO_WORKER_NAME"] = args.worker_name

    if args.transport == "quic":
        try:
            from zakuro.worker.quic_server import DEFAULT_PORT, run_quic_worker
        except ImportError as exc:
            sys.exit(
                "`--transport quic` requires the `[worker]` extra (aioquic), which "
                f"is not installed: {exc}.\n{_WORKER_EXTRA_HINT}"
            )
        port = (
            args.port
            if args.port is not None
            else int(os.environ.get("ZAKURO_PORT", str(DEFAULT_PORT)))
        )
        run_quic_worker(host=args.host, port=port)
        return

    # Default: HTTP via FastAPI + uvicorn. Probe BOTH deps here so a partial
    # install (e.g. uvicorn present but fastapi missing, the common
    # `pip install zakuro-ai` then `zakuro-worker` -> `ImportError: fastapi`
    # case) fails with the actionable hint instead of a raw traceback that
    # surfaces deep inside `uvicorn.run(...)` when it imports the server module.
    try:
        import fastapi  # noqa: F401
        import uvicorn
    except ImportError as exc:
        sys.exit(
            "Running the Zakuro worker (HTTP transport) requires the `[worker]` "
            f"extra (fastapi, uvicorn, psutil), which is not installed: {exc}.\n"
            f"{_WORKER_EXTRA_HINT}"
        )
    port = args.port if args.port is not None else int(os.environ.get("ZAKURO_PORT", "3960"))

    # mTLS rollout (#115 Phase 2): when ZAKURO_CERT_DIR is set, load
    # server cert + private key + CA bundle and pass to uvicorn so the
    # listener terminates TLS itself. When the env var is unset, the
    # server stays on plaintext HTTP (dev / CI / behind-a-TLS-ingress).
    ssl_kwargs: _SSLKwargs = {}
    if os.environ.get("ZAKURO_CERT_DIR", "").strip():
        # Defer the import: zakuro.transport pulls in cryptography which
        # we don't want on the `--help` path.
        from zakuro.transport import load_server_tls

        material = load_server_tls()
        ssl_kwargs = {
            "ssl_certfile": str(material.cert_path),
            "ssl_keyfile": str(material.key_path),
            "ssl_ca_certs": str(material.ca_bundle_path),
            # uvicorn 0.23+ accepts ssl.CERT_REQUIRED as int (2); avoid
            # importing ssl at module top to keep --help cheap.
            "ssl_cert_reqs": 2,
        }

    uvicorn.run(
        "zakuro.worker.server:app",
        host=args.host,
        port=port,
        reload=False,
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
