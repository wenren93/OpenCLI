#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
即刻动态流 HTML 报告生成器

调用 `opencli jike feed -f json --window background --keep-tab false` 拉取即刻首页动态，
按「近 N 小时」过滤，保留 作者 / 时间 / 内容 / 链接 四项核心字段，生成一份可读的 HTML 报告。

用法:
    python3 jike_feed_report.py                 # 默认近 4 小时
    python3 jike_feed_report.py --hours 6       # 近 6 小时
    python3 jike_feed_report.py --limit 200     # 拉取条数（默认 100）
    python3 jike_feed_report.py --output /path  # 自定义输出目录

依赖:
    - opencli (需已登录即刻: `opencli jike login`)
    - Python 3.8+（仅标准库）
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

# ──────────────────────────── 常量 ────────────────────────────

DEFAULT_HOURS = 4
DEFAULT_LIMIT = 100          # 单次拉取条数，确保覆盖时间窗口
DEFAULT_TIMEOUT = 300        # opencli 子进程超时（秒），浏览器命令较慢
BEIJING_TZ = timezone(timedelta(hours=8))   # 展示用北京时区
UTC_TZ = timezone.utc

# 即刻品牌黄
ACCENT = "#FFE411"
ACCENT_DARK = "#F5C518"

# ──────────────────────────── 数据获取 ────────────────────────────

def fetch_jike_feed(limit: int, timeout: int = DEFAULT_TIMEOUT) -> list:
    """调用 opencli jike feed 拉取动态流，返回原始字典列表。"""
    cmd = [
        "opencli", "jike", "feed",
        "-f", "json",
        "--limit", str(limit),
        "--window", "background",
        "--keep-tab", "false",
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        sys.exit("错误: 未找到 opencli 命令，请先安装并确保在 PATH 中。")
    except subprocess.TimeoutExpired:
        sys.exit(f"错误: opencli 执行超时（>{timeout}s）。可尝试重新登录: opencli jike login")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        sys.exit(
            f"错误: opencli 执行失败 (exit {result.returncode})。\n"
            f"stderr:\n{stderr}\n"
            f"提示: 若提示需要登录，请运行 `opencli jike login`。"
        )

    stdout = result.stdout.strip()
    if not stdout:
        return []

    # stdout 应为纯 JSON；防御性地截取到首个 [ 或 {
    start = min(
        (i for i in (stdout.find("["), stdout.find("{")) if i != -1),
        default=0,
    )
    try:
        data = json.loads(stdout[start:])
    except json.JSONDecodeError as e:
        sys.exit(f"错误: 解析 opencli JSON 输出失败: {e}\n原始输出前 500 字:\n{stdout[:500]}")

    if isinstance(data, dict):
        # 某些情况返回单对象，统一成列表
        data = [data]
    return data


# ──────────────────────────── 时间处理 ────────────────────────────

def parse_time(raw: str):
    """解析 ISO8601 时间字符串（兼容带 Z / 带时区 / 无时区）为 aware datetime(UTC)。"""
    if not raw:
        return None
    s = raw.strip()
    # Python<3.11 不识别末尾 'Z'，统一替换
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # 兜底：尝试常见格式
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ)


def relative_time(dt: datetime, now: datetime) -> str:
    """生成「x 分钟前 / x 小时前」相对时间文案。"""
    delta = now - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{secs // 60} 分钟前"
    if secs < 86400:
        return f"{secs // 3600} 小时前"
    return f"{secs // 86400} 天前"


# ──────────────────────────── 过滤 ────────────────────────────

def filter_recent(items: list, hours: float, now: datetime):
    """按时间窗口过滤；返回 (命中列表, 是否可能被截断)。

    截断判定：拉取到的最后一条仍在窗口内，说明窗口内可能还有更多未拉取的动态。
    """
    cutoff = now - timedelta(hours=hours)
    kept = []
    for item in items:
        dt = parse_time(item.get("time"))
        if dt is None:
            continue
        item["_dt"] = dt  # 缓存供渲染复用
        if dt >= cutoff:
            kept.append(item)
    # 按时间倒序（最新在前）
    kept.sort(key=lambda x: x["_dt"], reverse=True)
    truncated = bool(kept) and kept[-1]["_dt"] >= cutoff and len(items) >= 1 and \
        parse_time(items[-1].get("time")) is not None and \
        parse_time(items[-1].get("time")) >= cutoff
    return kept, truncated


# ──────────────────────────── HTML 渲染 ────────────────────────────

URL_RE = re.compile(r"(https?://[^\s<>\"']+)")

def linkify(text: str) -> str:
    """转义文本并把其中的 URL 转为可点击链接，换行转 <br>。"""
    safe = escape(text or "")
    safe = URL_RE.sub(lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>', safe)
    return safe.replace("\n", "<br>")


def avatar_initial(name: str) -> str:
    name = (name or "?").strip()
    return name[0] if name else "?"


def render_html(items: list, hours: float, now: datetime, truncated: bool) -> str:
    """渲染最终 HTML 字符串。"""
    gen_time_str = now.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    window_start_str = (now - timedelta(hours=hours)).astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")

    cards_html = []
    for i, item in enumerate(items, 1):
        dt = item["_dt"]
        local_str = dt.astimezone(BEIJING_TZ).strftime("%Y-%m-%d %H:%M")
        rel_str = relative_time(dt, now)
        author = escape(item.get("author", "匿名"))
        content = linkify(item.get("content", ""))
        url = escape(item.get("url", ""))
        likes = item.get("likes", 0)
        comments = item.get("comments", 0)
        meta_parts = []
        if likes:
            meta_parts.append(f"♥ {likes}")
        if comments:
            meta_parts.append(f"💬 {comments}")
        meta_html = f'<span class="meta">{escape(" · ".join(meta_parts))}</span>' if meta_parts else ""
        url_html = f'<a class="link" href="{url}" target="_blank" rel="noopener">查看原文 →</a>' if url else ""

        cards_html.append(f"""
    <article class="card">
      <header class="card-head">
        <span class="avatar">{escape(avatar_initial(item.get('author', '?')))}</span>
        <div class="head-info">
          <span class="author">{author}</span>
          <span class="time" title="{local_str}">{local_str} · {rel_str}</span>
        </div>
      </header>
      <div class="content">{content}</div>
      <footer class="card-foot">
        {meta_html}
        {url_html}
      </footer>
    </article>""")

    count = len(items)
    empty_html = '<div class="empty">该时间窗口内暂无动态。</div>' if count == 0 else ""
    trunc_html = (
        '<div class="notice">⚠️ 拉取到的最后一条仍在时间窗口内，可能还有更早的动态未被采集。'
        '可尝试增大 <code>--limit</code> 后重新生成。</div>'
        if truncated else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>即刻动态流 · 近 {hours:g} 小时</title>
<style>
  :root {{
    --accent: {ACCENT};
    --accent-dark: {ACCENT_DARK};
    --bg: #f6f7f9;
    --card-bg: #ffffff;
    --text: #1a1a1a;
    --text-sub: #8a8f99;
    --border: #eceef1;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
  }}
  .container {{ max-width: 760px; margin: 0 auto; padding: 32px 20px 80px; }}
  header.report-head {{ margin-bottom: 24px; }}
  h1 {{
    margin: 0 0 8px;
    font-size: 26px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }}
  h1 .badge {{
    display: inline-block;
    background: var(--accent);
    color: #1a1a1a;
    font-size: 13px;
    font-weight: 700;
    padding: 2px 10px;
    border-radius: 999px;
    vertical-align: middle;
    margin-left: 8px;
  }}
  .sub {{ color: var(--text-sub); font-size: 14px; }}
  .stats {{ display: flex; gap: 20px; margin-top: 14px; flex-wrap: wrap; }}
  .stat {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 10px 16px;
    min-width: 90px;
  }}
  .stat .num {{ font-size: 20px; font-weight: 700; }}
  .stat .lbl {{ font-size: 12px; color: var(--text-sub); }}
  .card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 18px 20px;
    margin-bottom: 14px;
    transition: box-shadow .15s ease;
  }}
  .card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,.06); }}
  .card-head {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
  .avatar {{
    width: 40px; height: 40px; flex: 0 0 40px;
    border-radius: 50%;
    background: var(--accent);
    color: #1a1a1a;
    font-weight: 700;
    font-size: 17px;
    display: flex; align-items: center; justify-content: center;
  }}
  .head-info {{ display: flex; flex-direction: column; line-height: 1.35; }}
  .author {{ font-weight: 600; font-size: 15px; }}
  .time {{ font-size: 12px; color: var(--text-sub); }}
  .content {{ font-size: 15px; word-break: break-word; }}
  .content a {{ color: #1a73e8; }}
  .card-foot {{ margin-top: 12px; display: flex; align-items: center; gap: 14px; }}
  .meta {{ font-size: 13px; color: var(--text-sub); }}
  .link {{ margin-left: auto; font-size: 13px; color: var(--accent-dark); text-decoration: none; font-weight: 600; }}
  .link:hover {{ text-decoration: underline; }}
  .empty, .notice {{
    text-align: center; padding: 40px 16px;
    color: var(--text-sub); font-size: 14px;
    background: var(--card-bg); border: 1px dashed var(--border);
    border-radius: 12px; margin-top: 8px;
  }}
  .notice {{ text-align: left; }}
  .notice code {{ background: #fff8d6; padding: 1px 5px; border-radius: 4px; font-size: 13px; }}
  footer.report-foot {{ margin-top: 32px; text-align: center; color: var(--text-sub); font-size: 12px; }}
</style>
</head>
<body>
  <div class="container">
    <header class="report-head">
      <h1>即刻动态流<span class="badge">近 {hours:g} 小时</span></h1>
      <div class="sub">窗口: {window_start_str} ～ 现在 &nbsp;|&nbsp; 生成于 {gen_time_str} (北京时间)</div>
      <div class="stats">
        <div class="stat"><div class="num">{count}</div><div class="lbl">动态条数</div></div>
        <div class="stat"><div class="num">{hours:g}h</div><div class="lbl">时间窗口</div></div>
      </div>
    </header>
    {trunc_html}
    {empty_html}
    {''.join(cards_html)}
    <footer class="report-foot">由 opencli jike feed 拉取 · jike_feed_report.py 生成</footer>
  </div>
</body>
</html>"""


# ──────────────────────────── 主流程 ────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="拉取近 N 小时即刻动态并生成 HTML 报告",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS,
                        help="时间窗口（小时）")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help="opencli 单次拉取条数")
    parser.add_argument("--output", type=str, default=None,
                        help="HTML 输出目录（默认: 脚本同级 output/ 目录）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="opencli 子进程超时（秒）")
    args = parser.parse_args()

    now = datetime.now(UTC_TZ)
    print(f"→ 拉取即刻动态流（limit={args.limit}）…")
    items = fetch_jike_feed(limit=args.limit, timeout=args.timeout)
    print(f"  共拉取 {len(items)} 条原始动态。")

    kept, truncated = filter_recent(items, args.hours, now)
    print(f"  近 {args.hours:g} 小时命中 {len(kept)} 条。")
    if truncated:
        print("  ⚠️ 可能存在截断，建议增大 --limit 重试。")

    html = render_html(kept, args.hours, now, truncated)

    out_dir = Path(args.output) if args.output else Path(__file__).resolve().parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = now.astimezone(BEIJING_TZ).strftime("%Y%m%d-%H%M%S")
    out_file = out_dir / f"jike-feed-{ts}.html"
    out_file.write_text(html, encoding="utf-8")

    print(f"✅ 已生成: {out_file}")
    return out_file


if __name__ == "__main__":
    main()
