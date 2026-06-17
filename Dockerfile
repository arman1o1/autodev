FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    make \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv globally
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create a non-root user and setup workspace
RUN useradd -m -u 1000 sandbox_user
WORKDIR /workspace
RUN chown sandbox_user:sandbox_user /workspace

USER sandbox_user

# Default command to keep container alive if run interactively, otherwise it will run commands
CMD ["python3"]
