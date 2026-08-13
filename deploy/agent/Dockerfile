# Dockerfile for the observability agent — builds from local source, no external registry needed
FROM python:3.12-slim

WORKDIR /app

# Install only runtime deps
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]" 2>/dev/null || true

# Copy source
COPY src/ src/

# Install the package
RUN pip install --no-cache-dir -e .

# Use the entry point from pyproject.toml
ENTRYPOINT ["observability-agent"]