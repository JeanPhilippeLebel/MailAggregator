FROM python:3.12-slim

# Create non-root user
RUN useradd -r -u 10001 -g nogroup -s /usr/sbin/nologin mailfetcher \
    && mkdir -p /app /data \
    && chown -R mailfetcher:nogroup /app /data

WORKDIR /app

# Copy app
COPY --chown=mailfetcher:nogroup mailfetcher.py /app/mailfetcher.py

# Make executable
RUN chmod +x /app/mailfetcher.py

USER mailfetcher

# Default command: config mounted at /config/config.ini, state in /data
CMD ["/app/mailfetcher.py", "/config/config.ini"]