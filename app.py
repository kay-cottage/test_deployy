#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Single-file Flask app: ChatGPT Share Extractor
- GET  /            -> 内嵌前端网页（输入分享 URL，点击提取）
- POST /api/extract -> {"url": "..."}，服务端 requests 获取 HTML 并解析，返回对话
部署：pip install flask requests; gunicorn app:app
"""

import os, re, json, socket, ipaddress, urllib.parse as urlparse
from typing import List, Tuple, Dict, Optional

# ---- 依赖 ----
try:
    from flask import Flask, request, jsonify, Response
except Exception as e:
    raise SystemExit("缺少 Flask：请先 `pip install flask`")
try:
    import requests
except Exception as e:
    raise SystemExit("缺少 requests：请先 `pip install requests`")


# ========== 配置 ==========
USER_AGENT = os.environ.get(
    "UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = float(os.environ.get("TIMEOUT", 15))
MAX_BYTES = int(os.environ.get("MAX_BYTES", 6_000_000))  # 响应体大小上限（字节）
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "").strip()

# 允许的主机白名单（逗号分隔）。为空则允许任意公网主机；建议线上设置为：
# "chatgpt.com,chat.openai.com,shareg.pt"
_env_allowed = os.environ.get("ALLOWED_HOSTS", "").strip()
ALLOWED_HOSTS = {h.strip().lower() for h in _env_allowed.split(",") if h.strip()} if _env_allowed else None


# ========== 前端页面 ==========
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>ChatGPT 分享页对话提取器 · Flask</title>
    <style>
      :root{--bg:#0b0d10;--panel:#11161c;--ink:#e6e9ef;--muted:#9aa3af;--acc:#6ee7b7;--err:#ff6b6b;--border:#1f2937}
      html,body{height:100%}body{margin:0;background:var(--bg);color:var(--ink);
      font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Noto Sans,Helvetica,Arial}
      .wrap{max-width:960px;margin:36px auto;padding:0 16px}
      .card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px 18px 10px;
        box-shadow:0 8px 26px rgba(0,0,0,.25)}
      h1{margin:0 0 8px;font-size:22px}
      .sub{margin:0 0 16px;color:var(--muted)}
      label{display:block;color:var(--muted);margin:.4rem 0 .25rem}
      input[type=url]{width:100%;box-sizing:border-box;background:#0c1117;color:var(--ink);
        border:1px solid var(--border);border-radius:10px;padding:12px 14px;outline:none}
      .bar{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
      button{appearance:none;background:#0f141b;border:1px solid var(--border);color:var(--ink);
        padding:10px 14px;border-radius:10px;cursor:pointer}
      button.primary{background:linear-gradient(180deg,#1f2937,#0f172a);border-color:#223047}
      button.accent{background:linear-gradient(180deg,#064e3b,#052e2f);border-color:#065f46;color:#d1fae5}
      button[disabled]{opacity:.6;cursor:not-allowed}
      .status{min-height:22px;color:var(--muted);margin-top:10px}
      .status.err{color:var(--err)}
      .results{margin-top:16px;display:grid;gap:10px}
      .msg{background:#0a0f14;border:1px solid var(--border);border-radius:12px;padding:12px 14px}
      .meta{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:12px;margin-bottom:6px}
      .role{padding:2px 8px;border-radius:999px;font-weight:600}
      .role.user{background:rgba(59,130,246,.15);color:#bfdbfe;border:1px solid #1d4ed8}
      .role.assistant{background:rgba(16,185,129,.15);color:#bbf7d0;border:1px solid #065f46}
      .txt{white-space:pre-wrap}
      .toolbar{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:8px}
      .small{font-size:12px;color:var(--muted)}
      footer{margin:18px 0;color:var(--muted);font-size:12px}
      code.inline{background:#0c1218;border:1px solid var(--border);padding:2px 6px;border-radius:6px}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <h1>ChatGPT 分享页对话提取器</h1>
        <p class="sub">后端使用 <code class="inline">requests</code> 拉取并解析。输入分享链接，例如：
          <code class="inline">https://chatgpt.com/share/68e851b3-03c4-8002-9cfe-0f9b8d4f6d22</code></p>
        <label for="url">分享链接（URL）</label>
        <input id="url" type="url" placeholder="https://chatgpt.com/share/..." />
        <div class="bar">
          <button id="go" class="primary">提取对话</button>
          <button id="clear">清空</button>
          <span id="status" class="status"></span>
        </div>
        <div class="toolbar">
          <button id="saveTxt" class="accent" disabled>下载 .txt</button>
          <button id="saveJson" class="accent" disabled>下载 .json</button>
          <span id="stats" class="small"></span>
        </div>
        <div id="results" class="results"></div>
        <footer>提示：本工具在服务端抓取网页，避免浏览器 CORS 问题。</footer>
      </div>
    </div>
    <script>
      const $ = s => document.querySelector(s);
      const url = $('#url'), go = $('#go'), clearBtn = $('#clear'),
            statusEl = $('#status'), results = $('#results'),
            saveTxt = $('#saveTxt'), saveJson = $('#saveJson'), stats = $('#stats');
      let lastData = [];

      clearBtn.addEventListener('click', () => {
        url.value = ''; results.innerHTML = ''; status(''); stats.textContent = '';
        toggleDownloads(false);
      });

      go.addEventListener('click', async () => {
        const u = (url.value || '').trim();
        results.innerHTML = ''; status('处理中…'); stats.textContent = ''; toggleDownloads(false);
        if (!u) { status('请输入分享链接'); return; }
        try {
          const resp = await fetch('/api/extract', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: u })
          });
          const data = await resp.json();
          if (!resp.ok || !data.ok) throw new Error(data.error || ('HTTP '+resp.status));
          lastData = data.messages || [];
          render(lastData);
          status('完成'); stats.textContent = `共 ${lastData.length} 条 · ${new Date().toLocaleString()}`;
          toggleDownloads(true);
        } catch (e) {
          console.error(e); status(e.message || String(e), true);
        }
      });

      function render(messages){
        results.innerHTML = '';
        if (!messages.length) { results.innerHTML = '<div class="small">未提取到消息。</div>'; return; }
        messages.forEach((m,i) => {
          const div = document.createElement('div');
          div.className = 'msg';
          const meta = document.createElement('div');
          meta.className = 'meta';
          const role = document.createElement('span');
          role.className = 'role ' + (m.role === 'assistant' ? 'assistant' : 'user');
          role.textContent = m.role.toUpperCase();
          const idx = document.createElement('span'); idx.textContent = '#' + (i+1);
          meta.appendChild(role); meta.appendChild(idx);
          const txt = document.createElement('div');
          txt.className = 'txt'; txt.textContent = m.text;
          div.appendChild(meta); div.appendChild(txt);
          results.appendChild(div);
        });
      }

      function toggleDownloads(enabled){
        saveTxt.disabled = !enabled; saveJson.disabled = !enabled;
        if (!enabled) { saveTxt.onclick = null; saveJson.onclick = null; return; }
        const txt = toTxt(lastData);
        const json = JSON.stringify(lastData.map((m, i) => ({idx: i+1, role: m.role, text: m.text})), null, 2);
        saveTxt.onclick = () => download('chat_messages.txt', txt, 'text/plain');
        saveJson.onclick = () => download('chat_messages.json', json, 'application/json');
      }

      function toTxt(arr){
        const parts = [];
        arr.forEach((m,i) => { parts.push(`--- ${i+1}. ${m.role.toUpperCase()} ---`); parts.push(m.text); parts.push(''); });
        return parts.join('\\n');
      }

      function download(name, content, mime){
        const blob = new Blob([content], {type:mime});
        const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = name; a.click();
        setTimeout(()=> URL.revokeObjectURL(a.href), 1000);
      }

      function status(t, err=false){ statusEl.textContent = t || ''; statusEl.classList.toggle('err', !!err); }
    </script>
  </body>
</html>
"""


# ========== 解析逻辑 ==========
ROLE_MARKER = re.compile(r'data-message-author-role="(user|assistant)"', re.I)

def _strip_html(chunk: str) -> str:
    chunk = re.sub(r"<script[^>]*>.*?</script>", "", chunk, flags=re.DOTALL|re.IGNORECASE)
    chunk = re.sub(r"<style[^>]*>.*?</style>", "", chunk, flags=re.DOTALL|re.IGNORECASE)
    chunk = re.sub(r"<[^>]+>", "", chunk)
    try:
        import html as _html
        chunk = _html.unescape(chunk)
    except Exception:
        pass
    chunk = chunk.replace("\r\n", "\n").replace("\r", "\n")
    chunk = re.sub(r"[ \t]+", " ", chunk)
    chunk = re.sub(r"\n{3,}", "\n\n", chunk)
    return chunk.strip()

def _post_clean(text: str) -> str:
    text = re.sub(r"ChatGPT\s*说：.*", "", text, flags=re.I)
    text = re.sub(r"(?m)^\s*复制链接.*$", "", text)
    text = re.sub(r"(?mi)^\s*Open in ChatGPT.*$", "", text)
    text = re.sub(r"(?mi)^\s*Copy link.*$", "", text)
    text = re.sub(r"(?mi)^\s*Regenerate.*$", "", text)
    text = re.sub(r"(?mi)^\s*模型:.*$", "", text)
    text = re.sub(r"(?mi)^\s*Model:.*$", "", text)
    text = text.strip(" <>")
    return text.strip()

def extract_messages(raw_html: str) -> List[Tuple[str, str]]:
    """从 HTML 源码中提取 (role, text) 列表。"""
    results: List[Tuple[str, str]] = []
    pos = 0
    while True:
        m = ROLE_MARKER.search(raw_html, pos)
        if not m:
            break
        role = m.group(1).lower()
        tag_close = raw_html.find(">", m.end())
        if tag_close == -1:
            tag_close = m.end()
        start_content = tag_close + 1
        next_m = ROLE_MARKER.search(raw_html, start_content)
        end_content = next_m.start() if next_m else len(raw_html)
        chunk = raw_html[start_content:end_content]
        text = _strip_html(chunk)
        text = _post_clean(text)
        results.append((role, text))
        pos = end_content
    return results


# ========== 安全抓取（避免 SSRF） ==========
PRIVATE_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

def _is_ip_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in PRIVATE_NETS)
    except Exception:
        return True  # 无法解析就当作不安全

def _host_ok(host: str) -> bool:
    h = (host or "").lower().strip()
    if not h: return False
    if h in {"localhost", "localhost.", "ip6-localhost"}: return False
    if ALLOWED_HOSTS is not None and h not in ALLOWED_HOSTS:
        return False
    try:
        infos = socket.getaddrinfo(h, None)
        ips = {ai[4][0] for ai in infos if ai and ai[4]}
        if not ips: return False
        return not any(_is_ip_private(ip) for ip in ips)
    except Exception:
        return False

def _safe_fetch(url: str) -> str:
    parsed = urlparse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("仅允许 http/https 链接")
    if not _host_ok(parsed.hostname or ""):
        raise ValueError("目标主机不被允许（可能为内网/本地地址或不在白名单）")
    headers = {"User-Agent": USER_AGENT}
    with requests.get(url, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
        r.raise_for_status()
        total = 0
        chunks = []
        for chunk in r.iter_content(8192):
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError(f"响应体超过大小限制 {MAX_BYTES} 字节")
        data = b"".join(chunks)
        enc = r.encoding or "utf-8"
        try:
            text = data.decode(enc, errors="replace")
        except Exception:
            text = data.decode("utf-8", errors="replace")
        return text


# ========== Flask 应用 ==========
app = Flask(__name__)  # ← 这里显式暴露名为 app 的实例（给 gunicorn app:app 用）

@app.after_request
def add_cors(resp: Response):
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = request.headers.get("Access-Control-Request-Headers", "Content-Type, Authorization")
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp

@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")

@app.route("/api/extract", methods=["POST", "OPTIONS"])
def api_extract():
    if request.method == "OPTIONS":
        return ("", 204)
    if ACCESS_TOKEN:
        token = request.headers.get("X-Access-Token", "")
        if token != ACCESS_TOKEN:
            return jsonify(ok=False, error="Unauthorized"), 401
    try:
        data = request.get_json(silent=True) or {}
        u = (data.get("url") or "").strip()
        if not u: return jsonify(ok=False, error="缺少 url"), 400
        raw_html = _safe_fetch(u)
        pairs = extract_messages(raw_html)
        messages = [{"idx": i+1, "role": r, "text": t} for i, (r, t) in enumerate(pairs)]
        return jsonify(ok=True, count=len(messages), messages=messages)
    except requests.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", 502)
        return jsonify(ok=False, error=f"上游 HTTP 错误：{code}"), 502
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # Render 会注入 PORT；本地可用默认 5000
    app.run(host="0.0.0.0", port=port, debug=False)
