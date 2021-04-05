FROM python:3.6

RUN pip uninstall zakuro

WORKDIR /workspace

RUN apt update -y && apt install git -y
#RUN pip install git+https://github.com/zakuro-ai/zakuro.git

#RUN pip install zakuro-ai
COPY dist/*.whl /workspace

RUN pip install *.whl
