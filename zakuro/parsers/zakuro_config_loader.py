import os
from typing import Any

import yaml


class ZakuroConfigLoader(yaml.FullLoader):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def get_single_data(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        ns = super().get_single_data()
        ns = {k: self.__try_expandvars(v) for k, v in ns.items()}
        return ns

    @staticmethod
    def __try_expandvars(v: Any) -> Any:
        try:
            assert isinstance(v, str)
            assert v[0] == "$"
            v = os.path.expandvars(v)
            return v
        except AssertionError:
            return v
