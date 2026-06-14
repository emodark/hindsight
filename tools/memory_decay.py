#!/usr/bin/env python3
"""
记忆衰减引擎 — 遗忘机制核心。

实现「远期记忆逐渐冷却」而非删除：
- 未被访问的记忆随时间衰减质量分
- 频繁访问和重要记忆衰减更慢（保留更久）
- 只降温不删除，降到最低分 0.12 后不再下降
- 集成到每周清理 cron 中执行
"""
import json, os, sys, subprocess
from datetime import datetime, timezone
from urllib.request import urlopen

HOMES_DIR = os.path.expanduser("~/.hermes")
HINDSIGHT_DIR = os.path.join(HOMES_DIR, "hindsight")
QUALITY_FILE = os.path.join(HINDSIGHT_DIR, "memory_quality.json")
LOG_FILE = os.path.join(HINDSIGHT_DIR, "logs", "memory_decay.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# 半衰期基准（天）
BASE_HALFLIFE = 60
# 最低质量分（不低于此值，允许充分冷却但不归零）
MIN_QUALITY = 0.08
# 免衰减期（天）：这个天数内的记忆完全不衰减
GRACE_PERIOD_DAYS = 14

# 访问次数 → 半衰期倍率 (access_multiplier)
# 从未访问过的记忆衰减最慢（给予基础保护），频繁访问几乎不衰减
ACCESS_MULTIPLIERS = [
    (0, 2.0),    # 0次访问：2倍半衰期（基础保护，120天）
    (1, 4.0),    # 1次访问：4倍半衰期（240天，访问过的地位更高）
    (3, 8.0),    # 3+次访问：8倍（480天）
    (8, 16.0),   # 8+次访问：16倍（960天，几乎永驻）
    (15, 32.0),  # 15+次访问：32倍（1920天，重点记忆）
]

# 重要性标签 → 半衰期倍率 (importance_multiplier)
IMPORTANT_TAGS = [
    ("core", 4.0),
    ("critical", 4.0),
    ("key:", 4.0),
    ("entity|concept:core_", 3.0),
    ("stock", 2.0),
    ("strategy", 2.0),
    ("trading", 2.0),
    ("lesson", 2.0),
    ("principle", 2.0),
    ("rule", 2.0),
    ("methodology", 1.5),
    ("analysis", 1.5),
    ("method", 1.5),
    ("user_profile", 2.0),
]


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def pg_query(sql: str) -> list:
    """查询 PG 并返回行列表。"""
    env = os.environ.copy()
    env["PGPASSWORD"] = "hindsight"
    result = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-p", "5433", "-U", "hindsight", "-d", "hindsight",
         "-tA", "-F", "|", "-c", sql],
        capture_output=True, text=True, timeout=30, env=env
    )
    if result.returncode != 0:
        log(f"  ⚠️ PG 查询失败: {result.stderr[:200]}")
        return []
    lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
    return lines


def load_quality() -> dict:
    """加载 quality.json。"""
    with open(QUALITY_FILE) as f:
        return json.load(f)


def save_quality(data: dict):
    """保存 quality.json。"""
    data["last_updated"] = datetime.now().isoformat()
    with open(QUALITY_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_access_multiplier(access_count: int) -> float:
    """根据访问次数获取半衰期倍率。"""
    for threshold, mult in reversed(ACCESS_MULTIPLIERS):
        if access_count >= threshold:
            return mult
    return 1.0


def get_importance_multiplier(tags: list) -> float:
    """根据标签获取重要性倍率。"""
    mult = 1.0
    for tag_pattern, tag_mult in IMPORTANT_TAGS:
        for tag in tags:
            if tag.startswith(tag_pattern):
                mult = max(mult, tag_mult)
    return mult


def get_consolidated_tag(tags: list) -> str:
    """获取合并后的标签摘要。"""
    for t in tags:
        if t.startswith("entity|concept:core_"):
            return "core"
        if t.startswith("key:"):
            return "key_memory"
        if t.startswith("date:"):
            continue
    for t in tags:
        if t in ("core", "critical", "strategy", "trading"):
            return t
    return "general"


def run_decay(dry_run: bool = False) -> dict:
    """
    执行记忆衰减。

    Args:
        dry_run: True 则只输出不修改

    Returns:
        统计字典
    """
    if not os.path.exists(QUALITY_FILE):
        log(f"  ⚠️ quality.json 不存在")
        return {}

    # Step 1: 加载 quality.json
    data = load_quality()
    memories = data.get("memories", {})
    if not memories:
        log("  ⚠️ quality.json 为空")
        return {}

    # Step 2: 批量从 PG 获取记忆年龄和访问次数
    all_ids = list(memories.keys())
    log(f"    quality.json 共 {len(all_ids)} 条")

    # 分批查询 PG（避免 SQL 过长）
    pg_data = {}  # id -> {created_at, access_count}
    batch_size = 500
    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i:i+batch_size]
        id_list = ", ".join([f"'{uid}'" for uid in batch])
        sql = (
            f"SELECT id::text, created_at::text, access_count "
            f"FROM memory_units WHERE id IN ({id_list})"
        )
        rows = pg_query(sql)
        for row in rows:
            parts = row.split("|")
            if len(parts) >= 3:
                uid, created_at, acc_count = parts[0].strip(), parts[1].strip(), parts[2].strip()
                pg_data[uid] = {
                    "created_at": created_at,
                    "access_count": int(acc_count) if acc_count and acc_count != "\\N" else 0,
                }
        if (i+1) % 2000 == 0 or (i+1) >= len(all_ids):
            log(f"    已查询 PG: {min(i+batch_size, len(all_ids))}/{len(all_ids)}")

    log(f"    PG 匹配: {len(pg_data)}/{len(all_ids)}")

    # Step 3: 计算衰减
    now = datetime.now(timezone.utc)
    stats = {
        "total": len(memories),
        "matched": len(pg_data),
        "decayed": 0,
        "skipped_no_pg": 0,
        "clamped_to_floor": 0,
        "sum_quality_before": 0.0,
        "sum_quality_after": 0.0,
        "by_category": {},
        "slowest_decay": {"id": "", "halflife_days": 0},
        "fastest_decay": {"id": "", "halflife_days": 999},
    }

    decayed_memories = []

    for uid, entry in memories.items():
        old_q = entry.get("quality", 0.5)
        tags = entry.get("tags", [])
        status = entry.get("status", "indication")
        access_count = entry.get("visit_count", 0)

        q_before = old_q
        stats["sum_quality_before"] += q_before

        # 从 PG 获取数据
        pg_info = pg_data.get(uid)
        if pg_info:
            access_count = pg_info.get("access_count", access_count)
            created_at_str = pg_info.get("created_at", "")
        else:
            created_at_str = ""

        # 计算年龄
        age_days = 0
        if created_at_str and created_at_str != "\\N":
            try:
                created_dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                age_days = (now - created_dt).days
            except (ValueError, TypeError):
                age_days = 0

        # 如果 PG 无数据但 quality.json 有，用最小衰减（保守）
        if not pg_info:
            stats["skipped_no_pg"] += 1
            continue

        # 计算半衰期
        access_mult = get_access_multiplier(access_count)
        importance_mult = get_importance_multiplier(tags)
        halflife = BASE_HALFLIFE * access_mult * importance_mult

        # 衰减因子
        if age_days <= GRACE_PERIOD_DAYS:
            decay_factor = 1.0  # 14天内不衰减
        elif age_days <= 0:
            decay_factor = 1.0
        else:
            decay_factor = 2 ** (-age_days / halflife)

        new_q = max(old_q * decay_factor, MIN_QUALITY)

        # 更新
        if new_q < old_q and old_q > MIN_QUALITY:
            entry["quality"] = round(new_q, 4)
            entry["visit_count"] = access_count
            decayed_memories.append(uid)

        if new_q <= MIN_QUALITY and old_q > MIN_QUALITY:
            stats["clamped_to_floor"] += 1

        stats["sum_quality_after"] += entry["quality"]

        # 按分类统计
        cat = get_consolidated_tag(tags)
        if cat not in stats["by_category"]:
            stats["by_category"][cat] = {"count": 0, "sum_decay": 0.0}
        stats["by_category"][cat]["count"] += 1
        stats["by_category"][cat]["sum_decay"] += (old_q - new_q)

        # 极值追踪
        if halflife > stats["slowest_decay"]["halflife_days"]:
            stats["slowest_decay"] = {"id": uid[:16], "halflife_days": round(halflife, 1)}
        if halflife < stats["fastest_decay"]["halflife_days"] and age_days > 0:
            stats["fastest_decay"] = {"id": uid[:16], "halflife_days": round(halflife, 1)}

    stats["decayed"] = len(decayed_memories)

    # Step 4: 保存（非 dry_run）
    if not dry_run and stats["decayed"] > 0:
        save_quality(data)
        log(f"    ✅ 已写入 quality.json")

        # Step 4b: 写回到 PG（让 recall 管道能读取）
        pg_updates = 0
        update_batch_size = 200
        for i in range(0, len(decayed_memories), update_batch_size):
            batch = decayed_memories[i:i+update_batch_size]
            values = ", ".join([
                f"('{uid}', {memories[uid]['quality']})"
                for uid in batch
                if uid in memories
            ])
            if not values:
                continue
            sql = (
                f"UPDATE memory_units SET decayed_quality = v.quality "
                f"FROM (VALUES {values}) AS v(id, quality) "
                f"WHERE memory_units.id = v.id::uuid"
            )
            r = pg_query(sql)
            if r is not None:  # SQL executed
                pg_updates += len(batch)
        log(f"    ✅ 已写入 PG: {pg_updates} 条")

    elif dry_run:
        log(f"    🔍 DRY RUN 模式，未写入")

    # 最终统计
    avg_before = stats["sum_quality_before"] / max(stats["total"], 1)
    avg_after = stats["sum_quality_after"] / max(stats["total"], 1)
    stats["avg_quality_before"] = round(avg_before, 4)
    stats["avg_quality_after"] = round(avg_after, 4)
    stats["avg_decay"] = round(avg_before - avg_after, 4)

    return stats


def print_report(stats: dict):
    """打印人类可读报告。"""
    print(f"\n{'='*50}")
    print(f"🧊 记忆衰减报告")
    print(f"{'='*50}")
    print(f"  总记忆: {stats.get('total', 0)}")
    print(f"  PG 匹配: {stats.get('matched', 0)}")
    print(f"  已衰减: {stats.get('decayed', 0)} 条")
    print(f"  触及最低分: {stats.get('clamped_to_floor', 0)} 条")
    print(f"  无 PG 记录: {stats.get('skipped_no_pg', 0)} 条")
    print()
    print(f"  衰减前平均质量: {stats.get('avg_quality_before', 0):.4f}")
    print(f"  衰减后平均质量: {stats.get('avg_quality_after', 0):.4f}")
    print(f"  平均衰减量: {stats.get('avg_decay', 0):.4f}")
    print()
    print(f"  最慢衰减记忆: {stats.get('slowest_decay', {}).get('id', '-')} "
          f"(半衰期 {stats.get('slowest_decay', {}).get('halflife_days', '-')}天)")
    print(f"  最快衰减记忆: {stats.get('fastest_decay', {}).get('id', '-')} "
          f"(半衰期 {stats.get('fastest_decay', {}).get('halflife_days', '-')}天)")
    print()
    print(f"  按分类:")
    for cat, info in sorted(stats.get("by_category", {}).items(), key=lambda x: x[1]["count"], reverse=True):
        print(f"    {cat:15s}: {info['count']:5d} 条, 共衰减 {info['sum_decay']:.2f} 分")
    print(f"{'='*50}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="记忆衰减引擎")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不写入")
    args = parser.parse_args()

    log("🧊 记忆衰减开始")
    if args.dry_run:
        log("   DRY RUN 模式")

    stats = run_decay(dry_run=args.dry_run)
    if stats:
        print_report(stats)

    # 保存统计到趋势文件
    trend_file = os.path.join(HINDSIGHT_DIR, "logs", "decay_trend.json")
    history = []
    if os.path.exists(trend_file):
        with open(trend_file) as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []
    stats["date"] = datetime.now().isoformat()
    history.append({k: v for k, v in stats.items() if not isinstance(v, dict) and not isinstance(v, list)})
    history = history[-52:]  # 保留一年
    with open(trend_file, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    log("🧊 记忆衰减完成")


if __name__ == "__main__":
    main()
