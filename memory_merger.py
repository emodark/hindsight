#!/usr/bin/env python3
"""
记忆合并压缩工具 — 将同一主题的低质记忆合并为一条高质摘要。

痛点：系统中大量 auto-retained 记忆（~6,700条）用不同措辞描述同一主题，
每条 specificity 低（平均0.53），独立看价值不高。

策略：按 entity|object:xxx 标签分组 → 每组 ≥3 条且 q<0.6 → LLM 合并为1条
       → 写入合并版 → 删除原版

Usage:
    python3 memory_merger.py scan              # 预览可压缩组
    python3 memory_merger.py merge              # 执行合并（交互确认）
    python3 memory_merger.py merge --force      # 执行合并（不确认）
    python3 memory_merger.py merge --group scanner   # 只合并指定组
"""

import json
import logging
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, UTC

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("memory_merger")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUALITY_FILE = os.path.join(BASE_DIR, "memory_quality.json")

def _load_llm_config() -> tuple[str, str, str]:
    """从 .env 文件加载 LLM 配置"""
    env_path = os.path.join(BASE_DIR, ".env")
    api_key = ""
    base_url = "https://opencode.ai/zen/go/v1"
    model = "minimax-m2.5"

    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("HINDSIGHT_API_LLM_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                elif line.startswith("HINDSIGHT_API_LLM_BASE_URL="):
                    base_url = line.split("=", 1)[1].strip()

    if not api_key:
        # 也尝试 fallback
        api_key = os.environ.get("HINDSIGHT_API_LLM_API_KEY", "")
    if not api_key:
        logger.error("❌ 无法获取 LLM API Key，请检查 .env 文件")

    return api_key, base_url, model


# ── 加载 LLM 配置 ──────────────────────────────────────────────────
LLM_API_KEY, LLM_BASE_URL, LLM_MODEL = _load_llm_config()

# Hindsight API
HINDSIGHT_API = "http://127.0.0.1:9177/v1/default/banks/hermes"

# PSQL 连接
PG_DSN = "host=127.0.0.1 port=5433 user=hindsight password=hindsight dbname=hindsight"


def filter_existing_uids(uids: list[str]) -> list[str]:
    """
    通过 PG 批量查询 UID 是否存在。
    分批（200 UID/批）避免"Argument list too long"。
    返回 PG 中确实存在的 UID。
    """
    if not uids:
        return []

    existing = []
    batch_size = 200
    for i in range(0, len(uids), batch_size):
        batch = uids[i:i + batch_size]
        uid_list = ", ".join(f"'{u}'" for u in batch)
        sql = f"SELECT id FROM memory_units WHERE id IN ({uid_list})"
        try:
            result = subprocess.run(
                ["psql", "-h", "127.0.0.1", "-p", "5433", "-U", "hindsight",
                 "-d", "hindsight", "-tA",
                 "-c", sql],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "PGPASSWORD": "hindsight"}
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line:
                        existing.append(line)
        except Exception as e:
            logger.warning("  ⚠️ PG存在性查询失败(batch %d): %s", i // batch_size, e)

    return existing


# ═══════════════════════════════════════════════════════════════════
#  1. 扫描可压缩组
# ═══════════════════════════════════════════════════════════════════

def scan_compressible_groups(quality_file: str = QUALITY_FILE) -> list[dict]:
    """
    从 memory_quality.json 扫描可压缩组。
    返回已排序列表（按 pg_exists_count 降序），
    每组包含 tag, entries, count, avg_q, uids, pg_exists_count。
    自动排除 PG 中全不存在的组。
    """
    with open(quality_file) as f:
        quality_data = json.load(f)
    mems = quality_data.get("memories", {})

    groups = defaultdict(list)
    for uid, v in mems.items():
        if v.get("status") != "active":
            continue
        qual = v.get("quality", 0)
        if qual >= 0.6:
            continue
        for t in v.get("tags", []):
            if t.startswith("entity|object:"):
                groups[t].append({"uid": uid, "quality": qual, "tags": v.get("tags", [])})

    result = []
    for tag, entries in groups.items():
        if len(entries) < 3:
            continue
        avg_q = sum(e["quality"] for e in entries) / len(entries)
        uids = [e["uid"] for e in entries]

        # PG 存在性预检：查该组 UID 在 PG 中实际存在多少
        pg_existing = filter_existing_uids(uids)
        pg_exists_count = len(pg_existing)

        # 排除 PG 中全不存在的组
        if pg_exists_count == 0:
            continue

        result.append({
            "tag": tag,
            "count": len(entries),
            "avg_q": round(avg_q, 4),
            "uids": uids,
            "pg_exists_count": pg_exists_count,
        })

    # 按 pg_exists_count 降序（而不是 raw count）
    result.sort(key=lambda x: -x["pg_exists_count"])
    return result


def fetch_texts(uids: list[str], batch_size: int = 50) -> dict[str, str]:
    """
    通过 Hindsight recall API 批量获取记忆文本。
    返回 {uid: text} 字典。
    """
    texts = {}
    for i in range(0, len(uids), batch_size):
        batch = uids[i:i + batch_size]
        for uid in batch:
            try:
                # Hindsight API 通过 recall/语义搜索查，不知道 UID 直接查
                # 改用 list 端点，按 ID 过滤
                pass
            except Exception:
                pass

    # 改用更可靠的方法：直接用 list 端点遍历
    # 但 list 端点没有按 ID 查询的功能。
    # 用另一种方式：通过 curl 直接调 /memories/{id}
    for i in range(0, len(uids), batch_size):
        batch = uids[i:i + batch_size]
        for uid in batch:
            try:
                result = subprocess.run(
                    ["curl", "-s", f"{HINDSIGHT_API}/memories/{uid}"],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    data = json.loads(result.stdout)
                    if isinstance(data, dict) and "text" in data:
                        texts[uid] = data["text"]
                    elif isinstance(data, list) and len(data) > 0:
                        texts[uid] = data[0].get("text", "")
            except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
                logger.warning("  ⚠️ FETCH FAILED: %s — %s", uid[:16], e)
    return texts


def fetch_texts_for_group(uids: list[str], tag: str) -> dict[str, str]:
    """
    通过 Hindsight recall 端点按主题搜索获取记忆文本。
    recall 返回语义匹配结果，含 id + text。
    再用 uids 做交集匹配。
    """
    texts = {}

    # 从 tag 中提取搜索关键词
    search_term = tag.split(":", 1)[-1] if ":" in tag else tag
    uid_set = set(uids)

    try:
        payload = json.dumps({"query": search_term, "limit": 200})
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{HINDSIGHT_API}/memories/recall",
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            items = data.get("results", [])
            for item in items:
                item_id = item.get("id", "")
                if item_id in uid_set:
                    t = item.get("text", "")
                    if t:
                        texts[item_id] = t
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        logger.warning("  ⚠️ recall 查询失败: %s", e)

    return texts


def get_pg_count() -> int:
    """PG 直连查记忆总数"""
    try:
        result = subprocess.run(
            ["psql", "-h", "127.0.0.1", "-p", "5433", "-U", "hindsight",
             "-d", "hindsight", "-tA",
             "-c", "SELECT count(*) FROM memory_units"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "PGPASSWORD": "hindsight"}
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except Exception as e:
        logger.warning("PG count failed: %s", e)
    return 0


def delete_memories_via_pg(uids: list[str]) -> int:
    """
    通过 PSQL 级联删除记忆（分批，避免 Argument list too long）。
    返回删除的 memory_units 条数。
    """
    if not uids:
        return 0

    total_deleted = 0
    # 分批删除，每批最多 200 个 UID
    batch_size = 200

    for i in range(0, len(uids), batch_size):
        batch = uids[i:i + batch_size]
        uid_list = ", ".join(f"'{u}'" for u in batch)
        sql = f"""
        DELETE FROM memory_links WHERE from_unit_id IN ({uid_list}) OR to_unit_id IN ({uid_list});
        DELETE FROM unit_entities WHERE unit_id IN ({uid_list});
        DELETE FROM memory_units WHERE id IN ({uid_list});
        """
        try:
            result = subprocess.run(
                ["psql", "-h", "127.0.0.1", "-p", "5433", "-U", "hindsight",
                 "-d", "hindsight", "-tA", "-c", sql],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "PGPASSWORD": "hindsight"}
            )
            s = result.stdout.strip()
            lines = [l.strip() for l in s.split("\n") if l.strip() and "DELETE" in l.upper()]
            if lines:
                last = lines[-1]
                count = int(last.split()[-1]) if last.split()[-1].isdigit() else 0
                total_deleted += count
        except Exception as e:
            logger.warning("  ⚠️ PG分批删除失败(batch %d): %s", i // batch_size, e)

    logger.info("  PG分批删除共 %d 条", total_deleted)
    return total_deleted


def call_llm_merge(memories: list[dict], tag: str) -> str | None:
    """
    调用 LLM 合并一组记忆。
    memories: [{"text": "...", "uid": "..."}, ...]
    tag: entity|object:xxx
    返回合并后的文本，或 None 表示失败。
    """
    if not memories:
        return None

    # 构建 prompt
    texts = []
    for i, m in enumerate(memories, 1):
        t = m.get("text", "").strip()
        if len(t) > 400:
            t = t[:400] + "..."
        texts.append(f"[{i}] {t}")

    # 智能截断：如果总输入太长，优先保留前面的
    max_input_chars = 6000
    combined = "\n".join(texts)
    if len(combined) > max_input_chars:
        truncated = []
        remaining = max_input_chars
        for t in texts:
            if remaining <= 0:
                break
            take = min(len(t), remaining)
            truncated.append(t[:take])
            remaining -= take
        texts = truncated

    prompt = f"""你是一个记忆合并专家。以下是一组关于同一主题「{tag}」的自动记忆片段。
这些记忆来自AI系统的自动保留机制，每条都比较简短、信息密度低。
请将它们合并为一条紧凑的高密度摘要：

- 保留所有实质性信息
- 删除重复表述
- 用一段话概括，不超过 300 字
- 使用具体名称和绝对时间（如果有）
- 不要用"这是关于..."之类的元描述，直接给内容

输入记忆（共{len(texts)}条）：
{chr(10).join(texts)}

合并后的摘要（一段话，不超过300字）："""

    # 调用 LLM（带重试）
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是一个精确的记忆合并工具。输出简洁、信息密集的摘要，不含元评论。"},
            {"role": "user", "content": prompt}
        ],
        "max_tokens": 500,
        "temperature": 0.1,
    })

    for attempt in range(3):
        try:
            result = subprocess.run(
                ["curl", "-s", "-X", "POST", f"{LLM_BASE_URL}/chat/completions",
                 "-H", "Content-Type: application/json",
                 "-H", f"Authorization: Bearer {LLM_API_KEY}",
                 "-d", payload],
                capture_output=True, text=True, timeout=90
            )
            if result.returncode != 0:
                logger.warning("  ⚠️ LLM调用失败(attempt=%d): returncode=%d", attempt+1, result.returncode)
                time.sleep(2)
                continue

            data = json.loads(result.stdout)
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

            # 检查 reasoning 模型特殊情况
            if not content:
                reasoning = data.get("choices", [{}])[0].get("message", {}).get("reasoning_content", "")
                if reasoning:
                    # 有一部分推理模型的content在reasoning_content里
                    # 尝试从中提取最终回答
                    lines = reasoning.split("\n")
                    answer_lines = [l for l in lines if l.strip() and not l.strip().startswith(("Think", "1.", "**", "Let"))]
                    if answer_lines:
                        content = " ".join(l.strip() for l in answer_lines[-3:])
                        logger.info("  ℹ️ 从reasoning_content提取内容(%d字)", len(content))

            if not content:
                logger.warning("  ⚠️ LLM返回空内容(attempt=%d), raw=%s", attempt+1, result.stdout[:200])
                time.sleep(2)
                continue

            return content

        except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            logger.warning("  ⚠️ LLM调用异常(attempt=%d): %s", attempt+1, e)
            time.sleep(2)

    logger.error("  ❌ LLM调用全部3次重试失败")
    return None


def store_consolidated(text: str, tag: str) -> bool:
    """
    通过 Hindsight API 存储合并后的记忆。
    """
    # 直接调 Hindsight API POST /memories
    # tags: 保留原 entity|object tag + 新增 consolidation 标记
    original_entity = tag  # e.g., entity|object:holding_analysis
    clean_name = tag.split(":", 1)[-1] if ":" in tag else tag
    tags = [
        original_entity,
        "dev-system",
        f"merged:{clean_name}",
        f"date:{datetime.now(UTC).strftime('%Y-%m-%d')}",
    ]

    payload = json.dumps({
        "items": [{
            "content": text,
            "tags": tags,
            "context": f"merged from {tag}",
        }]
    })

    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{HINDSIGHT_API}/memories",
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            success = data.get("success", False) or data.get("items_count", 0) > 0
            if success:
                logger.info("  ✅ 合并记忆存储成功")
                return True
            logger.warning("  ⚠️ 存储返回异常: %s", result.stdout[:200])
            return False
        logger.error("  ❌ 存储调用失败: %s", result.stdout[:200])
        return False
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        logger.error("  ❌ 存储异常: %s", e)
        return False


def update_quality_file(uids_removed: list[str], new_uid: str, new_quality: float = 0.75):
    """
    更新 memory_quality.json:
    - 删除已移除的 UID
    - 添加新合并的记忆条目（质量分给 0.75，因为 merged 后 specificity 更高）
    """
    with open(QUALITY_FILE) as f:
        quality_data = json.load(f)
    mems = quality_data.get("memories", {})

    for uid in uids_removed:
        mems.pop(uid, None)

    # 添加新条目
    mems[new_uid] = {
        "quality": new_quality,
        "status": "active",
        "visit_count": 0,
        "tags": ["consolidated"],
    }

    quality_data["memories"] = mems
    with open(QUALITY_FILE, "w") as f:
        json.dump(quality_data, f, ensure_ascii=False, indent=2)
    logger.info("  📝 quality.json 已更新")


# ═══════════════════════════════════════════════════════════════════
#  2. SCAN 命令
# ═══════════════════════════════════════════════════════════════════

def cmd_scan():
    groups = scan_compressible_groups()
    total_entries = sum(g["count"] for g in groups)
    total_pg_exists = sum(g["pg_exists_count"] for g in groups)

    print(f"📊 可压缩组数: {len(groups)}")
    print(f"📦 涉及总记忆(quality.json): {total_entries}")
    print(f"🗄️  PG 中真实存在: {total_pg_exists} ({(total_pg_exists/total_entries*100):.0f}%)")
    print(f"📉 合并后预计: ~{len(groups)} 条（减少 ~{total_pg_exists - len(groups)} 条）")
    print()
    print(f"{'排名':>4} {'主题':<46} {'条数':>6} {'PG存':>6} {'平均q':>8}")
    print("-" * 74)
    for i, g in enumerate(groups, 1):
        tag_short = g["tag"][:44]
        pct = f"{g['pg_exists_count']}/{g['count']}"
        print(f"{i:>4}  {tag_short:<46} {g['count']:>6} {pct:>6} {g['avg_q']:>8.3f}")
    print()
    pg_count = get_pg_count()
    print(f"PG 当前总记忆数: {pg_count}")


# ═══════════════════════════════════════════════════════════════════
#  3. quality.json 同步
# ═══════════════════════════════════════════════════════════════════

def sync_quality_file(uids_to_remove: set):
    """
    从 quality.json 中移除已从 PG 删除的 UID。
    """
    if not uids_to_remove:
        return
    try:
        with open(QUALITY_FILE) as f:
            q = json.load(f)
        mems = q.get("memories", {})
        before = len(mems)
        for uid in uids_to_remove:
            mems.pop(uid, None)
        q["memories"] = mems
        with open(QUALITY_FILE, "w") as f:
            json.dump(q, f, ensure_ascii=False, indent=2)
        logger.info("  📋 quality.json: %d → %d (移除 %d)", before, len(mems), before - len(mems))
    except Exception as e:
        logger.warning("  ⚠️ quality.json 同步失败: %s", e)


def sync_quality_pg() -> int:
    """将 quality.json 中已在 PG 不存在的 UID 标记/删除。

    规则：
    - quality.json 中 status=active 但 UID 不在 PG → 标记为 orphaned
    - quality.json 中 status=orphaned 且 UID 不在 PG → 直接从 quality.json 删除
    - 在末尾记录 last_sync_pg 时间戳
    """
    q_path = QUALITY_FILE
    if not os.path.exists(q_path):
        logger.warning("  ⚠️ quality.json 不存在")
        return 0

    # 1. 读 quality.json
    with open(q_path) as f:
        data = json.load(f)
    mems = data.get("memories", {})
    if not mems:
        logger.info("  ℹ️ quality.json 为空，无需同步")
        return 0

    # 2. 查 PG 全部 UID
    try:
        result = subprocess.run(
            ["psql", "-h", "127.0.0.1", "-p", "5433", "-U", "hindsight",
             "-d", "hindsight", "-tA",
             "-c", "SELECT id FROM memory_units"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PGPASSWORD": "hindsight"}
        )
        if result.returncode != 0:
            logger.warning("  ⚠️ psql 查询失败: %s", result.stderr.strip()[:200])
            return 0
        pg_ids = set(line.strip() for line in result.stdout.strip().split("\n") if line.strip())
    except Exception as e:
        logger.warning("  ⚠️ PG 查询异常: %s", e)
        return 0

    logger.info("  📊 quality.json: %d 条, PG: %d 条", len(mems), len(pg_ids))

    # 3. 遍历标记/删除
    fixed = 0
    removed_count = 0
    for uid, v in list(mems.items()):
        status = v.get("status", "active")
        if status == "active" and uid not in pg_ids:
            v["status"] = "orphaned"
            fixed += 1
        elif status == "orphaned" and uid not in pg_ids:
            mems.pop(uid)
            removed_count += 1
            fixed += 1

    data["memories"] = mems
    data["last_sync_pg"] = datetime.now(UTC).isoformat()
    with open(q_path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    if fixed:
        logger.info("  ✅ quality.json 同步: active→orphaned=%d, orphaned→删除=%d", fixed - removed_count, removed_count)
    else:
        logger.info("  ✅ quality.json 同步: 无变化")
    return fixed


# ═══════════════════════════════════════════════════════════════════
#  4. MERGE 命令
# ═══════════════════════════════════════════════════════════════════

def cmd_merge(groups_to_process: list[dict], force: bool = False,
              max_groups: int = 5):
    """
    执行合并。
    groups_to_process: 要处理的组列表（大组优先）
    force: 是否跳过确认
    max_groups: 本次最多处理几组
    """
    target_groups = groups_to_process[:max_groups]
    total_entries = sum(g["count"] for g in target_groups)

    print(f"🎯 本次计划合并 {len(target_groups)} 组，涉及 {total_entries} 条记忆")
    for g in target_groups:
        print(f"  • {g['tag']}: {g['count']}条 (q={g['avg_q']:.3f})")

    if not force:
        confirm = input(f"\n确认执行合并? (y/N): ").strip().lower()
        if confirm != "y":
            print("已取消")
            return

    # 先查询 PG 当前条数作为基准
    pg_before = get_pg_count()

    for idx, group in enumerate(target_groups):
        tag = group["tag"]
        uids = group["uids"]
        print(f"\n[{idx+1}/{len(target_groups)}] 处理 {tag} ({len(uids)}条) ...")

        # 1. PG 存在性预检 + 获取文本，过滤空内容
        print("  🔍 PG 存在性预检...")
        existing_uids = filter_existing_uids(uids)
        skipped = len(uids) - len(existing_uids)
        if skipped:
            print(f"     ⚠️ {skipped}/{len(uids)} 条在 PG 中已不存在，跳过")
        if not existing_uids:
            print("  ℹ️ PG 中无该组的记忆，跳过本组")
            continue
        uids = existing_uids

        print("  📡 获取记忆文本...")
        texts = fetch_texts_for_group(uids, tag)
        # 过滤掉无实质内容的条目（<20字）
        texts = {uid: t for uid, t in texts.items() if len(t.strip()) >= 20}
        found_count = len(texts)
        print(f"  📄 获取到 {found_count}/{len(uids)} 条有实质内容的文本")

        if found_count == 0:
            # 全部无实质内容 → 直接删除
            print("  🗑️ 全部无实质内容，直接删除...")
            deleted = delete_memories_via_pg(uids)
            print(f"  🗑️ 已删除 {deleted} 条")
            continue
        elif found_count < 3:
            # 不足3条 → 跳过合并，保留内容较丰富的
            print("  ⏭️ 跳过：有实质内容的文本不足3条，无需合并")
            continue

        # 2. 构建 LLM 输入
        memories_for_llm = [{"text": texts[uid], "uid": uid} for uid in uids if uid in texts]

        # 3. 调用 LLM 合并
        print("  🤖 LLM合并中...")
        merged = call_llm_merge(memories_for_llm, tag)
        if not merged:
            print("  ❌ 合并失败，跳过本组")
            continue
        print(f"  📝 合并结果 ({len(merged)}字): {merged[:120]}...")

        # 4. 存储合并版
        print("  💾 存储合并记忆...")
        stored = store_consolidated(merged, tag)
        if not stored:
            print("  ❌ 存储失败，跳过删除（已合并的内容不会丢失）")
            continue

        # 5. 删除原始记忆
        print("  🗑️ 删除原始记忆...")
        deleted = delete_memories_via_pg(uids)
        print(f"  🗑️ 已删除 {deleted} 条原始记忆")

        # 6. 同步 quality.json：删除已删 UID
        sync_quality_file(set(uids))
        print(f"  📋 quality.json 已同步")

        # 限流：组间间隔 2 秒
        if idx < len(target_groups) - 1:
            time.sleep(2)

    # 最终统计
    pg_after = get_pg_count()
    print(f"\n📊 合并完成")
    print(f"  PG 条数: {pg_before} → {pg_after} (减少 {pg_before - pg_after})")

    # 自动执行 quality.json 同步
    print("\n🔍 同步 quality.json...")
    try:
        fixed = sync_quality_pg()
        print(f"  ✅ quality.json 同步完成 (处理 {fixed} 条)")
    except Exception as e:
        logger.warning("  ⚠️ quality.json 同步异常: %s", e)


# ═══════════════════════════════════════════════════════════════════
#  4. 入口
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]
    force = "--force" in args
    group_filter = None
    max_groups = 5

    for arg in args:
        if arg.startswith("--group="):
            group_filter = arg.split("=", 1)[1]
        elif arg.startswith("--max="):
            max_groups = int(arg.split("=", 1)[1])

    command = args[0] if args and not args[0].startswith("--") else "scan"

    if command == "scan":
        cmd_scan()
    elif command == "merge":
        groups = scan_compressible_groups()
        if group_filter:
            groups = [g for g in groups if group_filter in g["tag"]]
        if not groups:
            print("没有找到可压缩的组")
            sys.exit(0)
        cmd_merge(groups, force=force, max_groups=max_groups)
    elif command == "sync-quality":
        fixed = sync_quality_pg()
        print(f"quality.json 同步完成，处理 {fixed} 条")
    else:
        print(f"未知命令: {command}")
        print("用法: python3 memory_merger.py [scan|merge|sync-quality] [--force] [--group=xxx] [--max=N]")
        sys.exit(1)
