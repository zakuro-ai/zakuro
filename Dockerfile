FROM python:3.13.5

WORKDIR /workspace

RUN apt update -y && apt install git -y

RUN pip install zakuro-ai
