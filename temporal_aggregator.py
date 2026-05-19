#!/usr/bin/env python3
"""
时序记忆聚合器 — 从 daily 摘要生成 weekly/monthly 聚合。

用法:
  # 查看最近 N 天的 daily 摘要
  temporal_aggregator.py list --days 7

  # 聚合本周为周摘要（写入 hindsight）
  temporal_aggregator.py weekly

  # 聚合本月为月摘要
  temporal_aggregator.py monthly

  # 查看时序层级状态
  temporal_aggregator.py status
"""
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen

API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"


def fetch_by_tag(tag: str, limit: int = 100) -> list:
    """按标签获取记忆。"""
    url = f"{API_BASE}/memories/recall"
    payload = json.dumps({"query": tag, "limit": limit,
                          "tags": [tag], "tags_match": "any"}).encode()
    try:
        req = Request(url, data=payload,
                       headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        return data.get("results", data if isinstance(data, list) else [])
    except Exception as e:
        print(f"⚠️ 获取记忆失败: {e}", file=sys.stderr)
        return []


def fetch_all_recent(limit: int = 500) -> list:
    """获取最近记忆列表。"""
    url = f"{API_BASE}/memories/list?limit={limit}&order=-created_at"
    try:
        resp = urlopen(url, timeout=15)
        data = json.loads(resp.read().decode())
        if isinstance(data, list):
            return data
        return data.get("items", data.get("data", []))
    except Exception as e:
        print(f"⚠️ 获取列表失败: {e}", file=sys.stderr)
        return []


def tag_exists(tag: str) -> bool:
    """检查 hindsight 中是否已有该标签的记忆。"""
    results = fetch_by_tag(tag, limit=1)
    return len(results) > 0


def write_summary(content: str, tags: list[str]) -> bool:
    """写入一条摘要到 hindsight。"""
    url = f"{API_BASE}/memories"
    payload = json.dumps({
        "items": [{"content": content[:2000], "tags": tags}]
    }).encode()
    try:
        req = Request(url, data=payload,
                       headers={"Content-Type": "application/json"},
                       method="POST")
        resp = urlopen(req, timeout=15)
        return True
    except Exception as e:
        print(f"⚠️ 写入失败: {e}", file=sys.stderr)
        return False


def compute_week_id(dt: datetime = None) -> str:
    """计算周标识，如 '2026-W19'。"""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-W%W")


def compute_month_id(dt: datetime = None) -> str:
    """计算月标识，如 '2026-05'。"""
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def cmd_list(days: int = 7):
    """列出最近 N 天的 daily 摘要。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    memories = fetch_all_recent(500)
    
    daily_entries = []
    for mem in memories:
        tags = mem.get("tags", []) or []
        if "daily-summary" in tags:
            ts_str = mem.get("occurred_start", "") or mem.get("mentioned_at", "")
            if ts_str:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        daily_entries.append(ts)
                        text = (mem.get("text", "") or "")[:150]
                        print(f"  [{ts.strftime('%m-%d')}] {text}")
                except:
                    pass
    
    if not daily_entries:
        print(f"近 {days} 天无 daily-summary")
    else:
        print(f"\n共 {len(daily_entries)} 条 daily-summary")


def cmd_weekly():
    """从 daily 摘要聚合本周周摘要。"""
    week_id = compute_week_id()
    tag = f"weekly-summary:{week_id}"
    
    if tag_exists(tag):
        print(f"⚠️ {week_id} 周摘要已存在")
        return
    
    # 获取本周 daily 摘要
    start_of_week = datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())
    daily_entries = fetch_by_tag("daily-summary", 50)
    
    weekly_content = []
    for mem in daily_entries:
        ts_str = mem.get("occurred_start", "") or mem.get("mentioned_at", "")
        text = (mem.get("text", "") or "")[:300]
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts >= start_of_week:
                    weekly_content.append(f"[{ts.strftime('%m-%d')}] {text}")
            except:
                pass
    
    if not weekly_content:
        print("本周无 daily 摘要数据")
        return
    
    summary = (f"# {week_id} 周度记忆摘要\n" +
               f"聚合自 {len(weekly_content)} 条日摘要\n\n" +
               "\n".join(weekly_content))
    
    if write_summary(summary, [tag, "temporal", "weekly-summary",
                                f"entity|object:temporal_memory",
                                f"date:{start_of_week.strftime('%Y-%m-%d')}"]):
        print(f"✅ 已写入 {week_id} 周摘要 ({len(weekly_content)} 条日摘要)")


def cmd_monthly():
    """从周摘要聚合月摘要。"""
    month_id = compute_month_id()
    tag = f"monthly-summary:{month_id}"
    
    if tag_exists(tag):
        print(f"⚠️ {month_id} 月摘要已存在")
        return
    
    # 获取当月所有 weekly-summary
    weekly_entries = fetch_by_tag("weekly-summary", 50)
    
    monthly_content = []
    for mem in weekly_entries:
        text = (mem.get("text", "") or "")[:500]
        tags = mem.get("tags", []) or []
        for t in tags:
            if t.startswith("weekly-summary:") and month_id in t:
                monthly_content.append(text)
                break
    
    if not monthly_content:
        print("本月无周摘要数据")
        return
    
    summary = (f"# {month_id} 月度记忆摘要\n" +
               f"聚合自 {len(monthly_content)} 条周摘要\n\n" +
               "\n---\n".join([c[:300] for c in monthly_content]))
    
    if write_summary(summary, [tag, "temporal", "monthly-summary",
                                f"entity|object:temporal_memory",
                                f"date:{month_id}"]):
        print(f"✅ 已写入 {month_id} 月摘要 ({len(monthly_content)} 条周摘要)")


def cmd_status():
    """查看时序层级状态。"""
    all_mem = fetch_all_recent(500)
    
    levels = {
        "daily-summary": [],
        "weekly-summary": [],
        "monthly-summary": [],
        "session-summary": [],
    }
    
    for mem in all_mem:
        tags = mem.get("tags", []) or []
        for tag in tags:
            ts = str(tag)
            for level in levels:
                if level in ts:
                    text = (mem.get("text", "") or "")[:80]
                    levels[level].append(text)
    
    print("📊 时序层级状态\n")
    for level, entries in levels.items():
        print(f"  {level:20s}  {len(entries):3d} 条")
        if entries:
            for e in entries[:2]:
                print(f"    → {e}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    cmd = sys.argv[1]
    
    if cmd == "list":
        days = 7
        if "--days" in sys.argv:
            idx = sys.argv.index("--days")
            if idx + 1 < len(sys.argv):
                days = int(sys.argv[idx + 1])
        cmd_list(days)
    elif cmd == "weekly":
        cmd_weekly()
    elif cmd == "monthly":
        cmd_monthly()
    elif cmd == "status":
        cmd_status()
    else:
        print(f"未知命令: {cmd}")


if __name__ == "__main__":
    main()
