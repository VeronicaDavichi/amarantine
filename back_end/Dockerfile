# 后端镜像（spec 049 G1：让 `docker compose up` 一条命令起全套）
#
# 设计取舍：
# - **不打包模型**：embedding / rerank 是独立服务，由 compose 里的 TEI 镜像提供
# - **数据落卷**：SQLite 在 `/app/data`，必须挂出去，否则容器重建记忆就没了
# - **非 root 运行**：容器里跑 uid 10001，卷的权限由 compose 的 `user:` 对齐
# - 用 `uv sync --frozen`：锁文件是唯一依赖事实，镜像里不做二次解析
# - **uv 从 PyPI 装，不用 ghcr 的预编译镜像**：
#   实测 `ghcr.io/astral-sh/uv` 在很多国内镜像站**不被代理**（403），
#   而 PyPI 是通的。少一个 registry 依赖，开源用户也少一个坑。
#
# 基础镜像可覆盖（Docker Hub 在部分网络不可达）：
#
#   docker build \
#     --build-arg BASE=docker.m.daocloud.io/library/python:3.12-slim \
#     -t amarantine-admin:dev back_end

ARG BASE=python:3.12-slim
FROM ${BASE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# curl 用于 HEALTHCHECK；uv 从 PyPI 装（版本与本地一致，锁文件才有意义）
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==0.11.13"

WORKDIR /app

# 先只拷依赖清单，让「改代码」不触发重装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 再拷代码
COPY src ./src
COPY config ./config

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    DATABASE_URL="sqlite:////app/data/memory.db"

# **不要**在这里设 MEMORY_NAMESPACE=""
# —— 空串过不了校验（`须匹配 ^[a-z0-9][a-z0-9_-]{0,63}$`），
# 容器会 FATAL 重启循环（实测踩过）。命名空间由 .env / compose 注入。

# 非 root：uid 固定，便于宿主卷授权
RUN useradd -u 10001 -m amarantine \
    && mkdir -p /app/data \
    && chown -R 10001:10001 /app \
    # 允许**任意 uid** 读/执行 /app：宿主机通常用 `user: <你的uid>` 覆盖运行身份，
    # 好让挂载的数据卷可写（否则 SQLite 报 "attempt to write a readonly database"）。
    && chmod -R a+rX /app
USER 10001

EXPOSE 8765

# 只探进程（不打外部依赖），供 compose 的 depends_on 用
HEALTHCHECK --interval=15s --timeout=3s --start-period=25s --retries=5 \
    CMD curl -fsS "http://127.0.0.1:8765/health?deps=0" || exit 1

CMD ["python", "-m", "memory.entrypoints.admin_http"]
