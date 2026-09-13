from argparse import Namespace

import torch


def load(path: str) -> Namespace:
    ckpt = Namespace(**torch.load(path))
    return ckpt
