FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MIH_DATA_DIR=/data \
    MIH_BIND_HOST=0.0.0.0 \
    MIH_BIND_PORT=8765

RUN useradd --create-home --uid 10001 mih
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && mkdir -p /data && chown mih:mih /data

USER mih
VOLUME ["/data"]
EXPOSE 8765
# Binding to 0.0.0.0 requires MIH_API_TOKEN; the service refuses to start without it.
CMD ["mih", "serve"]
