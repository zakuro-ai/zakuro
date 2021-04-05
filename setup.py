from setuptools import setup
import json

setup(
    name="zakuro-ai",
    version="0.0.2",
    short_description="Zakuro, the community cloud based technology powered by AI.",
    long_description="Zakuro, the community cloud based technology powered by AI.",
    url='https://zakuro.ai',
    packages=[
        "zakuro",
        "zakuro.hub",
        "zakuro.nn",
        "zakuro.parsers",
    ],
    include_package_data=True,
    package_data={
      "":[
          "*.yml"
      ]
    },
    license='ZakuroAI',
    author='ZakuroAI',
    python_requires='>=3.6',
    install_requires=[l.rsplit() for l in open("requirements.txt", "r")],
    author_email='info@zakuro.ai',
    description='Zakuro, the community cloud based technology powered by AI.',
    platforms="linux_debian_10_x86_64",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
    ]
)

