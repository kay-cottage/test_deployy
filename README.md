# ChatGPT Share Extractor (Flask)

服务器代抓取 **ChatGPT 分享页** 并解析出「用户 / 助手」对话列表，返回 JSON；
前端同域展示并支持一键下载 `.txt` / `.json`。**无跨域 (CORS) 问题**。

## 功能
- `POST /api/extract`：
  - JSON: `{"url": "https://chatgpt.com/share/xxxx"}` → 服务器用 `requests` 代抓取并解析。
  - 或`multipart/form-data` 上传 `html_file`（本地保存的分享页）。
- `GET /`：简洁 UI（你的上传/解析页面）。
- `GET /health`：健康检查。

## 安全
- 通过 `ALLOWED_HOSTS` 环境变量限制可代抓取的域名（逗号分隔）。默认仅允许：`chatgpt.com, chat.openai.com, share.g.pt`。
- 防 SSRF：拒绝 `localhost`、内网网段、`169.254.*` 等。
- 可选 `ACCESS_TOKEN`：若设置，则前端需在请求头携带 `X-Proxy-Token`。

## 本地运行
```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export FLASK_ENV=production
# 可选：export ALLOWED_HOSTS="chatgpt.com,chat.openai.com,share.g.pt"
gunicorn app:app -w 2 -k gthread -t 60 -b 0.0.0.0:8000
# 然后打开 http://127.0.0.1:8000/
```

## 部署到 Render
**方式 A：通过 `render.yaml`**（推荐，一键部署）  
1. 将本仓库推送到 GitHub。  
2. Render → New → Blueprint → 连接你的仓库（会识别 `render.yaml`）。  
3. 部署完成后即可访问。

**方式 B：手动 Web Service**
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- 环境变量：
  - `ALLOWED_HOSTS`: `chatgpt.com,chat.openai.com,share.g.pt`
  - `ACCESS_TOKEN`: （可选）任意字符串

## API 示例
```bash
curl -X POST https://your-app.onrender.com/api/extract \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://chatgpt.com/share/68e851b3-03c4-8002-9cfe-0f9b8d4f6d22"}'
```

响应:
```json
{
  "count": 42,
  "messages": [
    {"role":"user","text":"..."},
    {"role":"assistant","text":"..."}
  ]
}
```

## 目录结构
```
.
├── app.py
├── requirements.txt
├── Procfile
├── render.yaml
└── templates/
    └── index.html
```

## 许可
MIT
