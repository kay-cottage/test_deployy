#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT Share Extractor (simulate-browser headers only)
- GET  /            -> 内嵌前端页（输入分享 URL）
- POST /api/extract -> {"url": "..."} 后端 requests 抓取 + 解析对话
- GET  /api/probe   -> ?url=... 返回响应头/片段/锚点计数（排障）

依赖：pip install flask requests gunicorn
启动：gunicorn app:app
"""

import os, re, json, socket, ipaddress, urllib.parse as urlparse
from typing import List, Tuple
from flask import Flask, request, jsonify, Response
import requests

# ================== 配置 ==================
USER_AGENT = os.environ.get(
    "UA",
    # 可换成你自己的浏览器 UA；默认给一个常见桌面 Chrome UA
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = float(os.environ.get("TIMEOUT", 15))
MAX_BYTES = int(os.environ.get("MAX_BYTES", 8_000_000))
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "").strip()

# SSRF 白名单（逗号分隔）。为空则允许任何“公网”主机（会排除内网/本地）
_env_allowed = os.environ.get("ALLOWED_HOSTS", "").strip()
ALLOWED_HOSTS = {h.strip().lower() for h in _env_allowed.split(",") if h.strip()} if _env_allowed else None

# ================== 浏览器请求头（关键） ==================
BROWSER_HEADERS = {
    # 最关键三件套
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    # 更像浏览器的导航请求
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
    # 适度提供 Referer（可按需改成主页）
    "Referer": "https://chatgpt.com/",
    # UA-CH（不少站点会参考）
    "sec-ch-ua": '"Chromium";v="126", "Not.A/Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    # requests 会自动解压 gzip/br，这里保留更像浏览器
    "Accept-Encoding": "gzip, deflate, br",
}

# ================== 前端页面 ==================
INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>ChatGPT 分享页对话提取器</title>
    <style>
      :root{--bg:#0b0d10;--panel:#11161c;--ink:#e6e9ef;--muted:#9aa3af;--acc:#6ee7b7;--err:#ff6b6b;--border:#1f2937}
      html,body{height:100%}body{margin:0;background:var(--bg);color:var(--ink);
      font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,Noto Sans,Helvetica,Arial}
      .wrap{max-width:960px;margin:36px auto;padding:0 16px}
      .card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px}
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
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <h1>ChatGPT 分享页对话提取器</h1>
        <p class="sub">输入分享链接（例：<code>https://chatgpt.com/share/…</code>）。服务端抓取并解析。</p>
        <label for="url">分享链接</label>
        <input id="url" type="url" placeholder="https://chatgpt.com/share/..." />
        <div class="bar">
          <button id="go" class="primary">提取对话</button>
          <button id="clear">清空</button>
          <a id="probe" href="#" style="margin-left:auto">诊断 /probe</a>
        </div>
        <div class="toolbar">
          <button id="saveTxt" class="accent" disabled>下载 .txt</button>
          <button id="saveJson" class="accent" disabled>下载 .json</button>
          <span id="stats" class="small"></span>
        </div>
        <div id="status" class="status"></div>
        <div id="results" class="results"></div>
      </div>
    </div>
    <script>
      const $ = s => document.querySelector(s);
      const url = $('#url'), go = $('#go'), clearBtn = $('#clear'),
            statusEl = $('#status'), results = $('#results'),
            saveTxt = $('#saveTxt'), saveJson = $('#saveJson'), stats = $('#stats'),
            probeA = $('#probe');
      let lastData = [];

      probeA.addEventListener('click', async (e)=>{
        e.preventDefault();
        const u = (url.value || '').trim();
        if (!u) { alert('先输入 URL'); return; }
        const r = await fetch(`/api/probe?url=${encodeURIComponent(u)}`);
        const t = await r.text();
        const w = window.open(); w.document.write(`<pre>${t.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</pre>`);
      });

      clearBtn.addEventListener('click', () => {
        url.value = ''; results.innerHTML = ''; status(''); stats.textContent = '';
        toggleDownloads(false);
      });

      go.addEventListener('click', async () => {
        const u = (url.value || '').trim();
        results.innerHTML = ''; status('处理中…'); stats.textContent = ''; toggleDownloads(false);
        if (!u) { status('请输入分享链接', true); return; }
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

# ================== 解析逻辑 ==================
ROLE_MARKER = re.compile(r'data-message-author-role="(user|assistant)"', re.I)

def _strip_html(chunk: str) -> str:
    # 去掉 script/style，去标签，解码实体，归一化空白
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
    # 清理分享页里常见的 UI 文案
    text = re.sub(r"(?mi)^ChatGPT\s*说：.*$", "", text)
    text = re.sub(r"(?mi)^\s*(复制链接|Open in ChatGPT|Copy link|Regenerate|模型:|Model:).*$", "", text)
    return text.strip(" <>").strip()

def extract_messages(raw_html: str) -> List[Tuple[str, str]]:
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
        text = _post_clean(_strip_html(chunk))
        if text:
            results.append((role, text))
        pos = end_content
    return results

# ================== SSRF 防护 & 抓取 ==================
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

def _safe_get(url: str) -> requests.Response:
    parsed = urlparse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("仅允许 http/https")
    if not _host_ok(parsed.hostname or ""):
        raise ValueError("目标主机不被允许（可能为内网/白名单之外）")

    headers = BROWSER_HEADERS.copy()  # ← 高仿浏览器请求头

    sess = requests.Session()
    resp = sess.get(
        url,
        headers=headers,
        timeout=TIMEOUT,
        allow_redirects=True,
        stream=True,
    )
    resp.raise_for_status()
    return resp

def _read_text(resp: requests.Response) -> str:
    total = 0
    chunks = []
    for chunk in resp.iter_content(8192):
        if chunk:
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError(f"响应体超过大小限制 {MAX_BYTES} 字节")
    data = b"".join(chunks)
    enc = resp.encoding or "utf-8"
    try:
        return data.decode(enc, errors="replace")
    except Exception:
        return data.decode("utf-8", errors="replace")

# ================== Flask 应用 ==================
app = Flask(__name__)

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
        if not u:
            return jsonify(ok=False, error="缺少 url"), 400
        resp = _safe_get(u)
        html = _read_text(resp)
        pairs = extract_messages(html)
        messages = [{"idx": i+1, "role": r, "text": t} for i, (r, t) in enumerate(pairs)]
        return jsonify(ok=True, count=len(messages), messages=messages)
    except requests.HTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", 502)
        return jsonify(ok=False, error=f"上游 HTTP 错误：{code}"), 502
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400

@app.route("/api/probe", methods=["GET"])
def api_probe():
    """返回抓到的原始页面信息，便于排障。"""
    try:
        u = (request.args.get("url") or "").strip()
        if not u:
            return Response("缺少 url", status=400)
        resp = _safe_get(u)
        txt = _read_text(resp)
        hits = len(ROLE_MARKER.findall(txt))
        head = {
            "status": f"{resp.status_code}",
            "final_url": resp.url,
            "headers": dict(resp.headers),
        }
        preview = (
            f"--- HEAD ---\n{json.dumps(head, ensure_ascii=False, indent=2)}\n\n"
            f"--- FIRST 2000 ---\n{txt[:2000]}\n\n"
            f"--- LAST 2000 ---\n{txt[-2000:]}\n\n"
            f"(marker hits: {hits})\n"
        )
        return Response(preview, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"probe error: {e}", status=400, mimetype="text/plain; charset=utf-8")

@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify(ok=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
