# 部署说明
## Render（推荐）
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app:app --host 0.0.0.0 --port 8000`
- 环境变量：ALLOWED_HOSTS，ACCESS_TOKEN，DEFAULT_REFERER，TIMEOUT

## Docker
docker build -t py-cors-proxy .
docker run -p 8000:8000 -e ALLOWED_HOSTS="chat.openai.com,chatgpt.com" py-cors-proxy

## 本地
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
