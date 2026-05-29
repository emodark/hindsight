#!/usr/bin/env python3
"""
低质量记忆批量清除脚本 — 按质量阈值删除 PG 中的低置信条目并同步 quality.json。

用法:
  python3 purge_low_quality.py                    # 默认 q<0.35，预览不删除
  python3 purge_low_quality.py --apply            # 实际执行删除
  python3 purge_low_quality.py --threshold 0.25   # 自定义阈值
  python3 purge_low_quality.py --dry-run          # 只报告不删除（同默认行为）
  python3 purge_low_quality.py --apply --threshold 0.35

逻辑:
  1. 读 memory_quality.json
  2. 找出 quality < threshold 的活跃条目
  3. 用 PSQL 级联删除 PG 中的对应记忆
  4. 从 quality.json 中移除已删除条目
  5. 更新 memory_stats.json
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

HOMES_DIR = os.path.expanduser("~/.hermes")
HINDSIGHT_DIR = os.path.join(HOMES_DIR, "hindsight")
QUALITY_FILE = os.path.join(HINDSIGHT_DIR, "memory_quality.json")
STATS_FILE = os.path.join(HOMES_DIR, "self", "memory_stats.json")
DEFAULT_THRESHOLD = 0.35

# PG 连接参数
PG_HOST = "127.0.0.1"
PG_PORT = "5433"
PG_USER = "hindsight"
PG_DB = "hindsight"
PG_PASS = "hindsight"


def log(msg: str):
    """统一日志输出"""
    ts = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def get_pg_uids() -> set[str]:
    """从 PG 读全部 memory_units ID"""
    try:
        result = subprocess.run(
            ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER,
             "-d", PG_DB, "-tA",
             "-c", "SELECT id FROM memory_units"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PGPASSWORD": PG_PASS}
        )
        if result.returncode == 0 and result.stdout.strip():
            return set(line.strip() for line in result.stdout.split("\n") if line.strip())
    except Exception as e:
        log(f"⚠️  PG 查询失败: {e}")
    return set()


def delete_from_pg(uids: list[str]) -> int:
    """级联删除 PG 中的记忆（分批 200/批）"""
    if not uids:
        return 0

    total = 0
    batch_size = 200
    for i in range(0, len(uids), batch_size):
        batch = uids[i:i + batch_size]
        uid_list = ", ".join(f"'{u}'" for u in batch)
        sql = f"""DELETE FROM memory_links WHERE from_unit_id IN ({uid_list}) OR to_unit_id IN ({uid_list});
DELETE FROM unit_entities WHERE unit_id IN ({uid_list});
DELETE FROM memory_units WHERE id IN ({uid_list});"""
        try:
            result = subprocess.run(
                ["psql", "-h", PG_HOST, "-p", PG_PORT, "-U", PG_USER,
                 "-d", PG_DB, "-tA", "-c", sql],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "PGPASSWORD": PG_PASS}
            )
            lines = [l.strip() for l in result.stdout.split("\n") if l.strip() and "DELETE" in l.upper()]
            if lines:
                last = lines[-1]
                count = int(last.split()[-1]) if last.split()[-1].isdigit() else 0
                total += count
        except Exception as e:
            log(f"  ⚠️  分批删除失败(batch {i // batch_size}): {e}")

    return total


def load_quality() -> dict:
    """加载 memory_quality.json"""
    if not os.path.exists(QUALITY_FILE):
        log(f"❌ quality.json 不存在: {QUALITY_FILE}")
        sys.exit(1)
    with open(QUALITY_FILE) as f:
        return json.load(f)


def save_quality(data: dict):
    """写回 memory_quality.json"""
    with open(QUALITY_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_stats(total_removed: int, final_total: int, low_remaining: int):
    """更新 memory_stats.json"""
    now = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+0800")
    stats = {"total": final_total, "removed": total_removed,
             "low_remaining": low_remaining, "last_cleanup": now}
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE) as f:
                existing = json.load(f)
                existing.update(stats)
                stats = existing
        except (json.JSONDecodeError, Exception):
            pass
    os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


def summarize_tags(mems: dict, threshold: float) -> dict:
    """统计低质条目的tag分布"""
    tag_counts = defaultdict(int)
    for uid, v in mems.items():
        q = v.get("quality", 0.5)
        if q >= threshold:
            continue
        tags_raw = v.get("tags", "")
        if isinstance(tags_raw, list):
            for t in tags_raw:
                tag_counts[t] += 1
        elif isinstance(tags_raw, str):
            for t in tags_raw.split(","):
                t = t.strip()
                if t:
                    tag_counts[t] += 1
        # also check from hindsight content via text_preview heuristics
        text = v.get("text_preview", "")
        if "skill" in text.lower() or "update" in text.lower():
            tag_counts["__hint:skill_update__"] += 1
        if "prefetch" in text.lower() or "捕获" in text.lower() or "duplicate" in text.lower():
            tag_counts["__hint:prefetch_cache__"] += 1
        if "操作日志" in text or "operation log" in text.lower():
            tag_counts["__hint:op_log__"] += 1

    return dict(sorted(tag_counts.items(), key=lambda x: -x[1])[:15])


def main():
    # 解析参数
    args = [a for a in sys.argv[1:] if not a.startswith("--")]  # strip bare args
    apply_mode = "--apply" in sys.argv
    dry_run = "--dry-run" in sys.argv
    threshold = DEFAULT_THRESHOLD

    for a in sys.argv[1:]:
        if a.startswith("--threshold="):
            threshold = float(a.split("=")[1])
        elif a == "--apply":
            pass  # already handled
        elif a == "--dry-run":
            pass  # already handled

    if not apply_mode:
        log(f"🔍 预览模式（加 --apply 实际执行）threshold={threshold}")

    # Step 1: 加载 quality.json
    data = load_quality()
    mems = data.get("memories", {})
    total_before = len(mems)

    log(f"memory_quality.json: {total_before} 条")

    # Step 2: 筛选低质条目
    low_quality = {}
    for uid, v in mems.items():
        q = v.get("quality", 0.5)
        status = v.get("status", "active")
        if q < threshold and status == "active":
            low_quality[uid] = v

    if not low_quality:
        log(f"✅ 未发现 quality < {threshold} 的活跃条目")
        return

    log(f"🎯 待清理: {len(low_quality)} 条 (q<{threshold})")

    # tag 分布统计
    tag_dist = summarize_tags(low_quality, threshold)
    if tag_dist:
        log(f"📊 主要标签分布:")
        for tag, cnt in list(tag_dist.items())[:10]:
            log(f"    {tag}: {cnt}")

    # Step 3: 检查 PG 中实际存在哪些
    pg_ids = get_pg_uids()
    in_pg = [uid for uid in low_quality if uid in pg_ids]
    not_in_pg = [uid for uid in low_quality if uid not in pg_ids]
    log(f"  PG 中存在: {len(in_pg)}, 已不存在(quality.json残留): {len(not_in_pg)}")

    if not in_pg and not apply_mode:
        log(f"  只有 quality.json 残留，加 --apply 清理 quality.json 即可")
    elif not in_pg:
        log(f"  只有 quality.json 残留，直接从 quality.json 移除")
        for uid in low_quality:
            mems.pop(uid, None)
        data["memories"] = mems
        save_quality(data)
        log(f"  ✅ quality.json 清理完成: {total_before} → {len(mems)}")

        # 更新统计数据
        low_remaining = sum(1 for v in mems.values()
                            if v.get("quality", 0.5) < threshold
                            and v.get("status") == "active")
        update_stats(len(low_quality), len(mems), low_remaining)
        log(f"  📊 更新 memory_stats.json")
        return

    if dry_run or not apply_mode:
        log(f"📋 预览汇总:")
        log(f"    总低质条目: {len(low_quality)}")
        log(f"    在 PG 中可删: {len(in_pg)}")
        log(f"    quality.json 残留: {len(not_in_pg)}")
        log(f"  加 --apply 执行实际清理")
        return

    # Step 4: 从 PG 删除
    log(f"🗑️  从 PG 删除 {len(in_pg)} 条...")
    deleted = delete_from_pg(in_pg)
    log(f"  PG 删除完成: {deleted} 条")

    # Step 5: 从 quality.json 移除（PG 存在的 + 残留的）
    all_to_remove = set(low_quality.keys())
    for uid in all_to_remove:
        mems.pop(uid, None)
    data["memories"] = mems
    save_quality(data)
    log(f"  quality.json 清理完成: {total_before} → {len(mems)}")

    # Step 6: 更新 statistics
    low_remaining = sum(1 for v in mems.values()
                        if v.get("quality", 0.5) < threshold
                        and v.get("status") == "active")
    update_stats(len(low_quality), len(mems), low_remaining)
    log(f"  📊 memory_stats.json 已更新")

    # Step 7: 验证
    pg_after = get_pg_uids()
    still_in_pg = [uid for uid in in_pg if uid in pg_after]
    if still_in_pg:
        log(f"⚠️  {len(still_in_pg)} 条仍存在于 PG（可能未删干净）")
    else:
        log(f"✅ PG 验证通过 — 所有目标条目已删除")

    log(f"✅ 归档完成: 共清理 {len(low_quality)} 条")


if __name__ == "__main__":
    main()
