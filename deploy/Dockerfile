FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 gateway && useradd --uid 10001 --gid gateway --no-create-home gateway
WORKDIR /opt/gateway
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir .
USER 10001:10001
WORKDIR /runtime
ENTRYPOINT ["noreply-gateway"]
CMD ["serve", "--config", "/runtime/gateway.toml"]
