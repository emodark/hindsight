#!/usr/bin/env python3
"""
记忆质量校准系统 — GSEM 算法轻量移植版

GSEM (Graph-based Self-Evolving Memory) 的核心公式：
  delta_t = 2*score - 1
  q_i <- clip(q_i + η_q * a_i * delta_t, 0, 1)
  θ_ij <- θ_ij + η_w * b_ij * delta_t

本脚本实现：
  1. 单条记忆质量校准（用 feedback 信号调整 quality）
  2. 批量记忆质量校准（多记忆排序后按 rank 分配信用）
  3. 纠正关联标记（旧记忆 → 新记忆的 corrects 关系）
  4. 质量数据持久化到 memory_quality.json

用法:
  # 确认一条记忆正确（提高质量）
  python3 calibrate_memory.py confirm <memory_id> [--delta 0.1]
  
  # 标记一条记忆被纠正（降低质量）
  python3 calibrate_memory.py correct <memory_id> [--delta 0.2] [--correction-id <new_memory_id>]
  
  # 批量调整：成功时提高一批记忆质量
  python3 calibrate_memory.py batch --success --ids <id1,id2,...>

  # 查看质量分布
  python3 calibrate_memory.py stats

  # 搜索记忆ID（按文本片段）
  python3 calibrate_memory.py search <text_fragment>
"""
import fcntl
import json
import math
import os
import sys
import time
from urllib.request import Request, urlopen

# ── Config ──────────────────────────────────────────────────────────────────
QUALITY_FILE = os.path.expanduser("~/.hermes/hindsight/memory_quality.json")
API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"

# GSEM 超参数
ETA_Q0 = 0.1       # 节点质量学习率
ETA_W0 = 0.05      # 边权重学习率
RHO = 0.8          # 衰减指数
DEFAULT_QUALITY = 0.5  # 新记忆默认质量


# ── 数据层 ──────────────────────────────────────────────────────────────────

def load_quality() -> dict:
    """加载质量文件。"""
    if os.path.exists(QUALITY_FILE):
        with open(QUALITY_FILE) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
            except OSError:
                pass
            data = json.load(f)
        return data.get("memories", {})
    return {}


def save_quality(quality: dict):
    """保存质量文件。"""
    data = {
        "version": 2,
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "memories": quality,
    }
    with open(QUALITY_FILE, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
        except OSError:
            pass
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch_memory_by_id(memory_id: str) -> dict | None:
    """从 hindsight API 获取单条记忆。"""
    url = f"{API_BASE}/memories/{memory_id}"
    try:
        resp = urlopen(url, timeout=10)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def search_memory(text_fragment: str, limit: int = 20) -> list[dict]:
    """按文本片段搜索记忆（通过 recall API）。"""
    url = f"{API_BASE}/memories/recall"
    payload = json.dumps({"query": text_fragment, "limit": limit}).encode()
    try:
        req = Request(url, data=payload,
                       headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        return data.get("results", data if isinstance(data, list) else [])
    except Exception as e:
        print(f"  ⚠️  搜索失败: {e}")
        return []


# ── GSEM 核心算法 ───────────────────────────────────────────────────────────

def gsem_update_single(
    current_q: float,
    delta_t: float,
    visit_count: int = 0,
    eta_q: float | None = None,
) -> float:
    """
    GSEM 单条记忆质量更新。

    公式:
      q_i(t+1) = clip(q_i(t) + η_q * delta_t, 0, 1)
    
    其中 delta_t = 2*score - 1（score=1 成功/score=0 失败）
    简化: delta_t = ±0.2（微调）/ ±0.5（大幅调整）
    
    Args:
        current_q: 当前质量 (0-1)
        delta_t: 反馈信号 (-1 到 +1)
        visit_count: 访问次数（用于衰减学习率）
        eta_q: 学习率（None=自动根据 visit_count 衰减）
    
    Returns:
        更新后的质量 (0-1)
    """
    # 学习率衰减：被校准越多次，变化越小
    if eta_q is None:
        eta_q = ETA_Q0 / (1 + visit_count) ** RHO
    
    new_q = current_q + eta_q * delta_t
    return max(0.0, min(1.0, new_q))


def gsem_update_batch(
    quality: dict,
    memory_ids: list[str],
    success: bool,
):
    """
    GSEM 批量记忆质量更新（带 rank 衰减信用分配）。

    公式:
      a_i = (1/log(2+rank_i)) / sum_k(1/log(2+rank_k))
      q_i <- clip(q_i + η_q * a_i * delta_t, 0, 1)
    
    Args:
        quality: 质量数据字典
        memory_ids: 按排名排列的记忆 ID 列表（rank 0 = 最相关）
        success: True=成功, False=失败
    """
    if not memory_ids:
        return
    
    delta_t = 1.0 if success else -1.0
    
    # 过滤出存在于 quality 中的 ID
    valid_ids = [mid for mid in memory_ids if mid in quality]
    if not valid_ids:
        return
    
    K = valid_ids
    # rank-decay credit: a_i = 1/log(2+rank_i)
    raw_scores = {eid: 1.0 / math.log(2 + rank) for rank, eid in enumerate(K)}
    total = sum(raw_scores.values())
    if total == 0:
        return
    a = {eid: raw_scores[eid] / total for eid in K}
    
    updated = 0
    for eid in K:
        entry = quality[eid]
        n_i = entry.get("visit_count", 0)
        eta_q = ETA_Q0 / (1 + n_i) ** RHO
        
        old_q = entry["quality"]
        new_q = gsem_update_single(old_q, a[eid] * delta_t, n_i, eta_q)
        entry["quality"] = round(new_q, 4)
        entry["visit_count"] = n_i + 1
        updated += 1
    
    return updated


# ── CLI 命令 ────────────────────────────────────────────────────────────────

def cmd_confirm(memory_id: str, delta: float = 0.1):
    """确认一条记忆正确，提高质量。"""
    quality = load_quality()
    
    if memory_id not in quality:
        # 尝试从 API 获取
        mem = fetch_memory_by_id(memory_id)
        if mem:
            quality[memory_id] = {
                "quality": DEFAULT_QUALITY,
                "status": "active",
                "visit_count": 0,
                "text_preview": (mem.get("text", "") or "")[:100],
                "tags": mem.get("tags", [])[:5],
            }
        else:
            print(f"❌ 记忆 {memory_id[:16]}... 不存在")
            sys.exit(1)
    
    entry = quality[memory_id]
    old_q = entry["quality"]
    new_q = gsem_update_single(old_q, delta, entry.get("visit_count", 0))
    entry["quality"] = round(new_q, 4)
    entry["visit_count"] = entry.get("visit_count", 0) + 1
    entry["status"] = "indication" if new_q >= 0.5 else entry.get("status", "active")
    
    save_quality(quality)
    text = entry.get("text_preview", "")[:60]
    print(f"✅  确认: [{memory_id[:16]}] {text}")
    print(f"   质量: {old_q:.3f} → {new_q:.3f} (Δ={delta:+.2f})")


def cmd_correct(memory_id: str, delta: float = 0.2, correction_id: str | None = None):
    """标记一条记忆被纠正，降低质量。"""
    quality = load_quality()
    
    if memory_id not in quality:
        mem = fetch_memory_by_id(memory_id)
        if mem:
            quality[memory_id] = {
                "quality": DEFAULT_QUALITY,
                "status": "active",
                "visit_count": 0,
                "text_preview": (mem.get("text", "") or "")[:100],
                "tags": mem.get("tags", [])[:5],
            }
        else:
            print(f"❌ 记忆 {memory_id[:16]}... 不存在")
            sys.exit(1)
    
    entry = quality[memory_id]
    old_q = entry["quality"]
    new_q = gsem_update_single(old_q, -delta, entry.get("visit_count", 0))
    entry["quality"] = round(new_q, 4)
    entry["visit_count"] = entry.get("visit_count", 0) + 1
    entry["status"] = "contraindication"
    
    # 记录纠正关联
    if correction_id:
        entry["corrected_by"] = correction_id
        # 在新记忆上标记
        if correction_id in quality:
            quality[correction_id]["status"] = "indication"
            quality[correction_id]["corrects"] = memory_id
            # 新记忆质量提高
            q_old = quality[correction_id]["quality"]
            quality[correction_id]["quality"] = round(
                gsem_update_single(q_old, 0.15, quality[correction_id].get("visit_count", 0)), 4
            )
    
    save_quality(quality)
    text = entry.get("text_preview", "")[:60]
    print(f"🔻 纠正: [{memory_id[:16]}] {text}")
    print(f"   质量: {old_q:.3f} → {new_q:.3f} (Δ={-delta:.2f})")
    print(f"   状态: contraindication")
    if correction_id:
        print(f"   新记忆: {correction_id[:16]}")


def cmd_search(text: str):
    """搜索记忆并显示 ID 和质量。"""
    quality = load_quality()
    results = search_memory(text)
    
    if not results:
        print(f"未找到包含「{text}」的记忆")
        return
    
    print(f"搜索「{text}」找到 {len(results)} 条:\n")
    for r in results[:10]:
        mid = r.get("id", "")[:16]
        txt = (r.get("text", "") or "")[:80]
        q_entry = quality.get(mid if len(mid) == 16 else r.get("id", ""), {})
        q_val = q_entry.get("quality", "?")
        status = q_entry.get("status", "active")
        print(f"  [{mid}] q={q_val} [{status}] {txt}")


def cmd_stats():
    """显示质量分布统计。"""
    quality = load_quality()
    if not quality:
        print("质量数据为空")
        return
    
    q_values = [e["quality"] for e in quality.values()]
    avg_q = sum(q_values) / len(q_values) if q_values else 0
    
    # 按质量区间统计
    buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
    for q in q_values:
        if q < 0.2: buckets["0.0-0.2"] += 1
        elif q < 0.4: buckets["0.2-0.4"] += 1
        elif q < 0.6: buckets["0.4-0.6"] += 1
        elif q < 0.8: buckets["0.6-0.8"] += 1
        else: buckets["0.8-1.0"] += 1
    
    # 状态统计
    statuses = {}
    for e in quality.values():
        s = e.get("status", "active")
        statuses[s] = statuses.get(s, 0) + 1
    
    print(f"📊 记忆质量分布")
    print(f"   总条目: {len(quality)}")
    print(f"   平均质量: {avg_q:.3f}")
    print(f"\n   质量区间:")
    for bucket, count in buckets.items():
        bar = "█" * max(1, count * 40 // len(quality))
        print(f"     {bucket}: {count:4d} {bar}")
    print(f"\n   状态分布: {statuses}")
    
    # 最低/最高质量的条目
    sorted_by_q = sorted(quality.items(), key=lambda x: x[1]["quality"])
    print(f"\n   最低质量 TOP 3:")
    for mid, entry in sorted_by_q[:3]:
        print(f"     [{mid[:16]}] q={entry['quality']:.3f} {entry.get('text_preview','')[:50]}")
    print(f"\n   最高质量 TOP 3:")
    for mid, entry in sorted_by_q[-3:]:
        print(f"     [{mid[:16]}] q={entry['quality']:.3f} {entry.get('text_preview','')[:50]}")


def cmd_batch(success: bool, ids: list[str]):
    """批量更新一组记忆质量。"""
    quality = load_quality()
    
    valid = [mid for mid in ids if mid in quality]
    if not valid:
        print("❌ 没有有效的记忆 ID")
        # 显示前几个 ID 以帮助调试
        sample = list(quality.keys())[:3]
        print(f"   质量文件中样本 ID: {[s[:16] for s in sample]}")
        return
    
    count = gsem_update_batch(quality, valid, success)
    if count:
        save_quality(quality)
        print(f"✅ 批量更新 {count} 条记忆，{'成功(+δ)' if success else '失败(-δ)'}")


# ── 入口 ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    cmd = sys.argv[1]
    
    if cmd == "search" and len(sys.argv) >= 3:
        cmd_search(sys.argv[2])
    elif cmd == "stats":
        cmd_stats()
    elif cmd == "confirm" and len(sys.argv) >= 3:
        delta = 0.1
        if "--delta" in sys.argv:
            idx = sys.argv.index("--delta")
            if idx + 1 < len(sys.argv):
                delta = float(sys.argv[idx + 1])
        cmd_confirm(sys.argv[2], delta)
    elif cmd == "correct" and len(sys.argv) >= 3:
        delta = 0.2
        correction_id = None
        if "--delta" in sys.argv:
            idx = sys.argv.index("--delta")
            if idx + 1 < len(sys.argv):
                delta = float(sys.argv[idx + 1])
        if "--correction-id" in sys.argv:
            idx = sys.argv.index("--correction-id")
            if idx + 1 < len(sys.argv):
                correction_id = sys.argv[idx + 1]
        cmd_correct(sys.argv[2], delta, correction_id)
    elif cmd == "batch":
        success = "--success" in sys.argv
        if "--ids" in sys.argv:
            idx = sys.argv.index("--ids")
            if idx + 1 < len(sys.argv):
                ids = sys.argv[idx + 1].split(",")
                cmd_batch(success, ids)
            else:
                print("--ids 需要提供逗号分隔的 ID 列表")
        else:
            print("batch 模式需要 --ids <id1,id2,...>")
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
