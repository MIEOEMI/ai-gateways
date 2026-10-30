# workbuddy2api 网关容器。
#
# 依赖用 docker/wheels 里的离线 wheel 安装：构建环境常常连不上 PyPI（实测
# `pip install fastapi` 在 python:3.11-slim 里会卡住不动），而本项目只需要
# fastapi / uvicorn / httpx 三个包，把它们固化下来比依赖网络可靠得多。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime && echo "${TZ}" > /etc/timezone

WORKDIR /app

# 拷贝运行所需的全部网关模块 + 离线依赖。
# 用通配符而非逐个列举：三个协议层（anthropic_compat / responses_compat /
# deepseek_thinking 等）是并列的可选模块，逐个列会在上游新增模块时静默漏拷，
# 表现为运行到该端点才报 "module not available"。
COPY *.py turing_helper.cjs /app/
COPY wheels /wheels

RUN pip install --no-cache-dir --no-index --find-links=/wheels fastapi uvicorn httpx \
    && rm -rf /wheels

# 凭证真源：accounts.json（与桌面端同一结构），挂卷持久化。
# LOCALAPPDATA 指向挂载目录，使 _accounts_file() 命中
# $LOCALAPPDATA/workbuddy2api/accounts.json
RUN mkdir -p /data/workbuddy2api
ENV LOCALAPPDATA=/data
VOLUME ["/data"]

EXPOSE 8787

# --api-key 默认读环境变量 WORKBUDDY2API_KEY（converter.py 内 _env_compat("KEY")）
# 非回环监听时代码强制要求 Key，否则拒绝启动。
ENTRYPOINT ["python", "converter.py"]
CMD ["--host", "0.0.0.0", "--port", "8787", "--skip-check"]
