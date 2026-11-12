#!/usr/bin/env python3
"""Export all WeRead (微信读书) highlights and thoughts.

Usage:  WEREAD_API_KEY=wrk-... python3 sync.py

Writes, next to this script:
  notes/<书名>.md   one Markdown file per book
  README.md         index of all books
  site/data.json    data for the searchable website in site/
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.5"
ROOT = Path(__file__).resolve().parent
NOTES_DIR = ROOT / "notes"
SITE_DIR = ROOT / "site"
TZ = timezone(timedelta(hours=8))  # WeRead timestamps shown in China time

API_KEY = os.environ.get("WEREAD_API_KEY")
if not API_KEY:
    sys.exit("请先设置 WEREAD_API_KEY：export WEREAD_API_KEY=<你的apikey>")


def call(api_name, **params):
    body = {"api_name": api_name, "skill_version": SKILL_VERSION, **params}
    req = urllib.request.Request(
        GATEWAY,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read())
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    if data.get("upgrade_info"):
        print("⚠️  skill 需要升级:", data["upgrade_info"].get("message"), file=sys.stderr)
    if data.get("errcode"):
        raise RuntimeError(f"{api_name} 出错: {data.get('errcode')} {data.get('errmsg')}")
    return data


def date(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d") if ts else ""


def range_start(r):
    try:
        return int(str(r).split("-")[0])
    except (ValueError, TypeError):
        return 0


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|\n\r\t]', "_", s).strip()[:80] or "untitled"


def all_notebooks():
    books, last = [], None
    while True:
        p = {"count": 50}
        if last is not None:
            p["lastSort"] = last
        d = call("/user/notebooks", **p)
        page = d.get("books", [])
        books += page
        if not d.get("hasMore") or not page:
            return books
        last = page[-1]["sort"]


def my_reviews(book_id):
    out, synckey = [], 0
    while True:
        d = call("/review/list/mine", bookid=book_id, synckey=synckey, count=50)
        out += [x["review"] for x in d.get("reviews", []) if x.get("review")]
        if not d.get("hasMore"):
            return out
        synckey = d.get("synckey", 0)


def collect_book(nb):
    """Fetch one book's notes and return a structured dict."""
    info = nb.get("book", {})
    book_id = nb["bookId"]
    marks = call("/book/bookmarklist", bookId=book_id)
    reviews = my_reviews(book_id)
    chapters = {c["chapterUid"]: c for c in marks.get("chapters", [])}

    thoughts = {}  # thoughts attached to a highlight, keyed by (chapterUid, range)
    for rv in reviews:
        if rv.get("abstract") and rv.get("range") and rv.get("chapterUid") is not None:
            thoughts.setdefault((rv["chapterUid"], rv["range"]), []).append(rv)

    groups, book_reviews, used = {}, [], set()
    for m in marks.get("updated", []):
        cu, rg = m.get("chapterUid"), m.get("range")
        ts = [t for t in thoughts.get((cu, rg), [])]
        used.update(t["reviewId"] for t in ts)
        groups.setdefault(cu, []).append({
            "type": "highlight",
            "pos": range_start(rg),
            "text": m.get("markText", "").strip(),
            "thought": "\n\n".join(t.get("content", "").strip() for t in ts),
            "time": max([m.get("createTime", 0)] + [t.get("createTime", 0) for t in ts]),
        })

    for rv in reviews:
        if rv["reviewId"] in used:
            continue
        cu = rv.get("chapterUid")
        content = rv.get("content", "").strip()
        item = {"text": "", "thought": content, "time": rv.get("createTime", 0)}
        if rv.get("abstract"):  # thought on a passage that isn't in the highlight list
            item.update(type="highlight", pos=range_start(rv.get("range")),
                        text=rv["abstract"].strip())
            groups.setdefault(cu, []).append(item)
        elif cu is not None and rv.get("chapterName"):
            item.update(type="chapter_review", pos=10**9)
            groups.setdefault(cu, []).append(item)
        else:
            star = rv.get("star", -1) or 0
            if star > 5:  # API actually returns 0-100 (e.g. 100, 60); convert to 5 stars
                star = round(star / 20)
            item.update(type="book_review", star=max(0, min(5, star)))
            book_reviews.append(item)

    if any(cu is not None and cu not in chapters for cu in groups):
        try:
            for c in call("/book/chapterinfo", bookId=book_id).get("chapters", []):
                chapters.setdefault(c["chapterUid"], c)
        except Exception as e:
            print(f"  (章节目录获取失败: {e})", file=sys.stderr)

    def chap_idx(cu):
        c = chapters.get(cu)
        return c.get("chapterIdx", 10**6) if c else 10**6 + (cu if isinstance(cu, int) else 0)

    out_chapters = []
    for cu in sorted(groups, key=lambda k: (k is None, chap_idx(k) if k is not None else 0)):
        title = chapters.get(cu, {}).get("title") or (f"章节 {cu}" if cu is not None else "其他")
        items = sorted(groups[cu], key=lambda x: x["pos"])
        for it in items:
            it.pop("pos", None)
        out_chapters.append({"title": title, "items": items})

    cats = [c.get("title", "") for c in info.get("categories", []) if c.get("title")]
    return {
        "id": book_id,
        "title": info.get("title", book_id),
        "author": info.get("author", ""),
        "translator": info.get("translator", ""),
        "cover": info.get("cover", ""),
        "category": cats[0] if cats else "",
        "link": info.get("readerLink") or info.get("deepLink") or "",
        "finished": nb.get("markedStatus") == 1,
        "progress": nb.get("readingProgress"),
        "counts": {
            "highlights": nb.get("noteCount", 0),
            "thoughts": nb.get("reviewCount", 0),
            "bookmarks": nb.get("bookmarkCount", 0),
        },
        "updated": nb.get("sort", 0),
        "reviews": book_reviews,
        "chapters": out_chapters,
    }


def quote(text):
    return "> " + text.replace("\n", "\n> ")


def render_md(b):
    meta = [f"**作者**：{b['author']}"]
    if b["finished"]:
        meta.append("**状态**：读完")
    elif b["progress"] is not None:
        meta.append(f"**进度**：{b['progress']}%")
    c = b["counts"]
    meta.append(f"**笔记**：划线 {c['highlights']} · 想法/点评 {c['thoughts']} · 书签 {c['bookmarks']}")
    out = [f"# {b['title']}", "", " · ".join(meta), ""]
    if b["link"]:
        out += [f"[打开阅读]({b['link']})", ""]
    if b["reviews"]:
        out += ["## 书评", ""]
        for r in b["reviews"]:
            stars = f"（{'★' * r['star']}{'☆' * (5 - r['star'])}）" if r.get("star") else ""
            out += [f"{r['thought']}{stars}", "", f"<sub>{date(r['time'])}</sub>", ""]
    for ch in b["chapters"]:
        out += [f"## {ch['title']}", ""]
        for it in ch["items"]:
            if it["type"] == "chapter_review":
                out += [f"📝 **章节点评**：{it['thought']}", ""]
            else:
                out += [quote(it["text"]), ""]
                if it["thought"]:
                    out += ["💭 " + it["thought"], ""]
            out += [f"<sub>{date(it['time'])}</sub>", ""]
    return "\n".join(out).rstrip() + "\n"


def main():
    NOTES_DIR.mkdir(exist_ok=True)
    SITE_DIR.mkdir(exist_ok=True)
    notebooks = all_notebooks()
    print(f"共 {len(notebooks)} 本书有笔记")
    books, written = [], set()
    for i, nb in enumerate(notebooks, 1):
        b = collect_book(nb)
        fname = safe_name(b["title"])
        if fname in written:
            fname = f"{fname}_{b['id']}"
        written.add(fname)
        b["file"] = fname
        (NOTES_DIR / f"{fname}.md").write_text(render_md(b), encoding="utf-8")
        books.append(b)
        print(f"[{i}/{len(notebooks)}] {b['title']}")

    for f in NOTES_DIR.glob("*.md"):  # books whose notes were all deleted
        if f.stem not in written:
            f.unlink()

    books.sort(key=lambda b: b["updated"], reverse=True)
    total = sum(sum(b["counts"].values()) for b in books)
    lines = ["# 微信读书笔记", "",
             f"共 {len(books)} 本书、{total} 条笔记，按最近笔记时间排序。", "",
             "| # | 书名 | 作者 | 笔记数 | 最近笔记 |", "|---|---|---|---|---|"]
    for n, b in enumerate(books, 1):
        link = "notes/" + urllib.parse.quote(b["file"]) + ".md"
        lines.append(f"| {n} | [{b['title']}]({link}) | {b['author']} | "
                     f"{sum(b['counts'].values())} | {date(b['updated'])} |")
    (ROOT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    data = {"books": books}
    (SITE_DIR / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    main()
