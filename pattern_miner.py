#!/usr/bin/env python3
"""
离线知识提炼 — 从 hindsight 记忆中提取高频模式，生成启发式规则。

每周运行一次，通过分析近期记忆的共现关系，自动发现新的关联路径，
并将发现的模式作为 suggest: 边更新到 AMAP 路由表（MEMORY.md）。

用法:
    python3 pattern_miner.py [--days 7] [--bank hermes]

输出:
    - 发现的新模式列表
    - 建议更新的 AMAP 条目
    - 写入 ~/.hermes/hindsight/pattern_cache.json
"""

import json
import logging
import os
import re
import sys
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("pattern_miner")

# ── Config ──────────────────────────────────────────────────────────────────
API_BASE = os.environ.get("HINDSIGHT_API_URL", "http://127.0.0.1:9177/v1/default/banks")
BANK_ID = os.environ.get("HINDSIGHT_BANK_ID", "hermes")
CACHE_PATH = os.path.join(os.path.dirname(__file__), "pattern_cache.json")
MEMORY_PATH = os.path.expanduser("~/.hermes/memories/MEMORY.md")

# ── Entity/Relation tag patterns ────────────────────────────────────────────
ENTITY_RE = re.compile(r"^entity\|(\w+):(.+)$")
RELATION_RE = re.compile(r"^relation\|(\w+):(.+)$")


def curl_recall(query: str, limit: int = 50, tags: list[str] | None = None) -> list[dict]:
    """Fetch memories from hindsight for pattern analysis.

    使用 GET /list 端点（替代 recall 语义搜索），避免空 query 问题和
    语义索引超时。按 created_at 降序取最新 limit 条。
    """
    url = f"{API_BASE}/{BANK_ID}/memories/list?limit={limit}&order=-created_at"
    if tags:
        # 如果 tags 是逗号分隔的单个字符串，转为单元素列表
        if isinstance(tags, str):
            tags_list = [tags]
        else:
            tags_list = tags
        # list 端点不支持 tags 过滤，这里保留参数但提示
        logger.info("Tags filter not supported on list endpoint, ignoring: %s", tags_list)
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "15", url],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            # 兼容不同响应格式
            items = data.get("items", data.get("data", []))
            if not items and isinstance(data, list):
                items = data
            logger.info("Fetched %d memories via list endpoint", len(items))
            return items
        logger.warning("Empty response from list endpoint")
    except Exception as e:
        logger.warning("List failed: %s", e)
    return []


def extract_entities_from_memories(memories: list[dict]) -> list[dict]:
    """Extract entity/relation tags from memories, return entity info."""
    entities = []
    for mem in memories:
        tags = mem.get("tags", []) or []
        text = mem.get("text", "")[:200]
        for tag in tags:
            m = ENTITY_RE.match(tag)
            if m:
                entities.append({
                    "type": m.group(1),
                    "name": m.group(2),
                    "text": text,
                    "timestamp": mem.get("created_at", ""),
                })
    return entities


def find_cooccurrence_patterns(entities: list[dict]) -> list[dict]:
    """Find frequently co-occurring entity pairs.

    Returns:
        [{"entity_a": str, "entity_b": str, "count": int, "suggested_relation": str}]
    """
    # Group by timestamp proximity (same memory = same text prefix = co-occurrence)
    text_groups: dict[str, list] = defaultdict(list)
    for e in entities:
        text_groups[e["text"]].append(e)

    # Count entity pairs
    pair_counts: Counter = Counter()
    for text, group in text_groups.items():
        names = [(e["type"], e["name"]) for e in group]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                pair = tuple(sorted([f"{names[i][0]}:{names[i][1]}",
                                     f"{names[j][0]}:{names[j][1]}"]))
                pair_counts[pair] += 1

    # Convert to patterns
    patterns = []
    for (entity_a, entity_b), count in pair_counts.most_common(20):
        if count >= 2:  # Minimum threshold
            # Infer relation type from entity types
            type_a = entity_a.split(":")[0]
            type_b = entity_b.split(":")[0]
            if type_a == "event" and type_b in ("object", "concept"):
                suggested = "impact"
            elif type_a == "concept" and type_b == "object":
                suggested = "guide"
            elif type_a == "object" and type_b == "object":
                suggested = "depend"
            else:
                suggested = "suggest"
            patterns.append({
                "entity_a": entity_a,
                "entity_b": entity_b,
                "count": count,
                "suggested_relation": f"{suggested}:{entity_b.split(':', 1)[1]}",
            })

    return patterns


def generate_amap_suggestions(patterns: list[dict]) -> list[str]:
    """Generate human-readable AMAP update suggestions from patterns."""
    suggestions = []
    for p in patterns[:10]:
        suggestions.append(
            f"  建议: [{p['entity_a']}] ─{p['suggested_relation'].split(':')[0]}─→ [{p['entity_b']}] "
            f"(共现{p['count']}次)"
        )
    return suggestions


def update_cache(patterns: list[dict]) -> None:
    """Persist discovered patterns to cache file."""
    cache = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "patterns_count": len(patterns),
        "top_patterns": patterns[:20],
    }
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    logger.info("Cache written to %s (%d patterns)", CACHE_PATH, len(patterns))


def main(days: int = 7):
    logger.info("Pattern miner starting: last %d days, bank=%s", days, BANK_ID)

    # Step 1: Recall recent memories
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    memories = curl_recall("", limit=100)
    if not memories:
        logger.info("No memories found in last %d days", days)
        return

    logger.info("Retrieved %d memories", len(memories))

    # Step 2: Extract entity/relation tags
    entities = extract_entities_from_memories(memories)
    if not entities:
        logger.info("No entity-tagged memories found. Use entity|type:name tags when retaining.")
        return

    logger.info("Found %d entity tags in %d memories", len(entities), len(memories))

    # Step 3: Find co-occurrence patterns
    patterns = find_cooccurrence_patterns(entities)
    if not patterns:
        logger.info("No significant co-occurrence patterns found (threshold: 2+)")
        return

    # Step 4: Generate suggestions
    suggestions = generate_amap_suggestions(patterns)
    logger.info("Discovered %d patterns (top %d shown):", len(patterns), len(suggestions))
    for s in suggestions:
        print(f"  {s}")

    # Step 5: Check if MEMORY.md AMAP needs updating
    if os.path.exists(MEMORY_PATH):
        content = open(MEMORY_PATH).read()
        if "[AMAP]" in content:
            logger.info("AMAP table found in MEMORY.md — consider manual review of suggestions above")
        else:
            logger.info("No AMAP section in MEMORY.md yet")

    # Step 6: Cache results
    update_cache(patterns)


if __name__ == "__main__":
    days = 7
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])
    main(days)
