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
        port = (
            args.port
            if args.port is not None
            else int(os.environ.get("ZAKURO_PORT", str(DEFAULT_PORT)))
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
    port = args.port if args.port is not None else int(os.environ.get("ZAKURO_PORT", "3960"))

    # mTLS rollout (#115 Phase 2): when ZAKURO_CERT_DIR is set, load
    # server cert + private key + CA bundle and pass to uvicorn so the
    # listener terminates TLS itself. When the env var is unset, the
    # server stays on plaintext HTTP (dev / CI / behind-a-TLS-ingress).
    ssl_kwargs: dict[str, object] = {}
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
        **ssl_kwargs,  # type: ignore[arg-type]  # uvicorn.run params are typed; dict-spread loses per-key types
    )


if __name__ == "__main__":
    main()
