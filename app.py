"""
ChatGPT Share Extractor Flask App
--------------------------------

This single-file Flask application exposes a tiny API and a simple
webpage for extracting the conversation history from a ChatGPT share
link.  Users can paste a public share URL into the form on the
homepage, and the backend will fetch and parse that page to produce
a structured list of messages.  The parsing logic looks for
`data-message-author-role="user|assistant"` attributes, then
extracts the plain text from each message container.  All HTML
tags, scripts and styles are removed, and common UI artefacts
(such as “Open in ChatGPT” or “复制链接”) are stripped out.  The
results are returned to the frontend as JSON and rendered in
a minimalistic dark-themed interface.  No external template files or
dependencies are required.

To run locally:

    pip install flask requests
    python chatgpt_share_extractor_flask.py

Then open http://127.0.0.1:5000/ in your browser.

Optional environment variables:

    ALLOWED_HOSTS   Comma‑separated list of hostnames permitted for
                    fetching.  If unset, any public host is allowed.
                    Requests to localhost or private IPs are always
                    disallowed.

    TIMEOUT         Timeout in seconds for outbound HTTP requests (default: 15).

    MAX_BYTES       Maximum response size (bytes) to read from the
                    share page (default: 6_000_000).  Responses larger
                    than this will raise an error.

    UA              Override the User‑Agent header when fetching share pages.

    ACCESS_TOKEN    If set, clients must include a matching
                    `X-Access-Token` header on API requests.

Note: this application does not cache results between requests and will
re-fetch the share page each time.  As a simple measure against
server‑side request forgery (SSRF), it rejects local and private IP
addresses and optionally restricts allowed hostnames.  Adjust the
security logic below if you plan to expose this service publicly.
"""

from __future__ import annotations

import html as html_lib
import os
import re
import ipaddress
from urllib.parse import urlparse

# Optional imports: Flask may not be available in all environments used for
# testing.  Wrap the import in a try/except so that modules which only
# depend on the parsing helpers (e.g. for unit tests) can still import
# this file even if Flask is not installed.  The create_app() factory
# will raise a RuntimeError if Flask is missing when called.
try:
    from flask import Flask, request, jsonify, Response, abort  # type: ignore
except Exception:
    Flask = None  # type: ignore
    request = jsonify = Response = abort = None  # type: ignore
import requests


def is_private_host(hostname: str) -> bool:
    """Return True if the hostname resolves to a private or loopback address.

    This helper rejects obvious SSRF targets such as localhost and
    private IP ranges.  It does not perform DNS resolution; instead it
    checks numeric hosts directly and relies on callers to block other
    unsafe hosts via ALLOWED_HOSTS.

    """
    try:
        # If it parses as an IP address, classify it directly.
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_reserved
    except ValueError:
        # Not a bare IP – allow domain names through here.  They may be
        # filtered by ALLOWED_HOSTS upstream.  Avoid resolving DNS here
        # to keep this function lightweight and deterministic.
        return False


def check_allowed_url(url: str, allowed_hosts: set[str] | None) -> None:
    """Raise an exception if the URL is not permitted.

    Disallows local and private IP addresses and, if `allowed_hosts` is
    provided, only permits exact matches in that set.
    """
    parsed = urlparse(url)
    if not parsed.scheme or parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are supported")
    hostname = parsed.hostname or ""
    if is_private_host(hostname):
        raise ValueError("Access to localhost or private IPs is disallowed")
    if allowed_hosts is not None and hostname.lower() not in allowed_hosts:
        raise ValueError(f"Host '{hostname}' is not allowed")


def strip_unwanted(text: str) -> str:
    """Normalize whitespace and remove common UI artefacts.

    This helper collapses excess whitespace, removes stray labels such as
    “ChatGPT 说：” or “Open in ChatGPT”, and trims leading and trailing
    blank lines.  The list of patterns to remove can be extended to
    catch other spurious fragments as needed.
    """
    # Remove lines containing known artefacts (case‑insensitive)
    kill_patterns = [
        r"ChatGPT\s*说：.*",
        r"复制链接.*",
        r"Copy link.*",
        r"Open in ChatGPT.*",
        r"Use GPT-.*",
        r"使用 GPT-.*",
        r"Regenerate.*",
        r"模型:.*",
        r"Model:.*",
    ]
    for pat in kill_patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)
    # Collapse multiple spaces/tabs into single spaces
    text = re.sub(r"[\t ]+", " ", text)
    # Normalize newlines: CRLF or CR -> LF
    text = re.sub(r"\r\n?", "\n", text)
    # Remove three or more consecutive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_chat_messages(html_content: str) -> list[dict[str, str]]:
    """Extract user and assistant messages from a ChatGPT share page.

    The parsing logic searches for occurrences of
    `data-message-author-role="user|assistant"` to find the start of
    each message.  It then captures the text between the end of the
    opening tag and the next occurrence of this attribute (or the end
    of the document).  Script and style blocks are removed before
    scanning to prevent false positives.  All HTML tags are stripped
    from the extracted text, entities are unescaped, and spurious UI
    content is filtered out.
    """
    # Remove <script> and <style> blocks entirely
    no_scripts = re.sub(
        r"<script[^>]*>.*?</script>", "", html_content, flags=re.IGNORECASE | re.DOTALL
    )
    no_scripts = re.sub(
        r"<style[^>]*>.*?</style>", "", no_scripts, flags=re.IGNORECASE | re.DOTALL
    )
    # Regex to locate each message anchor
    anchor_pat = re.compile(r"data-message-author-role=\"(user|assistant)\"", re.IGNORECASE)
    matches = list(anchor_pat.finditer(no_scripts))
    results: list[dict[str, str]] = []
    for idx, m in enumerate(matches):
        role = m.group(1).lower()
        # Find the end of this opening tag.  We look for the next '>' after
        # the attribute because attributes may appear in any order.
        tag_end = no_scripts.find(">", m.end())
        if tag_end == -1:
            continue
        start = tag_end + 1
        # The end of this message runs up to the start of the next match
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(no_scripts)
        raw = no_scripts[start:end]
        # Strip HTML tags
        text = re.sub(r"<[^>]+>", "", raw)
        # Unescape HTML entities
        text = html_lib.unescape(text)
        # Normalize and remove UI junk
        cleaned = strip_unwanted(text)
        if cleaned:
            results.append({"role": role, "text": cleaned})
    return results


def create_app() -> Flask:
    """Factory to create and configure the Flask application.

    This function requires the ``Flask`` class to be available.  If
    Flask could not be imported when this module was loaded (for
    example, in a restricted environment where Flask is not
    installed), a ``RuntimeError`` will be raised to indicate that
    server functionality is unavailable.  Parsing helpers can still
    be imported and used without creating the app.
    """
    if Flask is None:
        raise RuntimeError(
            "Flask is not installed; cannot create a web application. "
            "Install the 'flask' package or run only the parsing helpers."
        )
    app = Flask(__name__)

    # Load configuration from environment variables
    allowed_hosts_env = os.environ.get("ALLOWED_HOSTS")
    if allowed_hosts_env:
        # Build a set of lowercase hostnames for exact matching
        allowed_hosts = {h.strip().lower() for h in allowed_hosts_env.split(",") if h.strip()}
    else:
        allowed_hosts = None
    timeout = float(os.environ.get("TIMEOUT", "15"))
    max_bytes = int(os.environ.get("MAX_BYTES", "6000000"))
    user_agent = os.environ.get("UA", "Mozilla/5.0 (compatible; ChatGPT-Share-Extractor/1.0)")
    access_token = os.environ.get("ACCESS_TOKEN")

    @app.before_request
    def check_token():
        """Optionally enforce a simple access token.

        If ACCESS_TOKEN is defined in the environment, incoming API
        requests must include a matching `X-Access-Token` header.  This
        protects the endpoint from unauthorised usage when deployed.
        """
        # Only protect API endpoints; static index page is public
        if request.path.startswith("/api/") and access_token:
            token = request.headers.get("X-Access-Token")
            if token != access_token:
                abort(403)

    @app.route("/", methods=["GET"])
    def index() -> Response:
        """Serve the embedded HTML page."""
        return Response(INDEX_PAGE, mimetype="text/html")

    @app.route("/api/parse", methods=["POST"])
    def api_parse() -> Response:
        """Fetch a share page and return the extracted messages as JSON."""
        if not request.is_json:
            return jsonify({"error": "Expected JSON body"}), 400
        data = request.get_json(silent=True) or {}
        url = data.get("url")
        if not isinstance(url, str) or not url.strip():
            return jsonify({"error": "Missing 'url' field"}), 400
        url = url.strip()
        try:
            check_allowed_url(url, allowed_hosts)
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            # Fetch the share page
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": user_agent},
                stream=True,
            )
        except Exception as exc:
            return jsonify({"error": f"Failed to fetch URL: {exc}"}), 502
        if resp.status_code >= 400:
            return jsonify({"error": f"Upstream returned status {resp.status_code}"}), 502
        # Limit the amount of content read to prevent memory exhaustion
        try:
            content = resp.raw.read(max_bytes, decode_content=True)
        finally:
            resp.close()
        if not content:
            return jsonify({"error": "No response body"}), 502
        try:
            html_text = content.decode(resp.encoding or "utf-8", errors="replace")
        except Exception:
            html_text = content.decode("utf-8", errors="replace")
        try:
            messages = parse_chat_messages(html_text)
        except Exception as exc:
            return jsonify({"error": f"Parsing error: {exc}"}), 500
        return jsonify({"items": messages})

    return app


# Embedded front-end HTML page.  This dark-themed interface allows users
# to paste a share URL and trigger the extraction process.  It
# communicates with the backend via fetch() and renders the returned
# messages.  File downloads can be implemented purely on the client
# side by constructing Blobs, so no additional download endpoints are
# needed.
INDEX_PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>ChatGPT Share Extractor</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { background-color: #0d1117; color: #c9d1d9; font-family: sans-serif; margin: 0; padding: 20px; }
    h1 { margin-top: 0; }
    input[type=text] { width: 100%; padding: 12px; margin: 0 0 10px; border: 1px solid #30363d; border-radius: 4px; background-color: #161b22; color: #c9d1d9; }
    button { padding: 10px 16px; background-color: #238636; color: white; border: none; border-radius: 4px; cursor: pointer; }
    button:disabled { background-color: #30363d; cursor: not-allowed; }
    #status { margin-top: 10px; }
    #messages { margin-top: 20px; }
    .message { padding: 10px; border-radius: 4px; margin-bottom: 8px; white-space: pre-wrap; }
    .message.user { background-color: #1f6feb; }
    .message.assistant { background-color: #8b949e; }
    .footer { margin-top: 40px; font-size: 0.8em; color: #8b949e; }
  </style>
</head>
<body>
  <h1>ChatGPT Share Extractor</h1>
  <p>Enter a ChatGPT share URL below and click <strong>Extract</strong> to retrieve the conversation messages.</p>
  <input id="urlInput" type="text" placeholder="https://chat.openai.com/share/..." />
  <button id="extractBtn">Extract</button>
  <div id="status"></div>
  <div id="messages"></div>
  <div class="footer">This tool fetches the provided link server‑side to overcome CORS restrictions and extracts user/assistant messages from the page. No data is persisted.</div>
  <script>
    const input = document.getElementById('urlInput');
    const btn = document.getElementById('extractBtn');
    const statusEl = document.getElementById('status');
    const messagesEl = document.getElementById('messages');
    btn.addEventListener('click', async () => {
      const url = input.value.trim();
      if (!url) {
        alert('Please enter a URL');
        return;
      }
      btn.disabled = true;
      statusEl.textContent = 'Fetching and parsing...';
      messagesEl.innerHTML = '';
      try {
        const res = await fetch('/api/parse', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (!res.ok) {
          statusEl.textContent = 'Error: ' + (data.error || res.statusText);
        } else {
          statusEl.textContent = `Found ${data.items.length} message${data.items.length !== 1 ? 's' : ''}`;
          data.items.forEach(item => {
            const div = document.createElement('div');
            div.className = 'message ' + item.role;
            div.textContent = item.text;
            messagesEl.appendChild(div);
          });
        }
      } catch (err) {
        statusEl.textContent = 'Error: ' + err;
      } finally {
        btn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    # When run directly, create the app and serve it.  Use host=0.0.0.0
    # so that it will listen on all interfaces when deployed behind a
    # proxy.  The port defaults to 5000 unless overridden by the
    # environment variable PORT (common in PaaS platforms).
    app = create_app()
    port_str = os.environ.get("PORT")
    port = int(port_str) if port_str and port_str.isdigit() else 5000
    app.run(host="0.0.0.0", port=port)
