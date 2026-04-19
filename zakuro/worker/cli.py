"""Entry point for the ``zakuro-worker`` CLI.

Kept light-weight: fastapi / uvicorn / aioquic are imported lazily only after
we know which transport the user asked for, so ``zakuro-worker --help`` works
even in partially-installed environments.
"""

from __future__ import annotations

import argparse
import os
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Zakuro Worker Server")
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
                "QUIC transport requires the `[worker]` extra (aioquic): "
                f"{exc}. Install with `pip install 'zakuro-ai[worker]'`."
            )
        port = args.port if args.port is not None else int(
            os.environ.get("ZAKURO_PORT", str(DEFAULT_PORT))
        )
        run_quic_worker(host=args.host, port=port)
        return

    # Default: HTTP via FastAPI + uvicorn.
    try:
        import uvicorn
    except ImportError as exc:
        sys.exit(
            "HTTP transport requires the `[worker]` extra (fastapi, uvicorn): "
            f"{exc}. Install with `pip install 'zakuro-ai[worker]'`."
        )
    port = args.port if args.port is not None else int(
        os.environ.get("ZAKURO_PORT", "3960")
    )
    uvicorn.run(
        "zakuro.worker.server:app",
        host=args.host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
