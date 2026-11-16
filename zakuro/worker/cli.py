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


class SSLKwargs(TypedDict, total=False):
    """uvicorn TLS keyword arguments, typed to match ``uvicorn.run``.

    ``total=False`` because the whole block is absent on the plaintext path.
    :func:`zakuro.worker.tls.uvicorn_ssl_kwargs` produces this shape.
    """

    ssl_certfile: str
    ssl_keyfile: str
    ssl_ca_certs: str
    ssl_cert_reqs: int


# Single source of truth for the "you need the worker extra" guidance so the
# HTTP and QUIC paths print identical, actionable instructions: a wheel uses
# the extra, a source checkout uses `uv sync --extra worker`.
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
            "starting a worker. A non-loopback --host needs caller authentication "
            "(ZAKURO_AUTH_REQUIRED, ZAKURO_WIRE or ZAKURO_CERT_DIR) or "
            "ZAKURO_INSECURE_BIND=1."
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
                "QUIC transport requires the `[worker]` extra (aioquic), which is not "
                f"installed: {exc}.\n{_WORKER_EXTRA_HINT}"
            )
        port = (
            args.port
            if args.port is not None
            else int(os.environ.get("ZAKURO_PORT", str(DEFAULT_PORT)))
        )
        from zakuro.worker.posture import InsecureBindError, StartupConfigError, resolve_listener

        try:
            host = resolve_listener(args.host, port)
        except (InsecureBindError, StartupConfigError) as exc:
            sys.exit(str(exc))
        run_quic_worker(host=host, port=port)
        return

    # Default: HTTP via FastAPI + uvicorn. Probe BOTH deps so a partial install
    # (uvicorn present, fastapi missing) fails with the hint instead of a raw
    # traceback from deep inside `uvicorn.run(...)`.
    try:
        import fastapi  # noqa: F401
        import uvicorn

        from zakuro.worker.posture import InsecureBindError, StartupConfigError, resolve_listener
    except ImportError as exc:
        sys.exit(
            "HTTP transport requires the `[worker]` extra (fastapi, uvicorn, psutil), "
            f"which is not installed: {exc}.\n{_WORKER_EXTRA_HINT}"
        )
    port = args.port if args.port is not None else int(os.environ.get("ZAKURO_PORT", "3960"))
    try:
        host = resolve_listener(args.host, port)
        from zakuro.worker.tls import uvicorn_ssl_kwargs

        ssl_kwargs: SSLKwargs = uvicorn_ssl_kwargs()  # type: ignore[assignment]
    except (InsecureBindError, StartupConfigError) as exc:
        sys.exit(str(exc))
    uvicorn.run(
        "zakuro.worker.server:app",
        host=host,
        port=port,
        reload=False,
        **ssl_kwargs,
    )


if __name__ == "__main__":  # pragma: no cover - script entry point
    main()
