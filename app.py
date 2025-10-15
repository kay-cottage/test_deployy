import asyncio, re
from typing import Optional
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Error as PWError

app = Flask(__name__)

# ------------ 全局单例 & 并发限流 ------------
_play = None            # type: Optional[any]
_browser = None         # type: Optional[Browser]
_context = None         # type: Optional[BrowserContext]
_init_lock = asyncio.Lock()
_sem = asyncio.Semaphore(4)  # 控制并发页面数

# ------------ 辅助：重启浏览器栈 ------------
async def _restart_browser():
    global _play, _browser, _context
    try:
        if _context and not _context.is_closed():
            await _context.close()
    except Exception:
        pass
    try:
        if _browser and _browser.is_connected():
            await _browser.close()
    except Exception:
        pass
    try:
        if _play:
            await _play.stop()
    except Exception:
        pass
    _play = _browser = _context = None

# ------------ 确保浏览器可用（必要时重建）------------
async def _ensure_browser():
    global _play, _browser, _context
    if _browser and _context:
        # 检查是否仍然可用
        ok_browser = True
        try:
            ok_browser = _browser.is_connected()
        except Exception:
            ok_browser = False
        ok_context = _context and (not _context.is_closed())
        if ok_browser and ok_context:
            return

    async with _init_lock:
        # 双检，避免并发重复重建
        if _browser and _context:
            try:
                if _browser.is_connected() and not _context.is_closed():
                    return
            except Exception:
                pass

        # 彻底重启
        await _restart_browser()
        _play = await async_playwright().start()
        _browser = await _play.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        _context = await _browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
        )

# ------------ 抓取（含一次自动重试）------------
async def extract_chat_from_share(url: str):
    # 最多尝试两次：第一次失败则重启浏览器栈后重试一次
    last_err = None
    for attempt in (1, 2):
        try:
            await _ensure_browser()
            async with _sem:
                page: Page = await _context.new_page()
                page.set_default_timeout(15000)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    # 优先等待消息节点
                    try:
                        await page.wait_for_selector('[data-message-author-role]', timeout=3000)
                    except Exception:
                        try:
                            await page.wait_for_load_state("networkidle", timeout=6000)
                        except Exception:
                            pass
                    html = await page.content()
                    return parse_html(html)
                finally:
                    await page.close()
        except (PWError, AttributeError) as e:
            # 常见：BrowserContext None / page 失败
            last_err = e
            # 第一次失败则重启浏览器栈
            await _restart_browser()
        except Exception as e:
            last_err = e
            await _restart_browser()
    # 两次都失败
    raise last_err if last_err else RuntimeError("unknown playwright error")

def parse_html(html: str):
    soup = BeautifulSoup(html, 'html.parser')
    nodes = soup.select('[data-message-author-role]')
    if not nodes:
        nodes = soup.select('[data-message-id], [data-testid*="message" i]')

    msgs = []
    for el in nodes:
        role = (el.get('data-message-author-role') or '').strip().lower()
        if not role:
            classes = [c.lower() for c in (el.get('class') or [])]
            role = 'assistant' if any('assistant' in c for c in classes) else 'user'
        text = (el.get_text() or '').strip()
        if text:
            msgs.append({'role': role, 'text': text})

    cleaned, idx = [], 1
    for m in msgs:
        t = re.sub(r'[\s\u200b\u200c\u200d]+', ' ', m['text']).strip()
        if not t or re.match(r'^(复制链接|Copy link|Open in ChatGPT|Use GPT|登录|Log in)', t, re.I):
            continue
        cleaned.append({'idx': idx, 'role': m['role'], 'text': t})
        idx += 1
    return cleaned

@app.route('/extract')
async def extract_api():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'missing url'}), 400
    try:
        data = await extract_chat_from_share(url)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/__health')
def health():
    ok_browser = bool(_browser)
    ok_context = bool(_context)
    try:
        ok_browser = ok_browser and _browser.is_connected()
    except Exception:
        ok_browser = False
    try:
        ok_context = ok_context and (not _context.is_closed())
    except Exception:
        ok_context = False
    return jsonify({'browser': ok_browser, 'context': ok_context})

@app.route('/__shutdown', methods=['POST'])
async def shutdown():
    await _restart_browser()
    return jsonify({'ok': True})

if __name__ == '__main__':
    # ⚠ 关键：禁用 reloader，避免上下文丢失
    app.run(host='0.0.0.0', port=5006, debug=False, use_reloader=False)
