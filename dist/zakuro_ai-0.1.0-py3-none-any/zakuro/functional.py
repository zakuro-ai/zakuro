import sys
# import yaml
# from argparse import Namespace
# import os
from gnutools.fs import parent
from gnutools.fs import load_config as _load_config, parent


def peer(worker):
    def wrapper_worker(f):
        def wrapper(*args, **kwargs):
            from zakuro import ctx
            try:
                v = ctx.get_worker(worker).submit(f, *args, **kwargs)
                return  v.result()
            except ModuleNotFoundError:
                return {worker: 
                    {"error": ModuleNotFoundError, "input":{"f":f, "args": args, "kwargs": kwargs}, "output": None}}
        return wrapper
    return wrapper_worker



try:
    from tqdm.auto import tqdm  # automatically select proper tqdm submodule if available
except ImportError:
    try:
        from tqdm import tqdm
    except ImportError:
        # fake tqdm if it's not installed
        class tqdm(object):  # type: ignore

            def __init__(self, total=None, disable=False,
                         unit=None, unit_scale=None, unit_divisor=None):
                self.total = total
                self.disable = disable
                self.n = 0
                # ignore unit, unit_scale, unit_divisor; they're just for real tqdm

            def update(self, n):
                if self.disable:
                    return

                self.n += n
                if self.total is None:
                    sys.stderr.write("\r{0:.1f} bytes".format(self.n))
                else:
                    sys.stderr.write("\r{0:.1f}%".format(100 * self.n / float(self.total)))
                sys.stderr.flush()

            def close(self):
                self.disable = True

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.disable:
                    return

                sys.stderr.write('\n')



def load_config():
    filename = f"{parent(__file__)}/config.yml"
    cfg = _load_config(filename)
    return cfg



def get_ip():
    import subprocess

    result = subprocess.run("zc wg0ip", shell=True, check=True, capture_output=True)
    host = result.stdout.decode().rsplit()[0]
    return host
