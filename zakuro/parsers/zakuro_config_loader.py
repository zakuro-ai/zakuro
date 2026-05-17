import os

import yaml


class ZakuroConfigLoader(yaml.FullLoader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def get_single_data(self, *args, **kwargs):
        ns = super().get_single_data()
        ns = {k: self.__try_expandvars(v) for k, v in ns.items()}
        return ns

    @staticmethod
    def __try_expandvars(v):
        try:
            assert isinstance(v, str)
            assert v[0] == "$"
            v = os.path.expandvars(v)
            return v
        except AssertionError:
            return v
