# 功能：后端内核（FastAPI）镜像。桌面壳不进容器，只跑 API 与 WebSocket。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/data

WORKDIR /app

# 先只拷打包描述，装依赖时能命中层缓存
COPY bandi/pyproject.toml /app/pyproject.toml
COPY bandi/bandi /app/bandi
RUN pip install --no-cache-dir /app

# 运行期数据（会话、记忆、团队、凭据）都落在 $HOME/.bandi
VOLUME ["/data"]
EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "uvicorn", "bandi.api.app:app", "--host", "0.0.0.0", "--port", "8766"]
