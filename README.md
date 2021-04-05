![zakuro Logo](imgs/zakuro-banner.png)

--------------------------------------------------------------------------------

Zakuro is a simple but powerful tool to reduce training time by running the train/test asynchronously. It provides two features:
- A model hub to enable federated learning applications.
- An integration with PyTorch. 


You can reuse your favorite Python framework such as Pytorch, Tensorflow or PaddlePaddle.


## Zakuro modules

At a granular level, Sakura is a library that consists of the following components:

| Component | Description |
| ---- | --- |
| **zakuro** | Contains the sakura modules. |
| **zakuro.hub** | Contains the code related to ml processing |
| **zakuro.nn** | Decorators used to synchronize the train/test.|
| **zakuro.parsers** | Decorators used to synchronize the train/test.|

## Installation
### Local
```
python setup.py install
```

### Pypi
```
pip install zakuro-ai  --no-cache-dir
```

### Docker
To build the image and launch a container to run a test demo on MNIST.
```
docker pull zakuroai/zakuro
sh docker.sh
```
