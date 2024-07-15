# -*- coding: utf-8 -*-

from zakuro.context import Context
from zakuro.pkg_info import __version__
# from zakuro.parsers import ZakuroConfigLoader
ctx = Context("zakuro://10.13.13.2")

# current_dir = os.path.dirname(__file__)
# config = Namespace(**yaml.load(open(f"{current_dir}/config.yml"), Loader=ZakuroConfigLoader))
from .functional import load_config, peer
cfg = load_config()
