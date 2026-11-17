# Bflow —— 容器镜像
#
# 设计要点：
# - 多阶段构建：builder 装依赖，runtime 只带运行所需文件（镜像更小）；
# - 数据全部落在 /data，通过卷挂载持久化（换容器不丢数据）；
# - 非 root 运行；
# - 带健康检查，便于 compose / k8s 判断就绪。

# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 先只拷依赖清单 —— 依赖不变时这一层命中缓存
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    BFLOW_DATA_ROOT=/data \
    BFLOW_HOST=0.0.0.0 \
    BFLOW_PORT=8000

# curl 用于健康检查
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY bflow/ ./bflow/
COPY migrations/ ./migrations/
COPY static/ ./static/
COPY main.py ./

# 非 root 用户
RUN useradd --create-home --uid 10001 bflow \
 && mkdir -p /data \
 && chown -R bflow:bflow /data /app
USER bflow

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "main.py"]
