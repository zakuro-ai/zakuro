FROM python:3.13.0rc2

WORKDIR /workspace

RUN apt update -y && apt install git -y

RUN pip install zakuro-ai
