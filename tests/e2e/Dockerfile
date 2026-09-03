# The e2e harness: the shipped image plus the tools that check its work.
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY frigate_tier ./frigate_tier
RUN pip install --no-cache-dir .

WORKDIR /work
