#!/usr/bin/env python3
"""Export all WeRead (微信读书) highlights and thoughts to Markdown, one file per book.

Usage:  WEREAD_API_KEY=wrk-... python3 sync.py
Writes notes/<书名>.md and README.md (index) next to this script.
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

GATEWAY = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.5"
ROOT = Path(__file__).resolve().parent
NOTES_DIR = ROOT / "notes"

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
        except Exception as e:  # network hiccup: retry
            if attempt == 2:
                raise
            time.sleep(2)
    if data.get("upgrade_info"):
        print("⚠️  skill 需要升级:", data["upgrade_info"].get("message"), file=sys.stderr)
    if data.get("errcode"):
        raise RuntimeError(f"{api_name} 出错: {data.get('errcode')} {data.get('errmsg')}")
    return data


def date(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""


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


def render_book(nb):
    info = nb.get("book", {})
    book_id = nb["bookId"]
    title = info.get("title", book_id)
    marks = call("/book/bookmarklist", bookId=book_id)
    reviews = my_reviews(book_id)

    chapters = {c["chapterUid"]: c for c in marks.get("chapters", [])}
    # group items by chapter; each item = (sort_key, markdown)
    groups = {}
    book_reviews = []

    # thoughts attached to a highlight range, keyed by (chapterUid, range)
    thoughts = {}
    for rv in reviews:
        if rv.get("abstract") and rv.get("range") and rv.get("chapterUid") is not None:
            thoughts.setdefault((rv["chapterUid"], rv["range"]), []).append(rv)

    used = set()
    for m in marks.get("updated", []):
        cu, rg = m.get("chapterUid"), m.get("range")
        lines = ["> " + m.get("markText", "").replace("\n", "\n> ")]
        for t in thoughts.get((cu, rg), []):
            used.add(t["reviewId"])
            lines.append(f"\n💭 {t.get('content', '').strip()}")
        lines.append(f"\n<sub>{date(m.get('createTime'))}</sub>")
        groups.setdefault(cu, []).append((range_start(rg), "\n".join(lines)))

    for rv in reviews:
        if rv["reviewId"] in used:
            continue
        cu = rv.get("chapterUid")
        content = rv.get("content", "").strip()
        if rv.get("abstract"):  # thought on a passage not in the highlight list
            md = "> " + rv["abstract"].replace("\n", "\n> ") + f"\n\n💭 {content}"
            md += f"\n\n<sub>{date(rv.get('createTime'))}</sub>"
            groups.setdefault(cu, []).append((range_start(rv.get("range")), md))
        elif cu is not None and rv.get("chapterName"):  # chapter comment
            md = f"📝 **章节点评**：{content}\n\n<sub>{date(rv.get('createTime'))}</sub>"
            groups.setdefault(cu, []).append((10**9, md))
        else:  # whole-book review
            star = rv.get("star", -1)
            stars = f"（{'★' * star}{'☆' * (5 - star)}）" if star and star > 0 else ""
            book_reviews.append(f"{content}{stars}\n\n<sub>{date(rv.get('createTime'))}</sub>")

    out = [f"# {title}", ""]
    meta = [f"**作者**：{info.get('author', '')}"]
    if nb.get("markedStatus") == 1:
        meta.append("**状态**：读完")
    elif nb.get("readingProgress") is not None:
        meta.append(f"**进度**：{nb['readingProgress']}%")
    meta.append(
        f"**笔记**：划线 {nb.get('noteCount', 0)} · 想法/点评 {nb.get('reviewCount', 0)}"
        f" · 书签 {nb.get('bookmarkCount', 0)}"
    )
    out += [" · ".join(meta), ""]
    if book_reviews:
        out += ["## 书评", ""] + ["\n\n".join(book_reviews), ""]

    missing = [cu for cu in groups if cu is not None and cu not in chapters]
    if missing:
        try:
            for c in call("/book/chapterinfo", bookId=book_id).get("chapters", []):
                chapters.setdefault(c["chapterUid"], c)
        except Exception as e:
            print(f"  (章节目录获取失败: {e})", file=sys.stderr)

    def chap_key(cu):
        c = chapters.get(cu)
        return c.get("chapterIdx", 10**6) if c else (cu if isinstance(cu, int) else 10**6)

    for cu in sorted(groups, key=lambda k: (k is None, chap_key(k) if k is not None else 0)):
        name = chapters.get(cu, {}).get("title") or (f"章节 {cu}" if cu is not None else "其他")
        out += [f"## {name}", ""]
        for _, md in sorted(groups[cu], key=lambda x: x[0]):
            out += [md, ""]
    return title, info.get("author", ""), "\n".join(out).rstrip() + "\n"


def main():
    NOTES_DIR.mkdir(exist_ok=True)
    notebooks = all_notebooks()
    print(f"共 {len(notebooks)} 本书有笔记")
    index, written = [], set()
    for i, nb in enumerate(notebooks, 1):
        title, author, md = render_book(nb)
        fname = safe_name(title)
        if fname in written:
            fname = f"{fname}_{nb['bookId']}"
        written.add(fname)
        (NOTES_DIR / f"{fname}.md").write_text(md, encoding="utf-8")
        total = nb.get("noteCount", 0) + nb.get("reviewCount", 0) + nb.get("bookmarkCount", 0)
        index.append((nb.get("sort", 0), title, author, fname, total))
        print(f"[{i}/{len(notebooks)}] {title}")

    # remove files for books that no longer have notes
    for f in NOTES_DIR.glob("*.md"):
        if f.stem not in written:
            f.unlink()

    lines = ["# 微信读书笔记", "", f"共 {len(index)} 本书，按最近笔记时间排序。", "",
             "| # | 书名 | 作者 | 笔记数 | 最近笔记 |", "|---|---|---|---|---|"]
    for n, (sort, title, author, fname, total) in enumerate(sorted(index, reverse=True), 1):
        link = "notes/" + urllib.request.quote(fname) + ".md"
        lines.append(f"| {n} | [{title}]({link}) | {author} | {total} | {date(sort)} |")
    (ROOT / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
