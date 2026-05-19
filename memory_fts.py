#!/usr/bin/env python3
"""
客户端全文检索（FTS）— 精确关键词/正则匹配，不走语义搜索。

pgvector 语义搜索对精确代码（600519）、文件路径（run_daily.py）、
版本号（v1.2.0）表现差。本工具通过 list 端点拉取全部记忆后本地 grep，
支持正则和精确匹配。

用法:
  # 精确匹配
  memory_fts.py search "600519"
  memory_fts.py search "run_daily_analysis" --exact

  # 正则匹配
  memory_fts.py search "v\d+\.\d+\.\d+" --regex

  # 按标签过滤
  memory_fts.py search "ADX" --tag "entity|fix"

  # 查看索引统计
  memory_fts.py stats
"""
import json
import os
import re
import sys
import time
from urllib.request import urlopen

API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"
CACHE_FILE = os.path.expanduser("~/.hermes/hindsight/fts_cache.json")
CACHE_TTL = 300  # 5分钟


def fetch_all(force: bool = False) -> list[dict]:
    """获取所有记忆（带缓存）。"""
    now = time.time()
    if not force and os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            cache = json.load(f)
        if now - cache.get("cached_at", 0) < CACHE_TTL:
            return cache.get("items", [])
    
    all_items = []
    limit = 500
    offset = 0
    while True:
        url = f"{API_BASE}/memories/list?limit={limit}&offset={offset}&order=-created_at"
        try:
            resp = urlopen(url, timeout=30)
            data = json.loads(resp.read().decode())
            if isinstance(data, list):
                items = data
            else:
                items = data.get("items", data.get("data", []))
            
            if not items:
                break
            all_items.extend(items)
            offset += limit
            
            if len(items) < limit:
                break
        except Exception as e:
            print(f"⚠️ 获取失败: {e}", file=sys.stderr)
            break
    
    # Write cache
    with open(CACHE_FILE, "w") as f:
        json.dump({"cached_at": now, "items": all_items}, f, ensure_ascii=False)
    
    return all_items


def cmd_search(pattern: str, exact: bool = False, regex: bool = False, tag: str | None = None):
    """搜索记忆。"""
    items = fetch_all()
    if not items:
        print("无记忆数据")
        return
    
    if regex:
        try:
            pat = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            print(f"正则错误: {e}")
            return
    elif exact:
        pat = re.compile(re.escape(pattern), re.IGNORECASE)
    else:
        # 智能匹配：数字代码精确，文本模糊
        if pattern.isdigit() and len(pattern) >= 4:
            pat = re.compile(re.escape(pattern))
        else:
            # 拆成多个关键词，全部命中才算
            keywords = [kw.strip() for kw in re.split(r'[\s,，]+', pattern) if kw.strip()]
            if len(keywords) > 1:
                def multi_match(text):
                    t = text.lower()
                    return all(kw.lower() in t for kw in keywords)
                pat = multi_match
            else:
                pat = re.compile(re.escape(pattern), re.IGNORECASE)
    
    results = []
    for item in items:
        text = (item.get("text", "") or "")
        if not text:
            continue
        
        # Tag filter
        if tag:
            tags = item.get("tags", []) or []
            if not any(tag in str(t) for t in tags):
                continue
        
        # Match
        if callable(pat):
            if not pat(text):
                continue
        else:
            if not pat.search(text):
                continue
        
        mid = item.get("id", "")[:16]
        tags = [str(t) for t in (item.get("tags", []) or []) 
                if not t.startswith(("session:", "parent:"))][:3]
        results.append({
            "id": mid,
            "text": text[:200],
            "tags": tags,
            "ts": item.get("occurred_start", "") or item.get("mentioned_at", ""),
        })
    
    if results:
        print(f"🔍 找到 {len(results)} 条匹配「{pattern}」:\n")
        for r in results[:20]:
            tag_str = " ".join(r["tags"]) if r["tags"] else ""
            ts = r["ts"][:10] if r["ts"] else ""
            print(f"  [{r['id']}] {ts} {tag_str}")
            print(f"    {r['text']}")
            print()
        if len(results) > 20:
            print(f"  ... 还有 {len(results) - 20} 条")
    else:
        print(f"未找到匹配「{pattern}」的记忆")


def cmd_stats():
    """索引统计。"""
    items = fetch_all(force=True)
    if not items:
        print("无数据")
        return
    
    total = len(items)
    total_chars = sum(len(item.get("text", "") or "") for item in items)
    
    # 标签分布
    tag_counts = {}
    for item in items:
        for tag in (item.get("tags", []) or []):
            ts = str(tag)
            tag_counts[ts] = tag_counts.get(ts, 0) + 1
    
    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:15]
    
    print(f"📊 FTS 索引统计")
    print(f"   总记忆: {total}")
    print(f"   总字符: {total_chars:,}")
    print(f"   缓存: {CACHE_FILE}")
    print(f"\n   标签 TOP 15:")
    for tag, count in top_tags:
        print(f"     {tag:30s}  {count}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    cmd = sys.argv[1]
    
    if cmd == "search" and len(sys.argv) >= 3:
        exact = "--exact" in sys.argv
        regex = "--regex" in sys.argv
        tag = None
        if "--tag" in sys.argv:
            idx = sys.argv.index("--tag")
            if idx + 1 < len(sys.argv):
                tag = sys.argv[idx + 1]
        cmd_search(" ".join([a for a in sys.argv[2:] if not a.startswith("--")]), exact, regex, tag)
    elif cmd == "stats":
        cmd_stats()
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
