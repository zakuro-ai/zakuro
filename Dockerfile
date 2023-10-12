FROM python:3.11.6

WORKDIR /workspace

RUN apt update -y && apt install git -y

RUN pip install zakuro-ai
