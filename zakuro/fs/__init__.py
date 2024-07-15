from .functional import *
from minio import Minio
from zakuro import cfg
client = None


def refresh():
    try:
        assert os.path.exists(os.environ["MINIOFS_CREDS"])
    except:
        os.environ["MINIOFS_CREDS"] = "/etc/default/zfs.creds"
    finally:
        assert os.path.exists(os.environ["MINIOFS_CREDS"])
        access_key, secret_key = (
            open(os.environ["MINIOFS_CREDS"], "r").readlines()[0].rstrip().split(":")
        )

    global client
    client0 = Minio(
        cfg.host,
        secure=False,
        access_key=access_key,
        secret_key=secret_key,
    )
    client = client0




# refresh()
