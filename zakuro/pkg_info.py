# if cfg.host == "auto":
#     try:
#         cfg.host = f"{get_ip()}:9000"
#     except:
#         cfg.host = "127.0.0.1:9000"
from gnutools.fs import parent        
__version__ = open(f"{parent(__file__)}/version", "r").readline()
__build__ = open(f"{parent(__file__)}/build", "r").readline()
