FROM python:3.12.3

WORKDIR /workspace

RUN apt update -y && apt install git -y

RUN pip install zakuro-ai
