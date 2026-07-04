# AutoMind — production container image
# Build:  docker build -t automind .
# Run:    docker run -p 8765:8765 -v automind_data:/data automind

FROM python:3.12-slim AS base

# 安全基线：非 root 运行 + 精简层
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先复制依赖清单以利用构建缓存
COPY pyproject.toml README.md ./
COPY automind ./automind

RUN pip install --no-cache-dir ".[web]" \
    && useradd --create-home --shell /bin/bash automind \
    && mkdir -p /data \
    && chown -R automind:automind /app /data

USER automind

# 工作目录挂载点：任务在 /data 内读写（配置/记忆/检查点均落在此）
WORKDIR /data
VOLUME ["/data"]

EXPOSE 8765

# 健康检查：/api/health 无需鉴权
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "automind.server", "--host", "0.0.0.0", "--port", "8765"]
