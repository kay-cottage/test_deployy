# app.py — FastAPI 版跨域代理（含白名单/令牌/Referer/UA）
import os
from urllib.parse import urlparse
from typing import Optional
import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import PlainTextResponse
from starlette.middleware.cors import CORSMiddleware

ALLOWED_HOSTS = {h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")  # 可选：需要时前端 header: X-Proxy-Token
TIMEOUT = float(os.getenv("TIMEOUT", "15"))
DEFAULT_REFERER = os.getenv("DEFAULT_REFERER", "")  # 可选：为部分站点补 Referer

app = FastAPI(title="CORS Proxy")
# CORS：放行所有来源（前端更易用）。如需更严谨可改为你的站点域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=86400,
)

HOP_BY_HOP = {
    "connection","keep-alive","proxy-authenticate","proxy-authorization",
    "te","trailer","transfer-encoding","upgrade",
}

BROWSER_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
    "sec-fetch-site": "none",
    "sec-fetch-mode": "navigate",
    "sec-fetch-dest": "document",
}

def check_allowed(host: str):
    if not ALLOWED_HOSTS:
        return True
    return host in ALLOWED_HOSTS

@app.get("/")
def root():
    return PlainTextResponse("OK. Use /proxy?url=https://example.com")

@app.api_route("/proxy", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, url: Optional[str] = None, debug: Optional[int] = 0):
    if ACCESS_TOKEN:
        token = request.headers.get("X-Proxy-Token") or ""
        if token != ACCESS_TOKEN:
            raise HTTPException(status_code=401, detail="Unauthorized")

    if not url:
        raise HTTPException(status_code=400, detail="Missing ?url=")

    try:
        p = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad url")
    if p.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Protocol not allowed")
    if not check_allowed(p.hostname or ""):
        raise HTTPException(status_code=403, detail=f"Host not allowed: {p.hostname}")

    # 复制请求头（去掉 hop-by-hop）
    fwd_headers = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP:
            continue
        # 默认不转发 Cookie，减少风控；如需登录可放开：
        if lk == "cookie":
            continue
        fwd_headers[k] = v

    for k, v in BROWSER_HEADERS.items():
        fwd_headers.setdefault(k, v)

    # 可选 Referer
    if DEFAULT_REFERER and "referer" not in {k.lower() for k in fwd_headers}:
        fwd_headers["Referer"] = DEFAULT_REFERER

    method = request.method.upper()
    content = await request.body() if method not in ("GET", "HEAD") else None

    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT) as client:
        try:
            upstream = await client.request(method, url, headers=fwd_headers, content=content)
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {e}")

    # 过滤某些不适合直出的响应头
    out_headers = {}
    for k, v in upstream.headers.items():
        lk = k.lower()
        if lk in HOP_BY_HOP or lk == "set-cookie":
            continue
        out_headers[k] = v

    if debug:
        out_headers["x-upstream-status"] = str(upstream.status_code)
        out_headers["x-upstream-url"] = url

    return Response(content=upstream.content, status_code=upstream.status_code, headers=out_headers)
