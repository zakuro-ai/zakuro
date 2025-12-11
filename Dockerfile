FROM python:3.14.2

WORKDIR /workspace

RUN apt update -y && apt install git -y

RUN pip install zakuro-ai
