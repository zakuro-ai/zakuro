# Worker image (slim variant). The distroless variant lives in
# Dockerfile.distroless and shares the same builder stage to keep image
# behaviour identical between variants.
#
# The base image is digest-pinned; Dependabot tracks both the tag and the
# digest under the `docker` ecosystem (see .github/dependabot.yml).
FROM python:3.12-slim@sha256:401f6e1a67dad31a1bd78e9ad22d0ee0a3b52154e6bd30e90be696bb6a3d7461

# Install uv (no apt packages — curl is dropped, healthcheck is in-process).
RUN pip install --no-cache-dir uv

# Non-root user
RUN useradd -m -s /bin/bash zakuro
WORKDIR /app

# Install zakuro + the worker extras into the system site-packages.
COPY dist/*.whl /tmp/
RUN uv pip install /tmp/*.whl --system \
    && uv pip install fastapi "uvicorn[standard]" psutil --system \
    && rm /tmp/*.whl

USER zakuro

EXPOSE 3960

# In-process healthcheck — uses urllib from the stdlib instead of curl so
# we don't carry an apt-installed binary that broadens the CVE surface.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request as r, sys; sys.exit(0 if r.urlopen('http://localhost:3960/health', timeout=3).status == 200 else 1)" \
        || exit 1

CMD ["python", "-m", "zakuro.worker.server"]
