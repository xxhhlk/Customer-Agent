"""
拼多多商家前端接口探针（只读）
================================

目的
----
在不启动浏览器、不触碰线上会话的前提下，复用数据库里已保存的登录态
（Account.cookies），GET 商家后台页面与其 JS bundle，grep 出「图片空间 /
视频空间 / 素材中心」相关接口路径，为「助手从素材空间发图/发视频」提供线索。

为什么这样做
------------
- 直接匿名 GET 会被重定向到登录 SPA（返回的是 login/static/js/*）。
- 带上 cookie 才能拿到真正的页面 shell 与业务 chunk。
- 全部是 GET，且不写库、不改 cookie、不发消息 —— 对线上零副作用。

用法
----
    # 1) 看账号
    .venv\\Scripts\\python.exe scripts\\pdd_web_probe.py --list

    # 2) 拉取页面，打印 JS 资源清单
    .venv\\Scripts\\python.exe scripts\\pdd_web_probe.py --url https://mms.pinduoduo.com/chat-merchant/index.html

    # 3) 拉页面 + 下载全部 JS，grep 关键词，结果落盘 temp/web_probe/
    .venv\\Scripts\\python.exe scripts\\pdd_web_probe.py --dump-js

注意
----
输出里可能含店铺/买家信息，分享前自行检查。脚本不打印 cookie 内容。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import config as _app_config  # noqa: E402
from core.di_container import configure_standard_services  # noqa: E402

configure_standard_services(_app_config)

from database import db_manager  # noqa: E402
from Channel.pinduoduo.utils.base_request import BaseRequest  # noqa: E402

OUT_DIR = PROJECT_ROOT / "temp" / "web_probe"

# 页面默认清单：客服工作台 + 几个可能的素材/空间入口
DEFAULT_URLS = [
    "https://mms.pinduoduo.com/chat-merchant/index.html",
    "https://mms.pinduoduo.com/material-center/index.html",
    "https://mms.pinduoduo.com/material/index.html",
    "https://mms.pinduoduo.com/goods/material",
    "https://mms.pinduoduo.com/material/service",
]

# grep 关键词：命中即打印上下文
KEYWORDS = [
    "material", "素材", "图片空间", "视频空间", "space",
    "ims", "upload", "store_image", "get_upload_sign",
    "send_message", "sendImage", "send_image", "sendVideo", "send_video",
    "plateau/chat", "plateau/message", "vodka", "chat-material", "media",
]


def pick_account(selector: str | None) -> dict | None:
    rows = db_manager.get_all_accounts_flat() or []
    rows = [r for r in rows if r.get("channel_name") == "pinduoduo"]
    if not rows:
        print("[!] 数据库里没有拼多多账号")
        return None
    if not selector:
        return rows[0]
    for r in rows:
        if f"{r.get('shop_id')}:{r.get('user_id')}" == selector:
            return r
    print(f"[!] 未找到账号 {selector}")
    return None


def fetch_html(req: BaseRequest, url: str) -> str | None:
    """带 cookie GET 页面 HTML（不解析 JSON）。"""
    res = req.get(url, expect_json=False, timeout=25)
    if not res:
        return None
    return res.get("text")


def extract_assets(html: str) -> list[str]:
    """从 HTML 中提取所有 script/link 资源绝对 URL。"""
    out: list[str] = []
    for m in re.finditer(r'(?:src|href)\s*=\s*["\']([^"\']+\.(?:js|json))["\']', html):
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = "https://mms.pinduoduo.com" + u
        out.append(u)
    # 保持顺序去重
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def download_text(req: BaseRequest, url: str) -> str | None:
    res = req.get(url, expect_json=False, timeout=60)
    if not res:
        return None
    return res.get("text")


def grep_keywords(text: str, keywords: list[str], window: int = 90, max_hits: int = 60) -> list[str]:
    """在（可能已压缩的）JS 文本里找关键词并截取上下文片段。"""
    hits: list[str] = []
    for kw in keywords:
        for m in re.finditer(re.escape(kw), text, flags=re.IGNORECASE):
            s = max(0, m.start() - window)
            e = min(len(text), m.end() + window)
            snippet = text[s:e].replace("\n", " ")
            hits.append(f"[{kw}] ...{snippet}...")
            if len(hits) >= max_hits:
                return hits
    return hits


def main() -> int:
    parser = argparse.ArgumentParser(description="拼多多商家前端接口探针（只读）")
    parser.add_argument("--list", action="store_true", help="列出拼多多账号")
    parser.add_argument("--account", help="指定账号 shop_id:user_id")
    parser.add_argument("--url", action="append", help="要探测的页面 URL（可多次）")
    parser.add_argument("--dump-js", action="store_true", help="下载页面引用的 JS 并 grep 关键词")
    parser.add_argument("--max-js", type=int, default=40, help="最多下载的 JS 数量")
    args = parser.parse_args()

    if args.list:
        for r in (db_manager.get_all_accounts_flat() or []):
            if r.get("channel_name") == "pinduoduo":
                print(f"{r.get('shop_id')}:{r.get('user_id')}  {r.get('shop_name')}  {r.get('username')}")
        return 0

    account = pick_account(args.account)
    if not account:
        return 1
    print(f"[*] 账号: {account.get('shop_name')} ({account.get('shop_id')}:{account.get('user_id')})")

    req = BaseRequest(str(account.get("shop_id")), str(account.get("user_id")))
    print(f"[*] cookie 数量: {len(req.cookies)}")

    urls = args.url or DEFAULT_URLS
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_js: list[str] = []
    for url in urls:
        print(f"\n=== GET {url} ===")
        html = fetch_html(req, url)
        if not html:
            print("  [!] 无响应")
            continue
        is_login = "login/static/js" in html
        print(f"  len={len(html)}  login_page={is_login}")
        (OUT_DIR / (re.sub(r'[^a-zA-Z0-9]+', '_', url)[-60:] + ".html")).write_text(html, encoding="utf-8")
        assets = extract_assets(html)
        print(f"  资源 {len(assets)} 个:")
        for a in assets:
            print(f"    {a}")
        all_js.extend(a for a in assets if a.endswith(".js"))

    if args.dump_js and all_js:
        seen, js_urls = set(), []
        for u in all_js:
            if u not in seen:
                seen.add(u)
                js_urls.append(u)
        js_urls = js_urls[: args.max_js]
        print(f"\n[*] 下载 {len(js_urls)} 个 JS 并 grep 关键词 ...")
        report: list[str] = []
        for i, u in enumerate(js_urls, 1):
            text = download_text(req, u)
            if not text:
                print(f"  [{i}/{len(js_urls)}] 失败 {u[:100]}")
                continue
            name = re.sub(r'[^a-zA-Z0-9._-]+', '_', u.split("/")[-1])
            (OUT_DIR / name).write_text(text, encoding="utf-8")
            hits = grep_keywords(text, KEYWORDS)
            print(f"  [{i}/{len(js_urls)}] {u.split('/')[-1][:60]}  len={len(text)}  命中={len(hits)}")
            if hits:
                report.append(f"\n########## {u}\n" + "\n".join(hits))
        (OUT_DIR / "keyword_hits.txt").write_text("\n".join(report), encoding="utf-8")
        print(f"\n[*] 关键词命中已写入 {OUT_DIR / 'keyword_hits.txt'}")

    print(f"\n[*] 产物目录: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
