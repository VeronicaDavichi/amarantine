# 后端镜像（spec 049 G1：让 `docker compose up` 一条命令起全套）
#
# 设计取舍：
# - **不打包模型**：embedding / rerank 是独立服务，由 compose 里的 TEI 镜像提供
# - **数据落卷**：SQLite 在 `/app/data`，必须挂出去，否则容器重建记忆就没了
# - **非 root 运行**：容器里跑 uid 10001，卷的权限由 compose 的 `user:` 对齐
# - 用 `uv sync --frozen`：锁文件是唯一依赖事实，镜像里不做二次解析

# 基础镜像可覆盖：Docker Hub 在部分网络不可达，改用镜像站即可
#   docker build --build-arg BASE=<mirror>/library/python:3.12-slim .
ARG BASE=python:3.12-slim
FROM ${BASE} AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# curl 用于 healthcheck；build-essential 只在装依赖阶段需要
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# uv：官方独立二进制，不经过 pip
# uv 同样可覆盖（ghcr.io 也可能不通）：
#   --build-arg UV_IMAGE=<mirror>/astral-sh/uv:latest
ARG UV_IMAGE=ghcr.io/astral-sh/uv:latest
COPY --from=${UV_IMAGE} /uv /usr/local/bin/uv

WORKDIR /app

# 先只拷依赖清单，让「改代码」不触发重装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 再拷代码
COPY src ./src
COPY config ./config

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    DATABASE_URL="sqlite:////app/data/memory.db" \
    MEMORY_NAMESPACE=""

# 非 root：uid 固定，便于宿主卷授权
RUN useradd -u 10001 -m amarantine \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app
USER 10001

EXPOSE 8765

# 只探进程（不打外部依赖），供 compose 的 depends_on 用
HEALTHCHECK --interval=15s --timeout=3s --start-period=25s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:8765/health?deps=0" || exit 1

CMD ["python", "-m", "memory.entrypoints.admin_http"]
