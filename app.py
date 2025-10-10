# app.py
import os
import re
import urllib.parse as urlparse
from flask import Flask, request, jsonify, render_template, abort
import requests
from bs4 import BeautifulSoup

APP_NAME = "chatgpt-share-extractor"
DEFAULT_ALLOWED = {"chatgpt.com", "chat.openai.com", "shareg.pt"}

app = Flask(__name__, template_folder="templates", static_folder="static")

# Optional simple access token to avoid public abuse
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "").strip()

# Allowed hosts for server-side fetching
_env_hosts = os.environ.get("ALLOWED_HOSTS", "")
if _env_hosts.strip():
    ALLOWED_HOSTS = {h.strip().lower() for h in _env_hosts.split(",") if h.strip()}
else:
    ALLOWED_HOSTS = DEFAULT_ALLOWED

# Safety: block localhost/metadata/169.254.* SSRF
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
        # allow subdomains of allowed hosts too
        return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)
    except Exception:
        return False

def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    # try to respect server encoding if provided
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text

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
    # collapse spaces and empty lines
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

def guess_role(el) -> str:
    role = (el.get("data-message-author-role") or "").strip().lower()
    if role:
        return "assistant" if role == "assistant" else "user"
    # fallback guess via class names
    klass = " ".join(el.get("class") or []).lower()
    if "assistant" in klass or "gpt" in klass:
        return "assistant"
    return "user"

def extract_messages_from_html(html: str):
    soup = BeautifulSoup(html, "lxml")
    # primary selection
    nodes = soup.select("[data-message-author-role]")
    # fallback: elements that look like messages
    if not nodes:
        nodes = soup.select("[data-message-id]")
    if not nodes:
        # data-testid contains 'message' (case-insensitive) – approximate
        nodes = [el for el in soup.find_all(attrs={"data-testid": True}) if "message" in str(el.get("data-testid")).lower()]
    msgs = []
    for el in nodes:
        role = guess_role(el)
        raw = el.get_text(separator="\n", strip=True)
        text = post_clean(raw)
        if not text:
            continue
        # filter out tiny UI garbage
        compact = re.sub(r"[\s\u200b\u200c\u200d]+", " ", text).strip()
        if not compact:
            continue
        if re.match(r"^(复制链接|Copy link|预览|Open in ChatGPT|Use GPT|登录|Log in|Sign in)\\b", compact, re.I):
            continue
        msgs.append({"role": role, "text": text})
    return msgs

@app.get("/")
def home():
    return render_template("index.html", app_name=APP_NAME, allowed_hosts=", ".join(sorted(ALLOWED_HOSTS)))

@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME}

@app.post("/api/extract")
def api_extract():
    # Optional simple token
    if ACCESS_TOKEN:
        token = request.headers.get("X-Proxy-Token", "")
        if token != ACCESS_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    # File upload takes priority
    if "html_file" in request.files and request.files["html_file"]:
        f = request.files["html_file"]
        try:
            content = f.read().decode("utf-8", errors="ignore")
        except Exception:
            content = f.read().decode("latin-1", errors="ignore")
        msgs = extract_messages_from_html(content)
        return jsonify({"count": len(msgs), "messages": msgs})

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing 'url' or 'html_file'"}), 400

    if not is_allowed_url(url):
        return jsonify({"error": "url not allowed for server-side fetch"}), 400

    try:
        html = fetch_html(url)
    except requests.HTTPError as e:
        return jsonify({"error": f"http error: {e.response.status_code}"}), 502
    except requests.RequestException as e:
        return jsonify({"error": f"request failed: {e.__class__.__name__}: {e}"}), 502
    except Exception as e:
        return jsonify({"error": f"unexpected error: {e}"}), 500

    msgs = extract_messages_from_html(html)
    return jsonify({"count": len(msgs), "messages": msgs})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
