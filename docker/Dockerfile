FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --no-cache-dir uv

# Create non-root user
RUN useradd -m -s /bin/bash zakuro
WORKDIR /app

# Copy wheel and install with worker extras
COPY dist/*.whl /tmp/
RUN uv pip install /tmp/*.whl --system \
    && uv pip install fastapi "uvicorn[standard]" psutil --system \
    && rm /tmp/*.whl

# Switch to non-root user
USER zakuro

# Expose worker port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run worker server
CMD ["python", "-m", "zakuro.worker.server"]
