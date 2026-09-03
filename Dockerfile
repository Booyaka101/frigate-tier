FROM python:3.11-slim

LABEL org.opencontainers.image.title="frigate-tier" \
      org.opencontainers.image.description="Move old Frigate recording segments to slower storage and keep them playable." \
      org.opencontainers.image.source="https://github.com/Booyaka101/frigate-tier" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY frigate_tier ./frigate_tier
RUN pip install --no-cache-dir . && rm -rf /src

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Runs as root so it can write to the same bind mounts Frigate writes to.
ENV FRIGATE_TIER_DB=/config/frigate.db \
    FRIGATE_TIER_HOT=/media/frigate/recordings \
    FRIGATE_TIER_OLDER_THAN=3d \
    FRIGATE_TIER_INTERVAL=3600

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
