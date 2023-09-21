FROM python:3.8.18

WORKDIR /workspace

RUN apt update -y && apt install git -y

RUN pip install zakuro-ai
