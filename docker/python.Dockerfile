# Python sandbox image for BLUET
# Based on python:3.11-slim with hypothesis and pytest pre-installed

ARG BASE_IMAGE=python:3.11-slim
FROM $BASE_IMAGE

# Install test dependencies
RUN pip install --no-cache-dir hypothesis pytest

# Create non-root user for security
RUN groupadd -r bluet && useradd -r -g bluet bluet

WORKDIR /workdir
USER bluet

ENTRYPOINT ["python"]