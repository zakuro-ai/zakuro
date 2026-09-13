# NOTE: legacy hub helpers referencing constants (``config.ZAKURO_URI``,
# ``config.DEFAULT_ZAKURO_HOME``, ``config.ZAKURO_HUB``) and ``zakuro.load``
# that are not exported by the current package. Kept as-is to preserve runtime
# behavior; the attribute accesses are ignored for mypy rather than silently
# rewired.
import os
import sys

import requests
import torch
from gnutools.fs import parent

import zakuro
from zakuro import config


def restart_from(model: torch.nn.Module, model_path: str) -> None:
    if os.path.exists(model_path):
        load_ckpt(model, model_path)
    elif model_path.startswith(config.ZAKURO_URI):  # type: ignore[attr-defined]
        restart_from_hub(model, model_path)
    else:
        restart_from_hub(model, f"{config.ZAKURO_URI}{model_path}")  # type: ignore[attr-defined]


def restart_from_hub(model: torch.nn.Module, key: str) -> torch.nn.Module:
    model_name, version = key.split(config.ZAKURO_URI)[1].split("/")  # type: ignore[attr-defined]
    output_dir = f"{config.DEFAULT_ZAKURO_HOME}/{model_name}"  # type: ignore[attr-defined]
    output_file = f"{output_dir}/{version}.pth"

    # Download the model
    try:
        if not os.path.exists(output_file):
            assert download_model(model_name, version, output_file)
        load_ckpt(model, output_file)
    except AssertionError:
        sys.stderr.write(f"FileNotFoundException: Could not find {model_name}/{version}")
    return model


def load_ckpt(model: torch.nn.Module, model_path: str) -> None:
    try:
        ckpt = zakuro.load(model_path)  # type: ignore[attr-defined]
        model.load_state_dict(ckpt.state_dict)
    except Exception:
        state_dict = torch.load(model_path)
        model.load_state_dict(state_dict)


def download_model(model_name: str, version: str, output_file: str) -> bool:
    tmp_file = f"/tmp/{version}.pth"
    hub_uri = config.ZAKURO_URI  # type: ignore[attr-defined]
    print(f"ZakuroHub >> Downloading the model from {hub_uri}{model_name}/{version}...")
    res = requests.get(f"{config.ZAKURO_HUB}/{model_name}/{version}")  # type: ignore[attr-defined]
    assert res.status_code == 200
    with open(tmp_file, "wb") as f:
        f.write(res.content)
    output_dir = parent(output_file)
    os.makedirs(output_dir, exist_ok=True)
    os.system(f"mv {tmp_file} {output_dir}")
    return os.path.exists(output_file)
