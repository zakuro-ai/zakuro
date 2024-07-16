from argparse import Namespace
import torch

def load(path):
    ckpt = Namespace(**torch.load(path))
    return ckpt
