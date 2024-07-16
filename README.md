![zakuro Logo](imgs/zakuro-banner.png)

--------------------------------------------------------------------------------

<p align="center">
        <img alt="Build" src="https://github.com/zakuro-ai/zakuro/actions/workflows/trigger.yml/badge.svg?branch=master">
        <img alt="GitHub" src="https://img.shields.io/github/license/zakuro-ai/zakuro.svg?color=blue">
        <img alt="GitHub release" src="https://img.shields.io/github/release/zakuro-ai/zakuro.svg">
</p>


<p align="center">
  <a href="#modules">Modules</a> •
  <a href="#code-structure">Code structure</a> •
  <a href="#installing-the-application">Installing the application</a> •
  <a href="#makefile-commands">Makefile commands</a> •
  <a href="#environments">Environments</a> •
  <a href="#environments">Entrypoints</a> •
  <a href="#ressources">Ressources</a>
</p>

Zakuro is a simple but powerful tool to enable federated learning  running on a community cloud based platform. It provides two features:
- A model hub to enable federated learning applications.
- An integration with PyTorch. 



# Modules

At a granular level, zakuro is a library that consists of the following components:

| Component | Description |
| ---- | --- |
| **zakuro** | Zakuro. |
| **zakuro.fs** | Manage filesystem |
| **zakuro.hub** | Hub to store and share pretrained models |
| **zakuro.nn** | Load models. |
| **zakuro.parsers** | Parse config files |

# Code structure
```python
from setuptools import setup
import json
import os
pkg_name="zakuro"
setup(
    name="zakuro-ai",
    version=open(f"{pkg_name}/version", "r").readline(),
    short_description="Zakuro, the community cloud based technology powered by AI.",
    long_description="Zakuro, the community cloud based technology powered by AI.",
    url='https://zakuro.ai',
    packages=[
        "zakuro",
        "zakuro.fs",
        "zakuro.hub",
        "zakuro.nn",
        "zakuro.parsers",
    ],
    include_package_data=True,
    package_data={pkg_name: ["config.yaml", "version", "build"]},
    license="BSD-3",
    keywords="fog computing, machine learning",
    author='ZakuroAI Team',
    python_requires='>=3.8',
    install_requires=[l.rsplit() for l in open("requirements.txt", "r")],
    author_email='info@zakuro.ai',
    description='Zakuro, the community cloud based technology powered by AI.',
    platforms="linux_debian_10_x86_64",
        classifiers=[
            "Intended Audience :: Developers",
            "Intended Audience :: Education",
            "Intended Audience :: Science/Research",
            "License :: OSI Approved :: BSD License",
            "Topic :: Scientific/Engineering",
            "Topic :: Scientific/Engineering :: Mathematics",
            "Topic :: Scientific/Engineering :: Artificial Intelligence",
            "Topic :: Software Development",
            "Topic :: Software Development :: Libraries",
            "Topic :: Software Development :: Libraries :: Python Modules",
            "Programming Language :: Python :: 3",
        ]
)
```

# Installing the application
To clone and run this application, you'll need the following installed on your computer:
- [Git](https://git-scm.com)
- Docker Desktop
   - [Install Docker Desktop on Mac](https://docs.docker.com/docker-for-mac/install/)
   - [Install Docker Desktop on Windows](https://docs.docker.com/desktop/install/windows-install/)
   - [Install Docker Desktop on Linux](https://docs.docker.com/desktop/install/linux-install/)
- [Python](https://www.python.org/downloads/)


# Get an access to Zakuro

To get started with ZakuroAI, you need to obtain an [access key](https://docs.zakuro.ai/) and set it as a global environment variable. Follow these steps to get access:

1. **Request Access**:
   To use ZakuroAI, you need to obtain an access key (`ZAKURO_AUTH`). Contact us at [early_access@zakuro.ai](mailto:early_access@zakuro.ai) to request your access key.

2. **Set the Access Key**:
   Once you have received your access key, set it as a global environment variable. This ensures that all ZakuroAI commands can authenticate successfully.

   On Unix-based systems (Linux, macOS):
   ```sh
   export ZAKURO_AUTH="your_access_key_here"
    ```

# Install the package:

```bash
# Clone this repository and install the code
git clone https://github.com/zakuro-ai/zakuro

# Go into the repository
cd zakuro
```

# Getting started
Build and launch dev container:
```
make all
```
