FROM python:3.15-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY cron_deadman ./cron_deadman
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.15-slim
COPY --from=build /install /usr/local
RUN useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
ENTRYPOINT ["cron-deadman"]
CMD ["serve", "-c", "/config/checks.yaml", "--state", "/data/state.json", "--host", "0.0.0.0", "--port", "8080"]
