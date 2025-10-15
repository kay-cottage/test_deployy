# 官方 Playwright Python 镜像（含浏览器）
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# 生产启动（禁用 Flask reloader，使用 gunicorn）
ENV PORT=5006
CMD bash -lc 'gunicorn app:app --bind 0.0.0.0:${PORT:-5006} --workers 2 --threads 8 --timeout 120'
