#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LOFTER 博客 -> GitHub Pages 全量镜像

设计要点：
1. 全量对齐：每次运行都重新枚举博客下的「所有」文章，按文章 ID 与 data.json 比对，执行 增 / 改 / 删。
2. 图文都从详情页取：列表页只用来拿文章链接，正文和高清图一律进内页提取。
3. 图片本地化：带上 Referer 下载原图，转存到本仓库 images/ 下，前端引用相对路径，彻底绕开防盗链。
4. 容错：单篇文章抓取失败不影响整轮；枚举不完整时「只增改、不删除」，避免误删。

用法：
    python main.py                # 正常同步
    python main.py --limit 3      # 只抓前 3 篇（调试用）
    python main.py --debug        # 把每篇文章的 HTML 存到 debug/ 便于排查
    python main.py --force-sync   # 即使枚举数变少也允许删除（慎用）

环境变量：
    LOFTER_BLOG      博客子域名，默认 xinjinjumin904472226641
    LOFTER_COOKIE    登录后的 Cookie 字符串（必须，否则会被登录墙挡住）
    MAX_POSTS        单轮最多抓多少篇，0 表示不限
    R2_*             可选，配置后图片改传 Cloudflare R2
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

import requests

# ---------------------------------------------------------------- 基础配置

BLOG_DOMAIN = os.environ.get("LOFTER_BLOG", "xinjinjumin904472226641").strip()
BASE_URL = f"https://{BLOG_DOMAIN}.lofter.com"
HOME_URL = f"{BASE_URL}/"
# 两个入口：老博客页（登录态下是服务端渲染的经典主题）、新版博客主页路由
ENTRY_URLS = [
    HOME_URL,
    f"https://www.lofter.com/front/blog/home-page/{BLOG_DOMAIN}",
]

COOKIE = os.environ.get("LOFTER_COOKIE", "").strip()
REFERER = "https://www.lofter.com/"
LOGIN_PATH = "/front/login"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data.json"
IMAGES_DIR = ROOT / "images"
DEBUG_DIR = ROOT / "debug"

MAX_POSTS = int(os.environ.get("MAX_POSTS") or 0)
TZ = timezone(timedelta(hours=8))

# 反爬节奏
DELAY_MIN, DELAY_MAX = 1.0, 3.0
SCROLL_ROUNDS = 40          # 最多滚动加载多少轮
STAGNANT_LIMIT = 3          # 连续几轮没有新链接就算到底
IMG_MIN_BYTES = 1024        # 小于这个字节的多半是占位图 / 1px 追踪像素


# ---------------------------------------------------------------- 工具函数

def log(msg: str) -> None:
    print(msg, flush=True)


def sleep_random(a: float = DELAY_MIN, b: float = DELAY_MAX) -> None:
    time.sleep(random.uniform(a, b))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def req_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {
        "User-Agent": UA,
        "Referer": REFERER,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if COOKIE:
        h["Cookie"] = COOKIE
    if extra:
        h.update(extra)
    return h


def parse_cookie_string(raw: str) -> list[dict[str, Any]]:
    """把 'a=1; b=2' 解析成 Playwright 的 cookie 列表。"""
    cookies = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": ".lofter.com",
            "path": "/",
            "secure": True,
        })
    return cookies


def safe_dirname(post_id: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", post_id)[:80] or "post"


def post_id_from_url(url: str) -> str:
    m = re.search(r"/post/([0-9A-Za-z_-]+)", url)
    return m.group(1) if m else ""


def normalize_image_url(url: str) -> str:
    """
    把缩略图 URL 还原成原图：
      - http -> https
      - 去掉网易图床的 imageView2 / imageMogr2 / thumbnail 之类缩放参数
      - 去掉 !thumb / !web 之类后缀
    """
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("data:"):
        return ""

    parts = urlsplit(url)
    query = parts.query
    if "imageView" in query or "imageMogr2" in query or "thumbnail" in query:
        query = ""
    path = re.sub(r"![a-zA-Z0-9_]+$", "", parts.path)
    return urlunsplit((parts.scheme or "https", parts.netloc, path, query, ""))


def ext_from_content_type(ct: str, url: str) -> str:
    ct = (ct or "").lower()
    if "png" in ct:
        return ".png"
    if "gif" in ct:
        return ".gif"
    if "webp" in ct:
        return ".webp"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:[?#]|$)", url.lower())
    return ".jpg" if m and m.group(1) in ("jpg", "jpeg") else (f".{m.group(1)}" if m else ".jpg")


def content_hash(title: str, text: str, images: list[str]) -> str:
    payload = "\x1f".join([title or "", text or "", "|".join(images)])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- R2 可选上传

def r2_enabled() -> bool:
    return all(os.environ.get(k) for k in (
        "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET", "R2_PUBLIC_BASE",
    ))


def r2_client():
    import boto3  # 只有启用 R2 时才需要 boto3
    account = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def r2_upload(local_path: Path, key: str) -> str:
    """上传到 R2，返回公开访问 URL；失败抛异常由调用方兜底。"""
    client = r2_client()
    ctype = "image/png" if local_path.suffix == ".png" else f"image/{local_path.suffix.lstrip('.')}"
    client.upload_file(
        str(local_path), os.environ["R2_BUCKET"], key,
        ExtraArgs={"ContentType": ctype, "CacheControl": "public, max-age=31536000"},
    )
    return os.environ["R2_PUBLIC_BASE"].rstrip("/") + "/" + key


def r2_delete(keys: Iterable[str]) -> None:
    if not r2_enabled():
        return
    try:
        client = r2_client()
        client.delete_objects(
            Bucket=os.environ["R2_BUCKET"],
            Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
        )
    except Exception as exc:  # 远端删除失败不该影响主流程
        log(f"  ! R2 删除失败：{exc}")


# ---------------------------------------------------------------- 图片下载

def download_image(url: str, post_id: str, index: int) -> str | None:
    """
    下载单张图片，返回写进 data.json 的路径（本地相对路径或 R2 URL）。
    失败返回 None，由调用方回退到原始 URL。
    """
    url = normalize_image_url(url)
    if not url:
        return None

    folder = IMAGES_DIR / safe_dirname(post_id)
    folder.mkdir(parents=True, exist_ok=True)

    for attempt in range(3):
        try:
            resp = requests.get(
                url,
                headers=req_headers({"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}),
                timeout=30,
                stream=True,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            ctype = resp.headers.get("Content-Type", "")
            if ctype and not ctype.startswith("image"):
                raise RuntimeError(f"非图片内容：{ctype}")

            data = resp.content
            if len(data) < IMG_MIN_BYTES:
                raise RuntimeError(f"图片过小：{len(data)} bytes")

            ext = ext_from_content_type(ctype, url)
            name = f"{index:02d}-{hashlib.sha1(url.encode()).hexdigest()[:12]}{ext}"
            path = folder / name
            path.write_bytes(data)

            if r2_enabled():
                key = f"{safe_dirname(post_id)}/{name}"
                try:
                    return r2_upload(path, key)
                except Exception as exc:
                    log(f"  ! R2 上传失败，改用本地：{exc}")

            return f"images/{safe_dirname(post_id)}/{name}"
        except Exception as exc:
            if attempt == 2:
                log(f"  ! 图片下载失败 {url} -> {exc}")
                return None
            sleep_random(1.0, 2.0)
    return None


# ---------------------------------------------------------------- 详情页解析

# 在浏览器里跑的提取脚本：先找「正文容器」，再清理噪声，返回结构化数据
EXTRACT_JS = r"""
() => {
  const textLen = el => (el.innerText || '').replace(/\s+/g, ' ').trim().length;
  const imgCount = el => el.querySelectorAll('img').length;
  const score = el => textLen(el) + imgCount(el) * 400;

  const NOISE = [
    '[class*="comment"]', '[class*="Comment"]',
    '[class*="share"]', '[class*="Share"]',
    '[class*="toolbar"]', '[class*="Toolbar"]',
    '[class*="action"]', '[class*="Action"]',
    '[class*="reward"]', '[class*="Reward"]',
    '[class*="recommend"]', '[class*="Recommend"]',
    '[class*="related"]', '[class*="Related"]',
    '[class*="qrcode"]', '[class*="Qrcode"]',
    '[class*="footer"]', '[class*="Footer"]',
    '[class*="advert"]', '[class*="Advert"]', '[class*="banner"]',
  ];

  // 1. 选正文容器
  const candidates = [];
  document.querySelectorAll(
    'article, main, .post, .post-content, .postContent, .content, .text, ' +
    '.article, .mcontent, .mcont, #post-content, .post-body, .postBody, .detail'
  ).forEach(el => {
    const s = score(el);
    if (s > 0) candidates.push([el, s]);
  });
  candidates.sort((a, b) => b[1] - a[1]);

  let container = candidates.length ? candidates[0][0] : document.body;
  let bestScore = candidates.length ? candidates[0][1] : score(document.body);

  // 2. 往下钻到最紧凑的那个容器
  for (let depth = 0; depth < 8; depth++) {
    let picked = null;
    for (const child of Array.from(container.children || [])) {
      const s = score(child);
      if (s >= bestScore * 0.85) { picked = child; break; }
    }
    if (!picked) break;
    container = picked;
    bestScore = score(picked);
  }

  // 3. 清掉容器内部的噪声节点
  NOISE.forEach(sel => {
    try { container.querySelectorAll(sel).forEach(n => n.remove()); } catch (e) {}
  });
  container.querySelectorAll('script, style, noscript, iframe, form, button, svg, nav, aside').forEach(n => n.remove());

  // 4. 标题
  const metaOg = document.querySelector('meta[property="og:title"]');
  let title = '';
  const h1 = document.querySelector('h1');
  if (h1 && textLen(h1) > 0 && textLen(h1) < 200) title = h1.innerText.trim();
  if (!title && metaOg) title = (metaOg.getAttribute('content') || '').trim();
  if (!title) title = (document.title || '').replace(/[_\-|]\s*LOFTER.*$/i, '').trim();

  // 5. 发布时间
  let published = '';
  const metaTime = document.querySelector('meta[property="article:published_time"]');
  if (metaTime) published = metaTime.getAttribute('content') || '';
  if (!published) {
    const t = document.querySelector('time[datetime]');
    if (t) published = t.getAttribute('datetime') || t.innerText || '';
  }
  if (!published) {
    for (const s of Array.from(document.querySelectorAll('script[type="application/ld+json"]'))) {
      try {
        const j = JSON.parse(s.textContent);
        const arr = Array.isArray(j) ? j : [j];
        for (const o of arr) {
          if (o && (o.datePublished || o.dateCreated)) { published = o.datePublished || o.dateCreated; break; }
        }
      } catch (e) {}
      if (published) break;
    }
  }
  if (!published) {
    const body = document.body.innerText || '';
    const m = body.match(/\d{4}[-年.]\s?\d{1,2}[-月.]\s?\d{1,2}日?[^\d]{0,4}\d{1,2}:\d{2}/);
    if (m) published = m[0];
  }

  // 6. 图片（含懒加载属性），并过滤头像 / 表情 / 图标
  const images = [];
  container.querySelectorAll('img').forEach(img => {
    const raw = img.getAttribute('data-original') || img.getAttribute('data-src') ||
                img.getAttribute('data-lazy-src') || img.currentSrc || img.getAttribute('src') || '';
    if (!raw || raw.startsWith('data:')) return;
    const cls = (img.className || '') + ' ' + (img.parentElement ? img.parentElement.className : '');
    if (/avatar|emoji|icon|logo|qrcode|badge|emoticon/i.test(cls)) return;
    const w = img.naturalWidth || parseInt(img.getAttribute('width') || '0', 10) || 0;
    const h = img.naturalHeight || parseInt(img.getAttribute('height') || '0', 10) || 0;
    if ((w && w < 120) || (h && h < 120)) return;
    images.push(raw);
  });

  return {
    url: location.href,
    title: title,
    published: published,
    text: (container.innerText || '').replace(/[\u00a0\u3000]/g, ' ').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim(),
    images: images
  };
}
"""


def open_page(page, url: str, wait_ms: int = 2500):
    """打开页面并等渲染，顺带做随机延时。"""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(wait_ms)
    if LOGIN_PATH in page.url:
        raise LoginRequired(url)
    sleep_random(1.0, 3.0)


class LoginRequired(RuntimeError):
    pass


def extract_post(page, url: str, debug: bool) -> dict[str, Any] | None:
    """进内页提取一篇的标题 / 时间 / 正文 / 图片。"""
    raw = None
    for attempt in range(2):
        try:
            open_page(page, url, wait_ms=random.randint(1800, 3200) + attempt * 2000)
            raw = page.evaluate(EXTRACT_JS)
        except LoginRequired:
            raise
        except Exception as exc:
            log(f"  ! 解析失败 {url}（第 {attempt + 1} 次）：{exc}")
            raw = None
            continue

        text = (raw.get("text") or "").strip()
        if len(text) >= 2 or (raw.get("images") or []):
            break

        log(f"  ~ 正文为空，重试一次：{url}")
        sleep_random(1.5, 2.5)

    if raw is None:
        return None

    if debug:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        pid = post_id_from_url(url) or "unknown"
        (DEBUG_DIR / f"{pid}.html").write_text(page.content(), encoding="utf-8")

    text = (raw.get("text") or "").strip()
    images = [normalize_image_url(u) for u in (raw.get("images") or [])]
    images = [u for u in images if u]

    if len(text) < 2 and not images:
        log(f"  ! 正文为空，跳过 {url}")
        return None

    return {
        "id": post_id_from_url(url),
        "url": raw.get("url") or url,
        "title": (raw.get("title") or "").strip(),
        "published_raw": (raw.get("published") or "").strip(),
        "text": text,
        "source_images": images,
    }


def parse_published(raw: str) -> str:
    """把各种时间写法尽量归一成 ISO；失败就原样返回。"""
    if not raw:
        return ""
    s = raw.strip().replace("年", "-").replace("月", "-").replace("日", " ")
    s = s.replace("/", "-").replace(".", "-")
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mm = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        try:
            return datetime(y, mo, d, hh, mm, ss, tzinfo=TZ).isoformat(timespec="seconds")
        except ValueError:
            return raw
    return raw


# ---------------------------------------------------------------- 列表枚举

def _scroll_collect(page, max_posts: int) -> dict[str, str]:
    """在当前页面上滚动加载，收集所有 /post/ 链接。"""
    seen: dict[str, str] = {}
    stagnant = 0

    for round_no in range(SCROLL_ROUNDS):
        hrefs = page.eval_on_selector_all(
            "a[href*='/post/']",
            "els => els.map(e => e.href)",
        ) or []
        new = 0
        for href in hrefs:
            pid = post_id_from_url(href)
            if pid and pid not in seen:
                seen[pid] = href.split("?")[0]
                new += 1

        log(f"  第 {round_no + 1} 轮：新增 {new}，累计 {len(seen)}")

        if max_posts and len(seen) >= max_posts:
            break
        if new == 0:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= STAGNANT_LIMIT:
            break

        # 触发懒加载：滚到底 + 点可能的“更多”入口（没有就算了）
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            page.get_by_text(re.compile(r"^(查看更多|加载更多|更多文章)$")).first.click(timeout=1500)
        except Exception:
            pass
        sleep_random(1.2, 2.6)

    return seen


def collect_post_links(page, max_posts: int) -> list[str]:
    """
    依次尝试多个入口地址，谁先能匿名/凭 Cookie 渲染出文章列表就用谁。
    老博客页（经典主题）是服务端渲染的，链接直接就在 HTML 里；
    新版主页路由是前端渲染，需要等它把列表拉出来。
    """
    for entry in ENTRY_URLS:
        try:
            open_page(page, entry, wait_ms=3500)
        except LoginRequired:
            log(f"  · 入口被拦回登录页：{entry}")
            continue

        body_text = page.inner_text("body")[:4000]
        if "还没有发布过文章" in body_text:
            log(f"  · 这个入口对访客隐藏了文章（显示 0 篇）：{entry}")
            continue

        seen = _scroll_collect(page, max_posts)
        if seen:
            log(f"  · 命中入口：{entry}")
            links = list(seen.values())
            return links[:max_posts] if max_posts else links

    raise LoginRequired("所有入口都拿不到文章列表")


# ---------------------------------------------------------------- 主流程

def load_existing() -> list[dict[str, Any]]:
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        posts = data.get("posts") or []
        return posts if isinstance(posts, list) else []
    except Exception as exc:
        log(f"! data.json 读取失败，按全新处理：{exc}")
        return []


def drop_post_images(post: dict[str, Any]) -> None:
    """删除某篇文章的本地图片目录 / R2 对象。"""
    folder = IMAGES_DIR / safe_dirname(post.get("id", ""))
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)

    r2_keys = []
    for u in post.get("images") or []:
        base = os.environ.get("R2_PUBLIC_BASE", "").rstrip("/")
        if base and isinstance(u, str) and u.startswith(base):
            r2_keys.append(u[len(base):].lstrip("/"))
    if r2_keys:
        r2_delete(r2_keys)


def attach_images(post: dict[str, Any]) -> list[dict[str, Any]]:
    """
    下载文章原图，返回 images 记录：
      {"local": "images/xx/01-xxx.jpg", "src": "https://原图地址"}
    下载失败的保留原始地址，保证页面不裂。
    """
    records = []
    for i, src in enumerate(post.get("source_images") or []):
        local = download_image(src, post["id"], i)
        records.append({"local": local or src, "src": src})
        sleep_random(0.6, 1.8)
    return records


def merge_posts(old_posts: list[dict[str, Any]],
                fresh_posts: list[dict[str, Any]],
                full_list: bool,
                failed_ids: set[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    按文章 ID 做增 / 改 / 删。
    full_list=True 表示本轮确实枚举到了完整列表，才允许删除；
    failed_ids 里的文章（本轮抓取报错）无论哪种情况都保留旧版本。
    """
    failed_ids = failed_ids or set()
    stats = {"added": 0, "updated": 0, "unchanged": 0, "removed": 0, "kept": 0}
    old_by_id = {p["id"]: p for p in old_posts if p.get("id")}

    result: list[dict[str, Any]] = []
    handled: set[str] = set()

    for fresh in fresh_posts:
        pid = fresh["id"]
        handled.add(pid)
        old = old_by_id.get(pid)

        if old and old.get("content_hash") == fresh.get("content_hash"):
            stats["unchanged"] += 1
            result.append(old)
            continue

        if old:
            drop_post_images(old)
            stats["updated"] += 1
        else:
            stats["added"] += 1
        result.append(fresh)

    for pid, old in old_by_id.items():
        if pid in handled:
            continue
        if pid in failed_ids or not full_list:
            # 抓取失败 / 列表可能不完整：一律保留，绝不用一次网络抖动换一次误删
            stats["kept"] += 1
            result.append(old)
        else:
            drop_post_images(old)
            stats["removed"] += 1

    if stats["kept"]:
        log(f"  ~ 保留 {stats['kept']} 篇旧文章（本轮未成功抓到，或列表不完整）")

    return result, stats


def prune_empty_dirs() -> None:
    if not IMAGES_DIR.exists():
        return
    for d in IMAGES_DIR.iterdir():
        if d.is_dir() and not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)


def launch_browser(pw):
    """
    优先用系统 Chrome（本地调试方便），没有就退回 Playwright 自带 Chromium（CI 用这个）。
    """
    opts = {
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    last_err = None
    for extra in ({"channel": "chrome"}, {}):
        try:
            return pw.chromium.launch(**opts, **extra)
        except Exception as exc:
            last_err = exc
    raise RuntimeError(
        "启动浏览器失败，请先执行：python -m playwright install chromium\n"
        f"底层错误：{last_err}"
    )


def run(args) -> int:
    if not COOKIE:
        log("!! 未设置 LOFTER_COOKIE。LOFTER 已对未登录访客整体加登录墙，")
        log("   匿名抓取拿不到任何内容。请在仓库 Secrets 里配置 LOFTER_COOKIE 后重跑。")
        return 2

    old_posts = load_existing()
    log(f"本地已有 {len(old_posts)} 篇")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("!! 缺少 playwright，请先 pip install -r requirements.txt && python -m playwright install chromium")
        return 2

    fresh_posts: list[dict[str, Any]] = []
    failed_ids: set[str] = set()
    full_list = False

    with sync_playwright() as pw:
        try:
            browser = launch_browser(pw)
        except Exception as exc:
            log(f"!! {exc}")
            return 2

        ctx = browser.new_context(
            user_agent=UA,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1440, "height": 960},
        )
        ctx.add_cookies(parse_cookie_string(COOKIE))
        ctx.set_default_timeout(40000)
        page = ctx.new_page()

        try:
            log("枚举文章列表…")
            links = collect_post_links(page, MAX_POSTS)
            log(f"共发现 {len(links)} 篇文章链接")

            if not links:
                log("!! 一篇文章都没枚举到：Cookie 可能失效，或页面结构变了（加 --debug 看 HTML）")
                return 3

            # 兜底判定：枚举数 >= 本地数，才认为这轮是完整列表，允许删除
            if args.force_sync:
                full_list = True             # 手动强制对齐
            elif MAX_POSTS:
                full_list = False            # 限制了数量，绝不能判删
            else:
                full_list = len(links) >= len(old_posts)
                if not full_list:
                    log(f"! 枚举数 {len(links)} < 本地 {len(old_posts)}，本轮不执行删除")

            for idx, url in enumerate(links, 1):
                log(f"[{idx}/{len(links)}] {url}")
                try:
                    item = extract_post(page, url, args.debug)
                except LoginRequired:
                    raise
                except Exception as exc:
                    log(f"  ! 抓取异常 {url}：{exc}")
                    item = None
                if not item:
                    failed_ids.add(post_id_from_url(url))
                    continue

                images = attach_images(item)
                item["images"] = [r["local"] for r in images]
                item["image_sources"] = [r["src"] for r in images]
                item["published_at"] = parse_published(item.get("published_raw", ""))
                item["fetched_at"] = now_iso()
                item["content_hash"] = content_hash(
                    item["title"], item["text"], item["image_sources"]
                )
                item["cover"] = images[0]["local"] if images else ""
                fresh_posts.append(item)

        except LoginRequired as exc:
            log(f"!! 拿不到文章列表（{exc}）。两个可能：")
            log("   1) LOFTER_COOKIE 已失效 —— 重新获取后更新 Secret；")
            log("   2) 这台机器的网络出口被 LOFTER 判定为未登录/海外，换用国内的出口再试。")
            return 3
        except Exception:
            log("!! 运行异常：")
            traceback.print_exc()
            if not fresh_posts:
                return 1
        finally:
            ctx.close()
            browser.close()

    merged, stats = merge_posts(old_posts, fresh_posts, full_list, failed_ids)

    merged.sort(key=lambda p: (p.get("published_at") or "", p.get("id") or ""), reverse=True)

    payload = {
        "blog": {
            "domain": BLOG_DOMAIN,
            "home": HOME_URL,
        },
        "updated_at": now_iso(),
        "count": len(merged),
        "posts": merged,
    }
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    prune_empty_dirs()

    log("")
    log(f"完成：共 {len(merged)} 篇 | 新增 {stats['added']} / 更新 {stats['updated']} "
        f"/ 未变 {stats['unchanged']} / 删除 {stats['removed']}")
    log(f"data.json -> {DATA_FILE}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="LOFTER 博客全量镜像")
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 篇（调试）")
    ap.add_argument("--debug", action="store_true", help="保存每篇 HTML 到 debug/")
    ap.add_argument("--force-sync", action="store_true", help="枚举不完整也允许删除（慎用）")
    args = ap.parse_args()

    global MAX_POSTS
    if args.limit:
        MAX_POSTS = args.limit

    sys.exit(run(args))


if __name__ == "__main__":
    main()
