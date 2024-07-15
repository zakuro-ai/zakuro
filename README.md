![zakuro Logo](imgs/zakuro-banner.png)

--------------------------------------------------------------------------------

Zakuro is a simple but powerful tool to enable federated learning  running on a community cloud based platform. It provides two features:
- A model hub to enable federated learning applications.
- An integration with PyTorch. 


You can reuse your favorite Python framework such as Pytorch, Tensorflow or PaddlePaddle.


## Zakuro modules

At a granular level, Sakura is a library that consists of the following components:

| Component | Description |
| ---- | --- |
| **zakuro** | Zakuro. |
| **zakuro.fs** | Manage filesystem |
| **zakuro.hub** | Hub to store and share pretrained models |
| **zakuro.nn** | Load models. |
| **zakuro.parsers** | Parse config files |

### Docker
To build the image with zakuro installed.
```
docker compose up vanilla -d
```
