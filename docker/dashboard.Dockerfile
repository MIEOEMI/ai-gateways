# 统一用量面板容器。
#
# 单文件 FastAPI 应用（HTML 内联，无前端构建、无外部 CDN）。
# 依赖同样走被 docker/wheels 里固化下来的离线 wheel。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/share/zoneinfo/${TZ} /etc/localtime && echo "${TZ}" > /etc/timezone

COPY docker/wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels fastapi uvicorn httpx \
    && rm -rf /wheels

WORKDIR /app
COPY dashboard/app.py /app/dashboard_app.py

EXPOSE 8899

# --no-access-log：面板每 30 秒自动刷新，访问日志只会刷屏
CMD ["python", "-m", "uvicorn", "dashboard_app:app", \
     "--host", "0.0.0.0", "--port", "8899", "--no-access-log"]
