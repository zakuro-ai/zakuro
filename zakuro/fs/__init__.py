import os

from minio import Minio

# Legacy module-level attribute; not exported by ``zakuro`` (kept as-is to
# preserve existing runtime behavior).
from zakuro import cfg  # type: ignore[attr-defined]

from .functional import *  # noqa: F403

client = None


def refresh() -> None:
    try:
        assert os.path.exists(os.environ["MINIOFS_CREDS"])
    except Exception:
        os.environ["MINIOFS_CREDS"] = "/etc/default/zfs.creds"
    finally:
        assert os.path.exists(os.environ["MINIOFS_CREDS"])
        with open(os.environ["MINIOFS_CREDS"]) as f:
            access_key, secret_key = f.readlines()[0].rstrip().split(":")

    global client
    client0 = Minio(
        cfg.host,
        secure=False,
        access_key=access_key,
        secret_key=secret_key,
    )
    client = client0


# refresh()
