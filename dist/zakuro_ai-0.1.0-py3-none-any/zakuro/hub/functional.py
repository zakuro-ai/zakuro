import os
import zakuro
from zakuro import config
from gnutools.fs import parent
import requests
import sys
import torch


def restart_from(model, model_path):
    if os.path.exists(model_path):
        load_ckpt(model, model_path)
    elif model_path.startswith(config.ZAKURO_URI):
        restart_from_hub(model, model_path)
    else:
        restart_from_hub(model, f"{config.ZAKURO_URI}{model_path}")



def restart_from_hub(model, key):
    model_name, version = key.split(config.ZAKURO_URI)[1].split("/")
    output_dir = f"{config.DEFAULT_ZAKURO_HOME}/{model_name}"
    output_file = f"{output_dir}/{version}.pth"

    # Download the model
    try:
        if not os.path.exists(output_file):
            assert download_model(model_name, version, output_file)
        load_ckpt(model, output_file)
    except AssertionError:
        sys.stderr.write(f"FileNotFoundException: Could not find {model_name}/{version}")
    return model


def load_ckpt(model, model_path):
    try:
        ckpt = zakuro.load(model_path)
        model.load_state_dict(ckpt.state_dict)
    except:
        state_dict = torch.load(model_path)
        model.load_state_dict(state_dict)


def download_model(model_name, version, output_file):
    tmp_file = f"/tmp/{version}.pth"
    print(f"ZakuroHub >> Downloading the model from {config.ZAKURO_URI}{model_name}/{version}...")
    res = requests.get(f"{config.ZAKURO_HUB}/{model_name}/{version}")
    assert res.status_code==200
    with open(tmp_file, "wb") as f:
        f.write(res.content)
    output_dir = parent(output_file)
    os.makedirs(output_dir, exist_ok=True)
    os.system(f"mv {tmp_file} {output_dir}")
    return os.path.exists(output_file)