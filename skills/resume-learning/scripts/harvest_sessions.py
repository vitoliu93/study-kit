#!/usr/bin/env python3
"""汇总四个 CLI 在指定 cwd 的本地会话里, 自某日以来的用户 prompt, 按时间排序输出。

用法: harvest_sessions.py <cwd> [--since YYYY-MM-DD]   (--since 默认今天)

来源:
  claude   ~/.claude/projects/<munged-cwd>/*.jsonl
  codex    ~/.codex/state_*.sqlite threads 表按 cwd 索引 rollout 文件(含 archived), 带会话标题
  grok     ~/.grok/sessions/<urlencode(cwd)>/prompt_history.jsonl
  cursor   ~/.cursor/chats/<md5(cwd)>/*/store.db + Cursor IDE globalStorage composer
"""
import argparse
import hashlib
import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
MAXLEN = 240


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def clean(text):
    text = " ".join(text.split())
    # 跳过注入的上下文/命令包装, 只留用户亲手打的内容
    if not text or text.startswith(
        ("<", "Caveat:", "# AGENTS.md", "Your conversation was summarized")
    ):
        return None
    return text[:MAXLEN]


def from_claude(cwd, since):
    proj = HOME / ".claude/projects" / re.sub(r"[^A-Za-z0-9-]", "-", cwd)
    for f in proj.glob("*.jsonl"):
        if datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc) < since:
            continue
        for line in f.open():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "user" or d.get("isMeta"):
                continue
            ts = parse_ts(d["timestamp"])
            if ts < since:
                continue
            c = d.get("message", {}).get("content")
            if isinstance(c, list):
                if any(x.get("type") == "tool_result" for x in c):
                    continue
                has_img = any(x.get("type") == "image" for x in c)
                c = " ".join(x.get("text", "") for x in c if x.get("type") == "text")
                if has_img:
                    c = "[图片] " + c
            if isinstance(c, str) and (t := clean(c)):
                yield ts, "claude", t


def from_codex(cwd, since):
    # threads 表是官方索引: cwd/title/rollout_path, 且覆盖 archived_sessions/
    dbs = sorted((HOME / ".codex").glob("state_*.sqlite"))
    if not dbs:
        return
    con = sqlite3.connect(f"file:{dbs[-1]}?mode=ro", uri=True)
    rows = con.execute(
        "select rollout_path, title, updated_at_ms from threads"
        " where cwd=? and updated_at_ms>=?",
        (cwd, since.timestamp() * 1000),
    ).fetchall()
    con.close()
    # threads.title 只是首条用户消息, 与 prompt 输出重复, 不单独输出
    for path, _title, _upd in rows:
        f = Path(path)
        if not f.exists():
            continue
        seen = set()
        for line in f.open():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = d.get("payload", {})
            text = None
            if d.get("type") == "event_msg" and p.get("type") == "user_message":
                text = p.get("message", "")
            elif d.get("type") == "response_item" and p.get("role") == "user":
                text = " ".join(
                    x.get("text", "") for x in p.get("content", [])
                    if x.get("type") == "input_text"
                )
            if text and (t := clean(text)) and t not in seen:
                seen.add(t)
                ts = parse_ts(d["timestamp"])
                if ts >= since:
                    yield ts, "codex", t


def from_grok(cwd, since):
    root = HOME / ".grok/sessions" / urllib.parse.quote(cwd, safe="")
    # session_summary 是"agent 做了什么"的现成蒸馏
    for sj in root.glob("*/summary.json"):
        try:
            d = json.loads(sj.read_text())
        except json.JSONDecodeError:
            continue
        ts = parse_ts(d["last_active_at"])
        if ts >= since and (t := clean(d.get("session_summary", ""))):
            yield ts, "grok:摘要", t
    f = root / "prompt_history.jsonl"
    if not f.exists():
        return
    for line in f.open():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("is_bash"):
            continue
        ts = parse_ts(d["timestamp"])
        if ts >= since and (t := clean(d.get("prompt", ""))):
            yield ts, "grok", t


def from_cursor_cli(cwd, since):
    root = HOME / ".cursor/chats" / hashlib.md5(cwd.encode()).hexdigest()
    for db in root.glob("*/store.db"):
        mtime = datetime.fromtimestamp(db.stat().st_mtime, tz=timezone.utc)
        if mtime < since:
            continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute("select data from blobs").fetchall()
            con.close()
        except sqlite3.Error:
            continue
        for (blob,) in rows:
            try:
                d = json.loads(blob)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if d.get("role") != "user":
                continue
            c = d.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if x.get("type") == "text")
            if isinstance(c, str) and (t := clean(c)):
                # ponytail: blob 无逐条时间戳, 借 session 文件 mtime
                yield mtime, "cursor", t


def from_cursor_ide(cwd, since):
    ws_root = HOME / "Library/Application Support/Cursor/User/workspaceStorage"
    gdb = HOME / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    if not gdb.exists():
        return
    uri = "file://" + urllib.parse.quote(cwd)
    ws_ids = [
        wj.parent.name
        for wj in ws_root.glob("*/workspace.json")
        if json.loads(wj.read_text()).get("folder") == uri
    ]
    if not ws_ids:
        return
    con = sqlite3.connect(f"file:{gdb}?mode=ro", uri=True)
    comp_ids = [
        r[0]
        for ws in ws_ids
        for r in con.execute(
            "select composerId from composerHeaders where workspaceId=?", (ws,)
        )
    ]
    for cid in comp_ids:
        row = con.execute(
            "select value from cursorDiskKV where key=?", (f"composerData:{cid}",)
        ).fetchone()
        if not row:
            continue
        cd = json.loads(row[0])
        if (upd := cd.get("lastUpdatedAt")) and cd.get("name"):
            ts = datetime.fromtimestamp(upd / 1000, tz=timezone.utc)
            if ts >= since and (t := clean(cd["name"])):
                yield ts, "cursor-ide:标题", t
        for h in cd.get("fullConversationHeadersOnly", []):
            if h.get("type") != 1 or "createdAt" not in h:
                continue
            ts = parse_ts(h["createdAt"])
            if ts < since:
                continue
            brow = con.execute(
                "select value from cursorDiskKV where key=?",
                (f"bubbleId:{cid}:{h['bubbleId']}",),
            ).fetchone()
            if not brow:
                continue
            b = json.loads(brow[0])
            text = b.get("text", "")
            if b.get("images") and not text:
                text = "[图片]"
            if t := clean(text):
                yield ts, "cursor-ide", t
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cwd")
    ap.add_argument("--since", default=datetime.now().strftime("%Y-%m-%d"))
    args = ap.parse_args()
    cwd = args.cwd.rstrip("/")
    since_str = args.since
    since = datetime.fromisoformat(since_str).astimezone()

    items = []
    for src in (from_claude, from_codex, from_grok, from_cursor_cli, from_cursor_ide):
        items.extend(src(cwd, since))
    items.sort(key=lambda x: x[0])

    if not items:
        print(f"(自 {since_str} 起, 四个 CLI 在 {cwd} 下均无用户对话记录)")
        return
    for ts, src, text in items:
        local = ts.astimezone().strftime("%m-%d %H:%M")
        print(f"{local} [{src}] {text}")


if __name__ == "__main__":
    main()
