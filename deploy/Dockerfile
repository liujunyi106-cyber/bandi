# 功能：后端内核多阶段镜像。builder 装依赖产出 venv，production 只带运行期产物。
# 桌面壳（Electron）不进镜像：容器只提供 API / WebSocket。

# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1：builder —— 装依赖到独立 venv
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY bandi/pyproject.toml ./pyproject.toml
COPY bandi/bandi ./bandi

RUN pip install --upgrade pip wheel setuptools \
    && pip install .

# ---------------------------------------------------------------------------
# Stage 2：production —— 非 root 运行，只带 venv 与业务包
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS production

LABEL description="Bandi 后端内核（FastAPI）" \
      org.opencontainers.image.source="https://github.com/liujunyi106-cyber/bandi"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PATH="/opt/venv/bin:$PATH" \
    BANDI_HOME=/data \
    BANDI_PORT=8766

RUN groupadd --gid 1000 bandi \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin bandi \
    && mkdir -p /data \
    && chown -R bandi:bandi /data

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER bandi

VOLUME ["/data"]
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "bandi.api.app:app", "--host", "0.0.0.0", "--port", "8766"]
