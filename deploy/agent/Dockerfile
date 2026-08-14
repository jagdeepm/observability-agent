# Dockerfile for the observability agent — builds from local source, no external registry needed
FROM python:3.12-slim

WORKDIR /app

# Dependencies first, against a stub package, so editing src/ does not re-download
# pydantic-ai on every `task agent-build`.
COPY pyproject.toml ./
RUN mkdir -p src/observability_agent \
    && touch src/observability_agent/__init__.py \
    && pip install --no-cache-dir -e .

COPY src/ src/
RUN pip install --no-cache-dir -e .

# Use the entry point from pyproject.toml
ENTRYPOINT ["observability-agent"]
