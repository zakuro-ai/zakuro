FROM zakuroai/compute

COPY dist/*.whl /workspace/

RUN uv pip install /workspace/*.whl && rm /workspace/*.whl

WORKDIR /workspace
