"""
chatgpt-share-extractor
~~~~~~~~~~~~~~~~~~~~~~~~

This module provides a minimal Flask application that can extract
conversation messages from ChatGPT share pages.  The extraction logic
replicates the behaviour of the accompanying `index.html` front‑end and
adds a fallback for pages that deliver their content via embedded
JSON.  When a HTML document is provided—either by direct upload or
fetched from a URL—the extractor will attempt to identify message
containers, determine the role (user or assistant) and gather the
corresponding text.

Key features:

* **DOM extraction** – Search for elements marked with
  `data-message-author-role` and `data-message-id`.  If not found,
  fallback to `[data-message-id]` or `[data-testid*="message" i]` as
  specified in the front‑end.  Within each message container,
  the extractor looks for a `.whitespace-pre-wrap` or `.markdown`
  element to capture rich text; if none exist, it falls back to the
  entire element’s text.  The same post‑processing rules used on the
  front‑end (`postClean`) are applied to remove UI detritus and
  collapse whitespace.

* **JSON fallback** – Some share pages are rendered via Next.js and
  only hydrate their content on the client.  In such cases the
  extracted DOM will be empty.  To handle these cases the extractor
  scans all `<script>` tags for JSON and attempts to parse any
  objects containing an `author.role` and a `content` (with `parts` or
  `text`).  A recursive walk collects these messages and performs the
  same cleaning as the DOM path.

This file defines a Flask route `/api/extract` to accept either a
remote URL (subject to a simple hostname allowlist and SSRF
protections) or a local HTML upload.  The response includes the
message count and a list of message dictionaries with `role` and
`text` keys.

To run locally:

    $ pip install -r requirements.txt
    $ python app.py

Visit `http://127.0.0.1:8000/` to access the simple upload UI (see
`index.html`).
"""

import os
import re
import json
import html as html_mod
import urllib.parse as urlparse
from typing import List, Iterable, Dict, Any

import requests
from flask import Flask, request, jsonify, render_template
from bs4 import BeautifulSoup

APP_NAME = "chatgpt-share-extractor"
DEFAULT_ALLOWED = {"chatgpt.com", "chat.openai.com", "shareg.pt"}

# Flask setup
# Configure Flask to look for templates in the same directory as this
# file.  The provided `index.html` lives in the repository root, so
# using the module directory allows Flask to find it without requiring
# a separate `templates` folder.  Static files (if any) can still be
# served from a `static` subdirectory.
app = Flask(__name__, template_folder=os.path.dirname(__file__), static_folder="static")

# Optional simple access token to avoid public abuse
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "").strip()

# Allowed hosts for server-side fetching (comma separated env var)
_env_hosts = os.environ.get("ALLOWED_HOSTS", "")
if _env_hosts.strip():
    ALLOWED_HOSTS = {h.strip().lower() for h in _env_hosts.split(",") if h.strip()}
else:
    ALLOWED_HOSTS = DEFAULT_ALLOWED

# SSRF protection: block localhost/metadata/private IP ranges
PRIVATE_NET_RE = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|"
    r"10\.\d+\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|"
    r"169\.254\.\d+\.\d+)$",
    re.IGNORECASE,
)


def is_allowed_url(target: str) -> bool:
    """Return True if the URL is allowed to be fetched server‑side.

    Disallows non‑HTTP schemes and private IPs.  Allows only hosts
    explicitly listed in ALLOWED_HOSTS (or any host if the list is
    empty).
    """
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
    """Fetch the target URL and return its HTML text.

    Uses a browser‑like User-Agent and attempts to respect the server’s
    encoding declaration.  Raises requests exceptions on failure.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
    resp.raise_for_status()
    # Use server provided encoding, fall back to apparent encoding
    resp.encoding = resp.encoding or resp.apparent_encoding
    try:
        return resp.text
    except Exception:
        return resp.content.decode(resp.encoding or "utf-8", errors="ignore")


# Cleaning patterns as per the front‑end
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
    """Apply the same cleaning rules as the front‑end postClean()."""
    if not text:
        return ""
    # Normalize newlines
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    # Remove known UI garbage
    for pat in KILL_PATTERNS:
        t = pat.sub("", t)
    # Collapse spaces and multiple blank lines
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def extract_via_dom(html_text: str) -> List[Dict[str, str]]:
    """Extract messages by scanning the DOM for message containers.

    The logic mirrors the front‑end’s extractMessagesFromHtml():
    1. Look for elements with data-message-author-role and data-message-id.
    2. If none, fall back to data-message-id or data-testid*="message".
    3. For each message, retrieve text from a descendant with
       `.whitespace-pre-wrap` or `.markdown` if present; otherwise use
       the element’s full text.
    4. Clean and filter the messages.
    """
    soup = BeautifulSoup(html_text, "lxml")
    # First, find explicit message containers
    nodes = soup.find_all(attrs={"data-message-author-role": True, "data-message-id": True})
    # Fallback: just data-message-id
    if not nodes:
        nodes = soup.find_all(attrs={"data-message-id": True})
    # Fallback: any element whose data-testid contains "message" (case-insensitive)
    if not nodes:
        nodes = [
            el
            for el in soup.find_all(attrs={"data-testid": True})
            if "message" in str(el.get("data-testid", "")).lower()
        ]

    messages: List[Dict[str, str]] = []
    for el in nodes:
        role_attr = el.get("data-message-author-role", "").strip().lower()
        # Normalise role
        role = "assistant" if role_attr == "assistant" else "user"
        # Attempt to get content from a dedicated text element
        content_el = el.select_one(".whitespace-pre-wrap") or el.select_one(".markdown")
        if content_el is not None:
            raw_text = content_el.get_text(separator="\n", strip=True)
        else:
            raw_text = el.get_text(separator="\n", strip=True)
        cleaned = post_clean(raw_text)
        if not cleaned:
            continue
        # Remove UI-only lines (e.g. preview, login) and collapse zero-width spaces
        compact = re.sub(r"[\s\u200b\u200c\u200d]+", " ", cleaned).strip()
        if not compact:
            continue
        if re.match(
            r"^(复制链接|Copy link|预览|Open in ChatGPT|Use GPT|登录|Log in|Sign in)\b",
            compact,
            re.IGNORECASE,
        ):
            continue
        messages.append({"role": role, "text": cleaned})
    return messages


def _coalesce_text_from_content(content: Any) -> str:
    """Helper to coalesce various forms of content into a string."""
    # Common content shapes: {parts: [..]}, {text: ..}, list, str
    if isinstance(content, dict):
        if "parts" in content and isinstance(content["parts"], list):
            return "\n".join(str(x) for x in content["parts"] if x is not None).strip()
        if "text" in content and isinstance(content["text"], str):
            return content["text"].strip()
    if isinstance(content, list):
        return "\n".join(str(x) for x in content if x is not None).strip()
    if isinstance(content, str):
        return content.strip()
    return ""


def _walk_messages(obj: Any) -> Iterable[Dict[str, str]]:
    """Recursively traverse JSON to find message objects.

    Yields dicts with keys 'role' and 'text' where 'role' is either
    'assistant' or 'user'.
    """
    if isinstance(obj, dict):
        role = None
        # Determine role via known keys
        if "author" in obj and isinstance(obj["author"], dict):
            role = obj["author"].get("role")
        elif "role" in obj and isinstance(obj["role"], str):
            role = obj["role"]
        # Normalise role and coalesce text
        if role:
            text = ""
            if "content" in obj:
                text = _coalesce_text_from_content(obj["content"])
            elif "message" in obj:
                text = _coalesce_text_from_content(obj["message"])
            elif "value" in obj:
                text = _coalesce_text_from_content(obj["value"])
            elif "text" in obj and isinstance(obj["text"], str):
                text = obj["text"].strip()
            if text:
                yield {"role": "assistant" if role == "assistant" else "user", "text": text}
        for v in obj.values():
            yield from _walk_messages(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _walk_messages(it)


def extract_via_json(html_text: str) -> List[Dict[str, str]]:
    """Extract messages by scanning embedded JSON in script tags.

    This looks for any `<script>` contents that parse as JSON objects or
    arrays and walks them for structures containing author roles and
    content.  This is a fallback when the DOM contains no message
    containers.
    """
    soup = BeautifulSoup(html_text, "lxml")
    msgs: List[Dict[str, str]] = []
    # Extract raw script contents
    script_tags = soup.find_all("script")
    for tag in script_tags:
        # Skip tags with src attributes or empty contents
        if tag.attrs.get("src"):
            continue
        data = tag.string or tag.get_text()
        if not data:
            continue
        # Heuristic: only attempt JSON parsing if it looks like JSON
        snippet = data.strip()
        # Trim wrapper if necessary (e.g. self.__next_f.push([...]);)
        # Remove leading variable assignments
        # Look for the first '[' or '{'
        start = None
        for i, ch in enumerate(snippet):
            if ch in "[{":
                start = i
                break
        if start is None:
            continue
        candidate = snippet[start:]
        # If the script contains multiple JSON objects (e.g. push arrays), try to split
        for part in re.split(r"(?<=\})(?=\{)|(?<=\])(?=\[)", candidate):
            part = part.strip().rstrip(";")
            if not part or (not part.startswith("{") and not part.startswith("[")):
                continue
            try:
                obj = json.loads(part)
            except Exception:
                continue
            for m in _walk_messages(obj):
                txt = post_clean(html_mod.unescape(m["text"]))
                if txt:
                    msgs.append({"role": m["role"], "text": txt})
    # Deduplicate and preserve order
    seen = set()
    unique_msgs: List[Dict[str, str]] = []
    for m in msgs:
        key = (m["role"], m["text"])
        if key not in seen:
            unique_msgs.append(m)
            seen.add(key)
    return unique_msgs


@app.get("/")
def home():
    return render_template("index.html", app_name=APP_NAME, allowed_hosts=", ".join(sorted(ALLOWED_HOSTS)))


@app.get("/health")
def health():
    return {"status": "ok", "app": APP_NAME}


@app.post("/api/extract")
def api_extract():
    """API endpoint to extract messages from a URL or uploaded HTML."""
    # Simple token authentication
    if ACCESS_TOKEN:
        token = request.headers.get("X-Proxy-Token", "")
        if token != ACCESS_TOKEN:
            return jsonify({"error": "unauthorized"}), 401

    # If an HTML file is uploaded, prioritise it
    if "html_file" in request.files and request.files["html_file"]:
        f = request.files["html_file"]
        blob = f.read()
        # Try UTF-8 then Latin-1
        for enc in ("utf-8", "latin-1"):
            try:
                content = blob.decode(enc, errors="ignore")
                break
            except Exception:
                continue
        else:
            content = blob.decode("utf-8", errors="ignore")
        dom_msgs = extract_via_dom(content)
        json_msgs = extract_via_json(content) if not dom_msgs else []
        msgs = dom_msgs or json_msgs or []
        return jsonify({"count": len(msgs), "messages": msgs})

    # Otherwise, expect a URL either in JSON payload or query param
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

    dom_msgs = extract_via_dom(html_text)
    json_msgs = extract_via_json(html_text) if not dom_msgs else []
    msgs = dom_msgs or json_msgs or []
    return jsonify({"count": len(msgs), "messages": msgs})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
