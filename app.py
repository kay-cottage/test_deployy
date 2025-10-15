import asyncio
import re
import subprocess
import sys
from typing import Optional

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Error as PWError,
)

app = Flask(__name__)

# ---------------- 全局：Playwright 单例 & 并发限流 ----------------
_play = None            # type: Optional[any]
_browser = None         # type: Optional[Browser]
_context = None         # type: Optional[BrowserContext]
_init_lock = asyncio.Lock()
_sem = asyncio.Semaphore(4)   # 同时最多处理 4 个请求；按需要调整

# ---------------- 工具：优雅重启浏览器栈 ----------------
async def _restart_browser():
    """关闭 context / browser / playwright，清空全局句柄。"""
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

# ---------------- 工具：在容器内安装浏览器二进制 ----------------
def _install_browsers_once_blocking():
    """阻塞方式安装 Playwright 浏览器二进制（Linux 推荐带 --with-deps）。"""
    subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--with-deps"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

# ---------------- 确保浏览器/上下文就绪（必要时安装并重建） ----------------
async def _ensure_browser():
    """
    确保 _browser/_context 可用；若断开或缺失：
    1) 尝试直接启动；
    2) 若因“Executable doesn't exist”失败，自动安装浏览器后重试一次。
    """
    global _play, _browser, _context

    # 已经有对象则快速健康检查
    if _browser and _context:
        try:
            if _browser.is_connected() and not _context.is_closed():
                return
        except Exception:
            pass

    async with _init_lock:
        # 双重检查，避免并发重复初始化
        if _browser and _context:
            try:
                if _browser.is_connected() and not _context.is_closed():
                    return
            except Exception:
                pass

        # 最多两次：第一次失败则尝试安装浏览器后重试
        for attempt in (1, 2):
            try:
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
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                )
                return  # 启动成功
            except PWError as e:
                msg = str(e)
                # 命中“缺少可执行文件”，在下一次尝试前安装浏览器
                if "Executable doesn't exist" in msg and attempt == 1:
                    # 安装发生在线程池，避免阻塞事件循环
                    await asyncio.to_thread(_install_browsers_once_blocking)
                    continue
                raise

# ---------------- 业务：抓取页面并解析 ----------------
async def extract_chat_from_share(url: str):
    """
    打开分享页，等待关键信号或网络空闲，提取 HTML 并解析为消息列表。
    内置一次失败自愈：若 context/page 报错，会整体重启浏览器栈再试一次。
    """
    last_err = None
    for attempt in (1, 2):
        try:
            await _ensure_browser()
            async with _sem:
                page: Page = await _context.new_page()
                page.set_default_timeout(15000)  # 单次等待动作 15s
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    # 优先等待消息节点，失败则退回 networkidle
                    try:
                        await page.wait_for_selector('[data-message-author-role]', timeout=6000)
                    except Exception:
                        try:
                            await page.wait_for_load_state("networkidle", timeout=6000)
                        except Exception:
                            pass
                    html = await page.content()
                    return parse_html(html)
                finally:
                    await page.close()
        except (PWError, AttributeError, RuntimeError) as e:
            last_err = e
            # 常见：context/page 失联或被杀；重启后重试一次
            await _restart_browser()
        except Exception as e:
            last_err = e
            await _restart_browser()
    # 两次都失败
    raise last_err if last_err else RuntimeError("unknown playwright error")

def parse_html(html: str):
    """
    DOM 解析策略：
    1) 首选具有 data-message-author-role 的节点；
    2) 回退 data-message-id / data-testid*="message"；
    3) 清理 UI 残留（如 Copy link / Open in ChatGPT / 登录提示等）；
    """
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

    cleaned = []
    idx = 1
    for m in msgs:
        # 清理多余空白和零宽字符
        t = re.sub(r'[\s\u200b\u200c\u200d]+', ' ', m['text']).strip()
        # 过滤分享页 UI 残留
        if not t or re.match(r'^(复制链接|Copy link|Open in ChatGPT|Use GPT|登录|Log in)', t, re.I):
            continue
        cleaned.append({'idx': idx, 'role': m['role'], 'text': t})
        idx += 1

    return cleaned

# ---------------- 路由 ----------------
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
    """用于外部探针的健康检查：browser/context 是否可用。"""
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
    """调试/维护用：主动关闭并清空 Playwright 单例。"""
    await _restart_browser()
    return jsonify({'ok': True})

# ---------------- 本地开发入口 ----------------
if __name__ == '__main__':
    # ⚠ 生产环境请使用 gunicorn（见下）。本地调试时请关闭 reloader。
    app.run(host='0.0.0.0', port=5006, debug=False, use_reloader=False)
