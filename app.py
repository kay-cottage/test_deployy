# app.py — ChatGPT 分享页对话提取器（后端：DOM优先 + __NEXT_DATA__/streaming 回退）
import os, re, json, html as _html, urllib.parse as urlparse
from typing import List, Dict, Any, Iterable
import requests
from flask import Flask, request, jsonify, render_template
from bs4 import BeautifulSoup

APP_NAME = "chatgpt-share-extractor"
DEFAULT_ALLOWED = {"chatgpt.com", "chat.openai.com", "shareg.pt"}

app = Flask(__name__, template_folder="templates", static_folder="static")

# 可选：简单访问令牌
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "").strip()

# 允许的服务端抓取域名（避免被滥用做 SSRF）
_env_hosts = os.environ.get("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = {h.strip().lower() for h in _env_hosts.split(",") if h.strip()} if _env_hosts.strip() else DEFAULT_ALLOWED

# 禁止内网 / 元数据网段
PRIVATE_NET_RE = re.compile(
    r"^(localhost|127\.0\.0\.1|0\.0\.0\.0|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+|192\.168\.\d+\.\d+|169\.254\.\d+\.\d+)$",
    re.IGNORECASE,
)

def is_allowed_url(target: str) -> bool:
    try:
        u = urlparse.urlparse(target)
        if u.scheme not in ("http", "https"):
            return False
        host = (u.hostname or "").lower()
        if PRIVATE_NET_RE.match(host or ""):
            return False
        if not ALLOWED_HOSTS:
            return True
        return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    except Exception:
        return False

def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
    resp.raise_for_status()
    resp.encoding = resp.encoding or resp.apparent_encoding
    return resp.text or resp.content.decode(resp.encoding or "utf-8", errors="ignore")

# —— 与前端一致的清洗 ——
KILL_PATTERNS = [
    re.compile(r"ChatGPT\s*说：.*", re.IGNORECASE),
    re.compile(r"复制链接.*", re.IGNORECASE),
    re.compile(r"Copy link.*", re.IGNORECASE),
    re.compile(r"Open in ChatGPT.*", re.IGNORECASE),
    re.compile(r"Use GPT-.*", re.IGNORECASE),
    re.compile(r"Regenerate.*", re.IGNORECASE),
    re.compile(r"模型:.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"Model:.*$", re.IGNORECASE | re.MULTILINE),
]

def post_clean(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    for pat in KILL_PATTERNS:
        t = pat.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def guess_role_bs(el) -> str:
    role = (el.get("data-message-author-role") or "").strip().lower()
    if role in ("assistant", "user"):
        return role
    klass = " ".join(el.get("class") or []).lower()
    return "assistant" if "assistant" in klass or "gpt" in klass else "user"

def extract_via_dom(html_text: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_text, "lxml")
    nodes = soup.select("[data-message-author-role]")
    if not nodes:
        nodes = soup.select("[data-message-id]")
    if not nodes:
        nodes = [el for el in soup.find_all(attrs={"data-testid": True})
                 if "message" in str(el.get("data-testid")).lower()]
    msgs: List[Dict[str, str]] = []
    for el in nodes:
        role = guess_role_bs(el)
        raw = el.get_text(separator="\n", strip=True)
        text = post_clean(raw)
        if not text:
            continue
        compact = re.sub(r"[\s\u200b\u200c\u200d]+", " ", text).strip()
        if not compact:
            continue
        if re.match(r"^(复制链接|Copy link|预览|Open in ChatGPT|Use GPT|登录|Log in|Sign in)\b", compact, re.I):
            continue
        msgs.append({"role": role, "text": text})
    cleaned = []
    for m in msgs:
        if not cleaned or cleaned[-1]["text"] != m["text"] or cleaned[-1]["role"] != m["role"]:
            cleaned.append(m)
    return cleaned

NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>\s*({.*?})\s*</script>',
    re.DOTALL | re.IGNORECASE,
)
PUSH_CHUNK_RE = re.compile(
    r"self\.__next_f\.push\(\s*(\[[\s\S]*?\])\s*\)\s*;",
    re.IGNORECASE,
)

def _coalesce_text_from_content(content):
    if isinstance(content, dict):
        if "parts" in content and isinstance(content["parts"], list):
            return "\n".join([str(x) for x in content["parts"] if x is not None]).strip()
        if "text" in content and isinstance(content["text"], str):
            return content["text"].strip()
    if isinstance(content, list):
        return "\n".join([str(x) for x in content if x is not None]).strip()
    if isinstance(content, str):
        return content.strip()
    return ""

def _walk_messages(obj):
    if isinstance(obj, dict):
        role = None
        if isinstance(obj.get("author"), dict) and isinstance(obj["author"].get("role"), str):
            role = obj["author"]["role"]
        elif isinstance(obj.get("role"), str):
            role = obj["role"]
        if role:
            text = ""
            for key in ("content", "message", "value"):
                if key in obj:
                    text = _coalesce_text_from_content(obj[key])
                    if text:
                        break
            if not text and isinstance(obj.get("text"), str):
                text = obj["text"].strip()
            if text:
                yield {"role": "assistant" if role == "assistant" else "user", "text": text}
        for v in obj.values():
            yield from _walk_messages(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _walk_messages(it)

def extract_via_nextdata(html_text: str):
    all_msgs = []
    m = NEXT_DATA_RE.search(html_text)
    if m:
        try:
            data = json.loads(m.group(1))
            all_msgs.extend(list(_walk_messages(data)))
        except Exception:
            pass
    for mm in PUSH_CHUNK_RE.finditer(html_text):
        payload = mm.group(1)
        try:
            data = json.loads(payload)
            all_msgs.extend(list(_walk_messages(data)))
        except Exception:
            continue
    soup = BeautifulSoup(html_text, "lxml")
    for s in soup.find_all("script", {"type": "application/json"}):
        txt = (s.string or s.get_text() or "").strip()
        if '"author"' in txt and '"role"' in txt:
            try:
                data = json.loads(txt)
                all_msgs.extend(list(_walk_messages(data)))
            except Exception:
                continue
    uniq, seen = [], set()
    for m in all_msgs:
        key = (m["role"], m["text"])
        if key not in seen:
            t = post_clean(_html.unescape(m["text"]))
            if t:
                uniq.append({"role": m["role"], "text": t})
                seen.add(key)
    return uniq

def extract_messages(html_text: str):
    dom_msgs = extract_via_dom(html_text)
    if dom_msgs:
        return dom_msgs
    json_msgs = extract_via_nextdata(html_text)
    return json_msgs

@app.get("/")
def home():
    return render_template("index.html", app_name=APP_NAME, allowed_hosts=", ".join(sorted(ALLOWED_HOSTS)))

@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME}

@app.post("/api/extract")
def api_extract():
    if ACCESS_TOKEN:
        token = request.headers.get("X-Proxy-Token", "")
        if token != ACCESS_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    if "html_file" in request.files and request.files["html_file"]:
        f = request.files["html_file"]
        blob = f.read()
        content = None
        for enc in ("utf-8", "latin-1"):
            try:
                content = blob.decode(enc, errors="ignore")
                break
            except Exception:
                continue
        if content is None:
            content = blob.decode("utf-8", errors="ignore")
        msgs = extract_messages(content)
        return jsonify({"count": len(msgs), "messages": msgs})

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing 'url' or 'html_file'"}), 400
    if not is_allowed_url(url):
        return jsonify({"error": "url not allowed for server-side fetch"}), 400

    try:
        html_text = fetch_html(url)
    except requests.HTTPError as e:
        return jsonify({"error": f"http error: {e.response.status_code}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"request failed: {e.__class__.__name__}: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"unexpected error: {e}"}), 500

    msgs = extract_messages(html_text)
    return jsonify({"count": len(msgs), "messages": msgs})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
