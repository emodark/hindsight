#!/usr/bin/env python3
"""
校准执行器 v3 — 三信号源: 交叉验证 + 用户纠正/确认 + Dreaming 怀疑清单

策略:
  1. 交叉验证 → 对 API 中全部记忆做主动质检（specificity/verifiability/connectedness/矛盾检测）
     → 产生基础质量分，覆盖默认 0.5
  2. 旧策略：用户纠正/确认事件 → GSEM 微调
  3. Dreaming 怀疑清单 → 从 ~/.hermes/hindsight/dreaming_doubt_list.json 读取用户确认结果

用法：
  python3 calibrate_runner.py              # 完整运行
  python3 calibrate_runner.py --dry-run    # 预览
  python3 calibrate_runner.py stats        # 只看统计
  python3 calibrate_runner.py --cross-only # 只做交叉验证
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

HOMES_DIR = os.path.expanduser("~/.hermes")
API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"
CALIBRATE = os.path.join(HOMES_DIR, "hindsight", "calibrate_memory.py")
QUALITY_FILE = os.path.join(HOMES_DIR, "hindsight", "memory_quality.json")
CROSS_VALIDATE = os.path.join(HOMES_DIR, "hindsight", "cross_validate_memories.py")
DOUBT_LIST = os.path.join(HOMES_DIR, "hindsight", "dreaming_doubt_list.json")


def api_get(path: str, params: str = "") -> dict:
    """GET hindsight API."""
    url = f"{API_BASE}{path}?{params}" if params else f"{API_BASE}{path}"
    try:
        resp = urlopen(url, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def api_post(path: str, data: dict) -> dict:
    """POST hindsight API."""
    url = f"{API_BASE}{path}"
    try:
        payload = json.dumps(data).encode()
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def run_calibrate(*args: str) -> tuple[str, int]:
    """Run calibrate_memory.py with args, return (output, exit_code)."""
    try:
        r = subprocess.run(
            ["python3", CALIBRATE, *args],
            capture_output=True, text=True, timeout=30
        )
        return r.stdout or r.stderr, r.returncode
    except Exception as e:
        return str(e), -1


def run_cross_validate() -> dict:
    """运行交叉验证脚本，返回统计。"""
    try:
        r = subprocess.run(
            ["python3", CROSS_VALIDATE, "--bootstrap"],
            capture_output=True, text=True, timeout=120
        )
        output = r.stdout or r.stderr
        print(output)  # 透传输出
        return {"exit_code": r.returncode, "output": output[:500]}
    except Exception as e:
        print(f"  ⚠️  交叉验证执行失败: {e}")
        return {"exit_code": -1, "error": str(e)}


def find_corrections(limit: int = 200) -> list[dict]:
    """扫描 correction/confirmation 信号。"""
    results = []
    tag_resp = api_get("/memories/list", f"limit={limit}")
    all_items = tag_resp.get("items", tag_resp.get("results", []))

    for item in all_items:
        tags = item.get("tags", [])
        text = (item.get("text", "") or "").lower()

        has_negative_tag = (
            "correction" in tags
            or "correct" in tags
            or "correction_id" in tags
        )
        has_correction_text = (
            "纠正" in text
            or "correction" in text
            or "user corrected" in text
        )
        is_strengthening = has_correction_text and not has_negative_tag
        is_confirmation = (
            "confirmation" in tags
            or "confirmed" in tags
            or item.get("proof_count", 0) >= 3
        )

        if has_negative_tag:
            results.append({
                "id": item.get("id", ""),
                "text": (item.get("text", "") or "")[:120],
                "tags": tags,
                "proof_count": item.get("proof_count", 0),
                "type": "correction",
            })
        elif is_strengthening:
            results.append({
                "id": item.get("id", ""),
                "text": (item.get("text", "") or "")[:120],
                "tags": tags,
                "proof_count": item.get("proof_count", 0),
                "type": "confirmation",
            })
        elif is_confirmation:
            results.append({
                "id": item.get("id", ""),
                "text": (item.get("text", "") or "")[:120],
                "tags": tags,
                "proof_count": item.get("proof_count", 0),
                "type": "confirmation",
            })

    return results


def calibrate_corrections(events: list[dict], dry_run: bool = False) -> dict:
    """对检测到的纠正/确认事件执行校准。"""
    stats = {"corrected": 0, "confirmed": 0, "skipped": 0, "errors": []}

    for event in events:
        mid = event["id"]
        etype = event["type"]

        if not mid or len(mid) < 8:
            stats["skipped"] += 1
            continue

        if dry_run:
            print(f"  [DRY-RUN] {'correct' if etype == 'correction' else 'confirm'} {mid[:16]}...")
            continue

        cmd = "correct" if etype == "correction" else "confirm"
        output, code = run_calibrate(cmd, mid)
        if code == 0:
            stats[cmd + "ed" if cmd == "correct" else cmd + "ed"] += 1
        else:
            stats["errors"].append(f"{mid[:16]}: {output[:100]}")

    return stats


def consume_dreaming_doubt_list(dry_run: bool = False) -> dict:
    """读取 Dreaming 怀疑清单的用户确认结果，应用校准。

    dreaming_doubt_list.json 格式：
    {
        "confirmed": ["id1", "id2", ...],    # 用户说"对的，没问题" → 确认升权
        "corrected": ["id3", "id4", ...],     # 用户说"这条错了" → 纠正降权
    }
    """
    stats = {"confirmed": 0, "corrected": 0, "skipped": 0}
    if not os.path.exists(DOUBT_LIST):
        return stats

    try:
        with open(DOUBT_LIST) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return stats

    confirmed = data.get("confirmed", [])
    corrected = data.get("corrected", [])
    if not confirmed and not corrected:
        return stats

    if dry_run:
        print(f"  [DRY-RUN] Dreaming结果: 确认{len(confirmed)}条, 纠正{len(corrected)}条")
        return stats

    for mid in confirmed:
        output, code = run_calibrate("confirm", mid)
        if code == 0:
            stats["confirmed"] += 1

    for mid in corrected:
        output, code = run_calibrate("correct", mid)
        if code == 0:
            stats["corrected"] += 1

    return stats


def get_quality_stats() -> dict:
    """读取 memory_quality.json 的统计。"""
    if not os.path.exists(QUALITY_FILE):
        return {"error": "quality file not found"}
    try:
        with open(QUALITY_FILE) as f:
            data = json.load(f)
        memories = data.get("memories", {})
        qs = [v["quality"] for v in memories.values()]
        statuses = {}
        for v in memories.values():
            s = v.get("status", "active")
            statuses[s] = statuses.get(s, 0) + 1
        return {
            "total": len(memories),
            "avg_quality": round(sum(qs) / len(qs), 4) if qs else 0,
            "unique_qualities": len(set(round(q, 4) for q in qs)),
            "min_q": round(min(qs), 4) if qs else 0,
            "max_q": round(max(qs), 4) if qs else 0,
            "buckets": {
                "0-0.2": sum(1 for q in qs if q < 0.2),
                "0.2-0.4": sum(1 for q in qs if 0.2 <= q < 0.4),
                "0.4-0.6": sum(1 for q in qs if 0.4 <= q < 0.6),
                "0.6-0.8": sum(1 for q in qs if 0.6 <= q < 0.8),
                "0.8-1.0": sum(1 for q in qs if q >= 0.8),
            },
            "statuses": statuses,
        }
    except Exception as e:
        return {"error": str(e)}


def main():
    dry_run = "--dry-run" in sys.argv
    cross_only = "--cross-only" in sys.argv

    if "stats" in sys.argv:
        print(json.dumps(get_quality_stats(), indent=2))
        return 0

    print(f"{'🔄 校准执行器 v3' if not dry_run else '🔄 校准执行器 v3 [DRY RUN]'}")
    print(f"时间: {datetime.now(timezone.utc).isoformat()}\n")

    # Step 0: 运行前统计
    before = get_quality_stats()
    print(f"📊 校准前:")
    print(f"   总条目: {before['total']} | 平均质量: {before['avg_quality']}")
    print(f"   唯一质量分数量: {before['unique_qualities']}")
    print(f"   状态分布: {before['statuses']}\n")

    # Step 1: 交叉验证（主要信号源）
    print("🔍 Step 1: 交叉验证 — 主动质检所有记忆...")
    if not dry_run:
        cv_result = run_cross_validate()
        if cv_result["exit_code"] != 0:
            print(f"   ⚠️  交叉验证异常，继续后续步骤")
    else:
        print("   [DRY-RUN] 跳过实际执行")
    print()

    # Step 2: 用户纠正/确认事件（次要信号源）
    print("🔍 Step 2: 扫描用户纠正/确认事件...")
    events = find_corrections(limit=500)
    corrections = [e for e in events if e["type"] == "correction"]
    confirmations = [e for e in events if e["type"] == "confirmation"]
    print(f"   找到 {len(corrections)} 条纠正事件, {len(confirmations)} 条确认事件")

    if corrections or confirmations:
        print(f"   {'[DRY-RUN]' if dry_run else ''} 校准中...")
        if corrections:
            s = calibrate_corrections(corrections, dry_run)
            print(f"   纠正: {s['corrected']} 成功, {s['skipped']} 跳过")
            if s["errors"]:
                for e in s["errors"][:3]:
                    print(f"     ⚠️  {e}")
        if confirmations:
            s = calibrate_corrections(confirmations, dry_run)
            print(f"   确认: {s['confirmed']} 成功, {s['skipped']} 跳过")
            if s["errors"]:
                for e in s["errors"][:3]:
                    print(f"     ⚠️  {e}")
    print()

    # Step 3: Dreaming 怀疑清单（增量信号源）
    print("🔍 Step 3: 消费Dreaming怀疑清单...")
    ds = consume_dreaming_doubt_list(dry_run)
    print(f"   确认 {ds['confirmed']} 条, 纠正 {ds['corrected']} 条\n")

    # Step 4: 运行后统计
    after = get_quality_stats()
    print(f"📊 校准后:")
    print(f"   总条目: {after['total']} | 平均质量: {after['avg_quality']}")
    print(f"   唯一质量分数量: {after['unique_qualities']}")
    print(f"   状态分布: {after['statuses']}")
    print(f"   质量分布: {after['buckets']}\n")

    # 汇总变化
    diff_unique = after["unique_qualities"] - before["unique_qualities"]
    diff_total = after["total"] - before["total"]
    changes = []
    if diff_unique > 0:
        changes.append(f"质量分值 +{diff_unique}")
    if diff_total != 0:
        changes.append(f"总量 {diff_total:+d}")
    if changes:
        print(f"✅ 校准生效: {', '.join(changes)}")
    else:
        print(f"ℹ️ 本次无变化")
    print(f"   🏆 高置信(>0.6): {after['buckets']['0.6-0.8']}")
    print(f"   ⚠️ 低置信(<0.4): {after['buckets']['0.2-0.4'] + after['buckets']['0-0.2']}")

    # 写入运行日志
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cross_validate": "ok",
        "corrections_found": len(corrections),
        "confirmations_found": len(confirmations),
        "dreaming_confirmed": ds["confirmed"],
        "dreaming_corrected": ds["corrected"],
        "before": {
            "avg": before["avg_quality"],
            "unique": before["unique_qualities"],
            "total": before["total"],
        },
        "after": {
            "avg": after["avg_quality"],
            "unique": after["unique_qualities"],
            "total": after["total"],
        },
    }
    log_path = os.path.join(HOMES_DIR, "self", "calibration_log.jsonl")
    with open(log_path, "a") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
