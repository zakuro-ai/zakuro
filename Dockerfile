FROM python:3.12.0a4

WORKDIR /workspace

RUN apt update -y && apt install git -y

RUN pip install zakuro-ai
