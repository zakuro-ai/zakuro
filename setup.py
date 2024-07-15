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

