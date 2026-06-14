#!/usr/bin/env python3
"""
每周矛盾自动清理脚本 — 被 cron 调度。
功能：
  1. 运行 cross_validate_memories.py 全量扫描（含实体过滤+时间感知）
  2. 自动确认非交易实体和时序快照（不会真的降权）
  3. 报告剩下的真正矛盾供人工审视
  4. 清理过期的怀疑清单条目
"""
import json, os, sys, subprocess, datetime, re
from collections import defaultdict
from urllib.request import urlopen

HOMES_DIR = os.path.expanduser("~/.hermes")
HINDSIGHT_DIR = os.path.join(HOMES_DIR, "hindsight")
QUALITY_FILE = os.path.join(HINDSIGHT_DIR, "memory_quality.json")
CROSS_VALIDATE = os.path.join(HINDSIGHT_DIR, "cross_validate_memories.py")
CALIBRATE = os.path.join(HINDSIGHT_DIR, "calibrate_memory.py")
DECAY_SCRIPT = os.path.join(HINDSIGHT_DIR, "tools", "memory_decay.py")
DOUBT_FILE = os.path.join(HINDSIGHT_DIR, "dreaming_doubt_list.json")
LOG_FILE = os.path.join(HINDSIGHT_DIR, "logs", "weekly_cleanup.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"

RE_NEGATION = re.compile(r'(已清仓|已卖出|已平仓|不再持有|清掉|不再关注|错误|错的|不正确|过期)')
RE_AFFIRMATION = re.compile(r'(持有|加仓|买入|建仓|持仓|看好|不错|正确|确认|已验证)')


def log(msg):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def run():
    log("=" * 60)
    log("🔍 每周记忆维护开始（矛盾清理 + 遗忘冷却）")

    # Step 0: 记忆衰减（遗忘机制）
    log("Step 0: 记忆衰减（遗忘冷却）...")
    decay_result = subprocess.run(
        [sys.executable, DECAY_SCRIPT],
        capture_output=True, text=True, timeout=120
    )
    decay_stdout = decay_result.stdout or ""
    decay_stderr = decay_result.stderr or ""
    for line in decay_stdout.split("\n"):
        if line.strip() and ("💧" in line or "平均" in line or "已衰减" in line
                              or "触达" in line or "分类" in line):
            log(f"  {line.strip()}")
    if decay_result.returncode != 0:
        log(f"  ⚠️ decay 脚本异常: {decay_stderr[:200]}")

    # Step 1: 运行 cross_validate --suspicions 获取最新报告
    log("Step 1: 运行矛盾检测...")
    result = subprocess.run(
        [sys.executable, CROSS_VALIDATE, "--suspicions"],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        log(f"  ⚠️ cross_validate 返回非零: {result.returncode}")
        log(f"  stderr: {result.stderr[-500:]}")

    output = result.stdout

    # 解析关键指标
    lines = output.split("\n")
    contra_count = 0
    temporal_skipped = 0
    trading_groups = 0
    total_contra_memories = 0

    for line in lines:
        if "发现" in line and "条记忆涉及矛盾" in line:
            try:
                total_contra_memories = int(line.split("发现")[1].split("条")[0].strip())
            except (ValueError, IndexError):
                pass
        if "最终矛盾对" in line:
            try:
                contra_count = int(line.split(":")[1].strip())
            except (ValueError, IndexError):
                pass
        if "时间感知过滤" in line:
            try:
                temporal_skipped = int(line.split(":")[1].split("对")[0].strip())
            except (ValueError, IndexError):
                pass
        if "交易实体组" in line:
            try:
                trading_groups = int(line.split(":")[1].split(",")[0].strip())
            except (ValueError, IndexError):
                pass

    log(f"   矛盾对: {contra_count} | 涉及记忆: {total_contra_memories} | "
        f"时序过滤: {temporal_skipped} | 交易实体: {trading_groups}")

    # Step 2: 获取前一天的报告作对比
    prev_log = None
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            all_lines = f.readlines()
        for i in range(len(all_lines) - 1, -1, -1):
            if "最终矛盾对" in all_lines[i] and all_lines[i] != lines:
                prev_log = all_lines[i]
                break

    if prev_log:
        log(f"   上周对比: {prev_log.strip()}")

    # Step 3: 如果矛盾数较低（<=20），自动确认所有 suspect（已由算法保证质量）
    # 如果矛盾数较高，分类处理
    if total_contra_memories <= 5:
        log("Step 2: 矛盾数极少，无需额外清理")
    elif total_contra_memories <= 20:
        log(f"Step 2: 矛盾数 {total_contra_memories} 条，数量适中，保留待观察趋势")
    else:
        log(f"Step 2: 矛盾数 {total_contra_memories} 条偏高，建议人工审视")
        log(f"   执行: python3 {CROSS_VALIDATE} --suspicions 查看详情")

    # Step 4: 检查质量分布趋势
    if os.path.exists(QUALITY_FILE):
        with open(QUALITY_FILE) as f:
            quality = json.load(f).get("memories", {})
        q_values = list(quality.values())
        avg_q = sum(q.get("quality", 0.5) for q in q_values) / max(len(q_values), 1)
        low_count = sum(1 for q in q_values if isinstance(q, dict) and q.get("quality", 0.5) < 0.25)
        total = len(q_values)
        log(f"   质量分布: avg={avg_q:.3f}, 低质<0.25: {low_count}/{total} ({low_count/max(total,1)*100:.1f}%)")

    # Step 5: 清理过期怀疑清单（超过14天未处理的条目自动丢弃）
    if os.path.exists(DOUBT_FILE):
        with open(DOUBT_FILE) as f:
            try:
                doubt = json.load(f)
            except json.JSONDecodeError:
                doubt = {}
        if isinstance(doubt, dict) and "entries" in doubt:
            entries = doubt["entries"]
            now = datetime.datetime.now()
            expired_entries = []
            kept_entries = []
            for entry in entries:
                date_str = entry.get("date", "")
                if date_str:
                    try:
                        entry_date = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        days_old = (now - entry_date).days
                        if days_old > 14:
                            expired_entries.append((entry.get("id", "?")[:16], days_old))
                        else:
                            kept_entries.append(entry)
                    except (ValueError, TypeError):
                        kept_entries.append(entry)
                else:
                    kept_entries.append(entry)
            if expired_entries:
                doubt["entries"] = kept_entries
                with open(DOUBT_FILE, "w") as f:
                    json.dump(doubt, f, indent=2, ensure_ascii=False)
                log(f"   清理过期怀疑条目: {len(expired_entries)} 条")
                for mid, days in expired_entries[:5]:
                    log(f"     移除: {mid} ({days}天前)")
                if len(expired_entries) > 5:
                    log(f"     ... 还有 {len(expired_entries)-5} 条")
            else:
                log(f"   怀疑清单 {len(entries)} 条，无过期条目")
        else:
            log(f"   怀疑清单文件存在但格式异常 ({len(str(doubt))} 字节)")

    # Step 6: 记录本摘要到统计文件
    stats = {
        "date": datetime.datetime.now().isoformat(),
        "contradiction_pairs": contra_count,
        "contradiction_memories": total_contra_memories,
        "temporal_skipped": temporal_skipped,
        "trading_groups": trading_groups,
    }
    stats_file = os.path.join(HINDSIGHT_DIR, "logs", "contradiction_trend.json")
    history = []
    if os.path.exists(stats_file):
        with open(stats_file) as f:
            try:
                history = json.load(f)
            except json.JSONDecodeError:
                history = []
    history.append(stats)
    # 只保留最近 12 周
    history = history[-12:]
    with open(stats_file, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

    log("=" * 60)
    return total_contra_memories, contra_count, temporal_skipped


if __name__ == "__main__":
    total_contra, pairs, skipped = run()
    # 最终输出给 cron 推送
    print(f"\n📊 本周清理报告")
    print(f"   矛盾记忆: {total_contra} 条")
    print(f"   矛盾对: {pairs} 对")
    print(f"   时序过滤: {skipped} 对")
    if total_contra <= 5:
        print(f"   ✅ 状态良好，无需操作")
    elif total_contra <= 20:
        print(f"   ⚠️ 一般水平，下周期观察趋势")
    else:
        print(f"   ❗ 偏高 ({total_contra}条)，建议审视:\n"
              f"      python3 {CROSS_VALIDATE} --suspicions")
