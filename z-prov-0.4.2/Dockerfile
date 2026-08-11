FROM python:3.12-slim-bookworm AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    Z_PROV_CONFIG=/app/config/providers.yaml \
    Z_PROV_HOST=0.0.0.0
RUN groupadd --gid 10001 z-prov && \
    useradd --uid 10001 --gid z-prov --shell /usr/sbin/nologin --no-create-home z-prov
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
WORKDIR /app
COPY --chown=z-prov:z-prov config/providers.example.yaml /app/config/providers.yaml
USER z-prov
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/live', timeout=3)"]
ENTRYPOINT ["z-prov"]
