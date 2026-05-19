#!/usr/bin/env python3
"""
Memory Manager — unified interface for hindsight memory operations.

Integrates:
  - Scene tagging (Phase 2.1)
  - Online dedup (Phase 2.3)
  - Semantic compression (Phase 1.3)
  - Temporal aggregation (Phase 2.2) via cron triggers

Usage:
    from memory_manager import MemoryManager
    mm = MemoryManager()
    mm.store("修复了retain KeyError", scene="dev", tags=["bug-fix"])
    mm.search("retain bug")
    mm.daily_summary()  # cron: aggregate today's memories
"""

import json
import logging
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, UTC
from typing import Any

# ── Scene tag definitions ────────────────────────────────────────────
SCENE_TAGS = {
    "stock":      "stock-analysis",     # 股票分析相关
    "dev":        "dev-system",         # 系统开发/调试
    "life":       "personal-life",       # 个人生活
    "project":    "project-planning",    # 项目规划/方案讨论
    "trading":    "trading-rules",       # 交易规则/持仓
}

# Reverse lookup: scene_tag → scene_name
SCENE_BY_TAG = {v: k for k, v in SCENE_TAGS.items()}

# ── Scene auto-classification keywords ───────────────────────────────
_SCENE_KEYWORDS = {
    "stock": ["股票", "K线", "涨停", "跌停", "ADX", "BOLL", "持仓", "买入", "卖出",
              "行情", "大盘", "板块", "仓位", "止损", "止盈", "均线", "成交量",
              "换手率", "趋势", "突破", "回调", "反弹", "支撑", "压力"],
    "dev":   ["bug", "修复", "部署", "配置", "API", "skill", "MongoDB", "git",
              "错误", "报错", "调试", "代码", "函数", "类", "模块", "重构",
              "升级", "迁移", "版本", "commit", "push", "PR"],
    "life":  ["猫", "福宝", "吃饭", "天气", "旅行", "日记", "生活", "个人",
              "德文", "宠物", "健康", "运动", "电影", "音乐"],
    "project": ["方案", "计划", "设计", "架构", "路线图", "需求", "规划",
                "文档", "设计稿", "顶层设计", "流程图", "分析", "对比",
                "调研", "评审", "里程碑", "deadline"],
    "trading": ["交易", "止损", "仓位", "预期", "目标价", "风报比",
                "加仓", "减仓", "清仓", "底仓", "做T", "回撤", "胜率",
                "盈亏比", "信号", "入场", "出场"],
}

# ── Memory extraction rules path ─────────────────────────────────────
RULES_PATH = os.path.join(os.path.dirname(__file__), "memory_rules.json")
CONFLICT_LOG_PATH = os.path.join(os.path.dirname(__file__), "conflict_log.json")

# ── Logger ───────────────────────────────────────────────────────────
logger = logging.getLogger("memory_manager")


# ── Core class ───────────────────────────────────────────────────────
class MemoryManager:
    """Unified interface for hindsight memory with scene awareness."""

    API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"

    def __init__(self):
        self._session = None

    # ── Public API ────────────────────────────────────────────────────

    def store(self, content: str, scene: str = "",
              tags: list[str] | None = None, context: str = "",
              dedup: bool = True, check_conflict: bool = True
              ) -> dict[str, Any]:
        """
        Store a memory with scene tagging and optional dedup/conflict check.

        Args:
            content: Memory content (will be compressed + scene-traced)
            scene: One of 'stock', 'dev', 'life', 'project', 'trading'.
                   If empty, auto-inferred from content.
            tags: Additional tags
            context: Short context label
            dedup: If True, check for duplicates before storing
            check_conflict: If True, check for semantic contradictions

        Returns:
            {"success": bool, "action": "stored"|"skipped_duplicate"|"error"
             "conflicts": [...] }
        """
        result_meta: dict[str, Any] = {}

        # 1. Semantic compression
        compressed = self._compress(content)

        # 2. Dual-Trace Encoding: generate scene trace for cross-session recall
        scene_trace = self._build_scene_trace(compressed, scene, context)
        stored_content = f"{scene_trace}\n{compressed}" if scene_trace else compressed

        # 3. Auto-infer scene if not specified
        if not scene:
            scene = self._infer_scene(stored_content)

        # 4. Build tags
        all_tags = list(tags or [])
        if scene and scene in SCENE_TAGS:
            scene_tag = SCENE_TAGS[scene]
            if scene_tag not in all_tags:
                all_tags.append(scene_tag)
        all_tags = list(dict.fromkeys(all_tags))

        # 4. Conflict detection (before dedup, to log even if skipped)
        if check_conflict and all_tags:
            conflicts = self._check_conflicts(stored_content, all_tags)
            if conflicts:
                result_meta["conflicts"] = conflicts
                logger.info("Conflict detected: %d conflicting memories found for: %s",
                            len(conflicts), compressed[:60])
                # Apply extraction rules to extract structured info
                extractions = self._apply_extraction_rules(compressed)
                if extractions:
                    result_meta["extractions"] = extractions

        # 5. Online dedup
        if dedup and all_tags:
            similar = self._find_similar(stored_content, tags=all_tags)
            if similar and similar[0].get("score", 0) > 0.90:
                logger.info("Dedup: skipped duplicate (score=%.2f): %s",
                            similar[0]["score"], stored_content[:80])
                return {"success": True, "action": "skipped_duplicate",
                        "existing_id": similar[0].get("id"),
                        **result_meta}

        # 6. Store via curl
        payload = {
            "items": [{
                "content": stored_content,
                "tags": all_tags,
                "context": context,
            }]
        }
        result = self._curl("POST", "/memories", payload)

        if result and result.get("success"):
            return {"success": True, "action": "stored",
                    "items_count": result.get("items_count", 1),
                    **result_meta}
        else:
            return {"success": False, "action": "error",
                    "error": str(result), **result_meta}

    def search(self, query: str, scene: str = "",
               limit: int = 10) -> list[dict[str, Any]]:
        """Search memories, optionally scoped to a scene."""
        payload = {"query": query, "limit": limit}
        if scene and scene in SCENE_TAGS:
            payload["tags"] = [SCENE_TAGS[scene]]
            payload["tags_match"] = "any"
        result = self._curl("POST", "/memories/recall", payload)
        if result and "results" in result:
            return result["results"]
        return []

    def get_recent_session_summaries(self, count: int = 3) -> list[dict[str, Any]]:
        """获取最近的会话摘要，用于跨会话上下文注入。

        Args:
            count: 返回最近几条摘要（默认3条）

        Returns:
            list[dict]: 按时间倒序的摘要列表，每项包含 text/tags/created_at 等字段
        """
        payload = {
            "query": "",
            "limit": count,
            "tags": ["session-summary"],
            "tags_match": "any",
        }
        result = self._curl("POST", "/memories/recall", payload)
        if result and "results" in result:
            return result["results"]
        return []

    def daily_summary(self, date_str: str | None = None) -> dict[str, Any]:
        """
        Generate a summary of all memories from a given day.
        Intended to be called by a cron job.
        """
        from memory_compressor import compress as semantic_compress

        if date_str is None:
            date_str = datetime.now(UTC).strftime("%Y-%m-%d")

        # Recall all memories from today
        results = self.search(f"{date_str} memories", limit=50)
        if not results:
            return {"success": True, "action": "no_memories", "date": date_str}

        # Group by tag for compact summary
        tag_counter: Counter = Counter()
        for r in results:
            for t in (r.get("tags") or []):
                tag_counter[t] += 1

        top_tags = ", ".join(f"{t}({c})" for t, c in tag_counter.most_common(10))
        short_items = "\n".join(
            f"- {r['text'][:150]}"
            for r in results[:10]
        )
        if len(results) > 10:
            short_items += f"\n  ... 还有 {len(results) - 10} 条"

        summary_text = (
            f"【日汇总·{date_str}】"
            f"当日{len(results)}条记忆。"
            f"标签分布: {top_tags}\n"
            f"{short_items}"
        )
        compressed = semantic_compress(summary_text)

        # Store as daily summary
        return self.store(
            compressed,
            scene="project",
            tags=[f"daily-summary", f"date:{date_str}"],
            context="auto-daily-summary",
            dedup=False,  # Don't dedup summaries
        )

    def weekly_summary(self, year_week: str | None = None) -> dict[str, Any]:
        """
        Generate a weekly summary from daily summaries.
        Intended to be called by a cron job (weekly).
        """
        from memory_compressor import compress as semantic_compress

        if year_week is None:
            now = datetime.now(UTC)
            iso = now.isocalendar()
            year_week = f"{iso[0]}-W{iso[1]:02d}"

        # Search for daily summaries tagged with this week
        results = self.search(f"{year_week} daily-summary", limit=20)
        if not results:
            return {"success": True, "action": "no_daily_summaries", "week": year_week}

        all_summaries = "\n".join(r['text'][:1000] for r in results)
        summary_text = (
            f"【周汇总·{year_week}】\n"
            f"本周围绕 {len(results)} 天汇总：\n{all_summaries}"
        )
        compressed = semantic_compress(summary_text)

        return self.store(
            compressed,
            scene="project",
            tags=[f"weekly-summary", f"week:{year_week}"],
            context="auto-weekly-summary",
            dedup=False,
        )

    def session_summary(self, memories: list[dict[str, Any]],
                        session_label: str = "") -> dict[str, Any]:
        """Generate a session summary from a list of memories.

        Called at the end of a conversation to compact what was discussed.

        Args:
            memories: List of dicts with at least 'text' key
            session_label: Optional short label for the session

        Returns:
            store() result
        """
        if not memories:
            return {"success": True, "action": "no_memories"}

        # Dedup by text
        seen: set[str] = set()
        unique: list[str] = []
        for m in memories:
            t = (m.get("text") or m.get("content") or "")[:200]
            if t and t not in seen:
                seen.add(t)
                unique.append(t)

        if not unique:
            return {"success": True, "action": "no_content"}

        prefix = f"【会话摘要{session_label}】" if session_label else "【会话摘要】"
        summary = (
            f"{prefix}本会话共{len(memories)}条操作，"
            f"其中{len(unique)}条不重复。\\n"
            + "\\n".join(f"- {t}" for t in unique[:15])
        )
        if len(unique) > 15:
            summary += f"\\n  ... 还有 {len(unique) - 15} 条"

        return self.store(
            summary,
            scene="project",
            tags=["session-summary"],
            context="auto-session-summary",
            dedup=False,
        )

    # ── Internal helpers ─────────────────────────────────────────────

    def _compress(self, content: str) -> str:
        """Apply semantic compression."""
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from memory_compressor import compress
            return compress(content)
        except ImportError:
            return content

    def _build_scene_trace(self, content: str, scene: str, context: str) -> str:
        """Generate a scene trace for Dual-Trace Encoding.

        Creates a compact context prefix that anchors the memory in its scene,
        improving cross-session recall by providing richer semantic signals.

        Format: [场景名] [上下文标签] → content
        Example: [dev-system] [bug-fix] → 修复了retain KeyError...
        """
        scene_name = scene or self._infer_scene(content) or ""
        parts = []
        if scene_name and scene_name in SCENE_TAGS:
            parts.append(SCENE_TAGS[scene_name])
        elif scene_name:
            parts.append(scene_name)
        if context:
            parts.append(context)
        if not parts:
            return ""
        return f"{' '.join(f'[{p}]' for p in parts)} →"

    def _chain_consolidation(self, from_level: str, to_level: str,
                            date_ref: str = "") -> dict[str, Any]:
        """链式整合：将下级摘要聚合为上级摘要。

        Args:
            from_level: 源级别标签（如 "session-summary", "daily-summary"）
            to_level:   目标级别标签（如 "daily-summary", "weekly-summary"）
            date_ref:   日期/周参考（如 "2026-05-01", "2026-W18"）

        Returns:
            store() result
        """
        # 搜索下级摘要
        search_query = f"{date_ref} {from_level}" if date_ref else from_level
        results = self.search(search_query, scene="project", limit=30)
        if not results:
            return {"success": True, "action": "no_items"}

        # 去重合并
        seen: set[str] = set()
        items: list[str] = []
        for r in results:
            t = (r.get("text") or "")[:200]
            if t and t not in seen:
                seen.add(t)
                items.append(f"- {t}")

        if not items:
            return {"success": True, "action": "no_content"}

        prefix = f"【{to_level}】来自{len(results)}条{from_level}记录"
        summary = prefix + "\n" + "\n".join(items[:20])
        if len(items) > 20:
            summary += f"\n  ... 还有 {len(items) - 20} 条"

        return self.store(
            summary,
            scene="project",
            tags=[to_level],
            context=f"chain-{from_level}-to-{to_level}",
            dedup=False,
        )

    def session_end_summary(self, session_id: str = "",
                            recent_memories: list[dict[str, Any]] | None = None
                            ) -> dict[str, Any]:
        """会话结束时自动生成摘要并触发链式整合。

        先存 session-summary，再触发到 daily-summary 的整合。
        """
        # 优先使用传入的记忆列表，否则搜索最近记忆
        if not recent_memories:
            recent_memories = self.search(f"session {session_id}", limit=15)

        result = self.session_summary(recent_memories,
                                       session_label=f"·{session_id[:8]}" if session_id else "")

        # 触发链式整合：session-summary → daily-summary
        if result.get("action") in ("stored", "skipped_duplicate"):
            self._chain_consolidation("session-summary", "daily-summary",
                                       datetime.now(UTC).strftime("%Y-%m-%d"))
        return result

    def _find_similar(self, content: str, tags: list[str] | None = None,
                      threshold: float = 0.85) -> list[dict]:
        """Find similar existing memories (for dedup)."""
        payload = {"query": content[:200], "limit": 5}
        if tags:
            payload["tags"] = tags
            payload["tags_match"] = "any"
        result = self._curl("POST", "/memories/recall", payload)
        if result and "results" in result:
            scored = []
            for r in result["results"]:
                # Simple similarity: if the same tags and content overlaps significantly
                text = r.get("text", "")
                if text and self._text_similarity(content, text) > threshold:
                    scored.append(r)
            return scored
        return []

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """Simple overlap-based similarity (word-level Jaccard)."""
        set_a = set(a.lower().split()[:50])
        set_b = set(b.lower().split()[:50])
        if not set_a or not set_b:
            return 0.0
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersection / union if union > 0 else 0.0

    def _curl(self, method: str, path: str,
              payload: dict | None = None) -> dict | None:
        """Make a curl call to the hindsight API."""
        url = f"{self.API_BASE}{path}"
        cmd = ["curl", "-s", url]
        if payload:
            cmd += ["-X", method, "-H", "Content-Type: application/json",
                    "-d", json.dumps(payload, ensure_ascii=False)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode == 0 and result.stdout.strip():
                return json.loads(result.stdout)
            return None
        except Exception as e:
            logger.warning("curl failed: %s", e)
            return None

    # ── Scene auto-inference ────────────────────────────────────────────

    def _infer_scene(self, content: str) -> str:
        """Auto-infer scene from content keywords."""
        if not content:
            return ""
        content_lower = content.lower()
        scores: dict[str, int] = {}
        for scene_name, keywords in _SCENE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw.lower() in content_lower)
            if score > 0:
                scores[scene_name] = score
        if not scores:
            return ""
        # Return highest-scoring scene
        best = max(scores, key=scores.get)
        logger.debug("Auto-scene inferred: %s (scores=%s)", best, scores)
        return best

    # ── Conflict detection ──────────────────────────────────────────────

    def _check_conflicts(self, content: str,
                         tags: list[str]) -> list[dict[str, Any]]:
        """Check if new memory contradicts existing ones.

        Returns list of conflicting memory dicts (empty if none).
        """
        if not content or not tags:
            return []
        similar = self._find_similar(content, tags=tags, threshold=0.6)
        if not similar:
            return []
        conflicts: list[dict[str, Any]] = []
        for mem in similar:
            existing_text = mem.get("text", "")
            if not existing_text:
                continue
            # Rough contradiction heuristic: same topic, opposite sentiment
            sim_score = self._text_similarity(content, existing_text)
            if sim_score < 0.3:
                continue  # too different, ignore
            # If content mentions explicit negation vs existing
            negations_new = any(w in content for w in ["不", "没", "停止", "反对", "讨厌"])
            negations_old = any(w in existing_text for w in ["不", "没", "停止", "反对", "讨厌"])
            if negations_new != negations_old and sim_score > 0.4:
                conflicts.append({
                    "existing_id": mem.get("id"),
                    "existing_text": existing_text[:200],
                    "similarity": round(sim_score, 3),
                    "note": "可能矛盾：一方含否定词"
                })
        if conflicts:
            self._log_conflicts(content, conflicts)
        return conflicts

    def _log_conflicts(self, new_content: str,
                       conflicts: list[dict[str, Any]]) -> None:
        """Write conflict info to log file for later review."""
        try:
            existing: list[dict] = []
            if os.path.exists(CONFLICT_LOG_PATH):
                with open(CONFLICT_LOG_PATH) as f:
                    existing = json.load(f)
            existing.append({
                "timestamp": datetime.now(UTC).isoformat(),
                "new_content": new_content[:200],
                "conflicts": conflicts,
            })
            # Keep last 100 entries
            if len(existing) > 100:
                existing = existing[-100:]
            with open(CONFLICT_LOG_PATH, "w") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.debug("Failed to log conflict: %s", e)

    # ── Memory extraction rules ─────────────────────────────────────────

    def _apply_extraction_rules(self, content: str) -> list[dict[str, Any]]:
        """Apply rules to extract structured info from content.

        Returns list of matched extractions:
            [{"rule": rule_name, "matched": trigger_word, "tag": tag}, ...]
        """
        if not content:
            return []
        rules = _load_rules()
        if not rules:
            return []
        results: list[dict[str, Any]] = []
        for rule_name, rule in rules.items():
            for trigger in rule.get("triggers", []):
                if trigger in content:
                    results.append({
                        "rule": rule_name,
                        "matched": trigger,
                        "tag": rule.get("tag", ""),
                    })
                    break  # one match per rule
        return results


# ── CLI entry points ─────────────────────────────────────────────────
def cli_store():
    """python3 memory_manager.py store <scene> <content> [--tags ...]"""
    mm = MemoryManager()
    scene = sys.argv[2] if len(sys.argv) > 2 else ""
    content = sys.argv[3] if len(sys.argv) > 3 else sys.stdin.read().strip()
    tags = []
    if "--tags" in sys.argv:
        idx = sys.argv.index("--tags")
        tags = sys.argv[idx+1:]
    result = mm.store(content, scene=scene, tags=tags)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cli_search():
    """python3 memory_manager.py search <query> [--scene ...]"""
    mm = MemoryManager()
    query = sys.argv[2] if len(sys.argv) > 2 else ""
    scene = ""
    if "--scene" in sys.argv:
        idx = sys.argv.index("--scene")
        scene = sys.argv[idx+1]
    results = mm.search(query, scene=scene)
    for r in results[:10]:
        print(f"  [{r.get('score', 0):.2f}] {r['text'][:120]}...")


def cli_daily_summary():
    """python3 memory_manager.py daily-summary [date]"""
    mm = MemoryManager()
    date_str = sys.argv[2] if len(sys.argv) > 2 else None
    result = mm.daily_summary(date_str)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cli_weekly_summary():
    """python3 memory_manager.py weekly-summary [year-week]"""
    mm = MemoryManager()
    week = sys.argv[2] if len(sys.argv) > 2 else None
    result = mm.weekly_summary(week)
    print(json.dumps(result, ensure_ascii=False, indent=2))


# ── Memory rules helpers ─────────────────────────────────────────────

def _load_rules() -> dict[str, Any]:
    """Load extraction rules from JSON file."""
    if os.path.exists(RULES_PATH):
        try:
            with open(RULES_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_rules(rules: dict[str, Any]) -> None:
    """Save extraction rules to JSON file."""
    os.makedirs(os.path.dirname(RULES_PATH), exist_ok=True)
    with open(RULES_PATH, "w") as f:
        json.dump(rules, f, indent=2, ensure_ascii=False)


def cli_session_summary():
    """python3 memory_manager.py session-summary [label] < memories.json"""
    mm = MemoryManager()
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        memories = json.load(sys.stdin)
    except Exception:
        print('{"success":false,"error":"stdin must be JSON array of memories"}')
        return
    result = mm.session_summary(memories, session_label=label)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cli_conflict_check():
    """python3 memory_manager.py conflict-check <content>"""
    mm = MemoryManager()
    content = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else sys.stdin.read().strip()
    if not content:
        print('{"success":false,"error":"No content provided"}')
        return
    scene = mm._infer_scene(content)
    tags = [SCENE_TAGS.get(scene, "project")]
    from memory_compressor import compress
    compressed = compress(content)
    conflicts = mm._check_conflicts(compressed, tags)
    print(json.dumps({
        "content": compressed[:200],
        "scene": scene,
        "conflicts": conflicts,
    }, ensure_ascii=False, indent=2))


def cli_rule_list():
    """python3 memory_manager.py rule-list"""
    rules = _load_rules()
    if not rules:
        print("No rules defined.")
        return
    for name, rule in rules.items():
        triggers = ", ".join(rule.get("triggers", []))
        print(f"  {name}: [{', '.join(rule.get('tags', []))}] triggers={triggers}")


def cli_rule_add():
    """python3 memory_manager.py rule-add <name> <trigger1,trigger2,...> <tag1,tag2,...>"""
    if len(sys.argv) < 5:
        print("Usage: memory_manager.py rule-add <name> <trigger1,t2,...> <tag1,tag2,...>")
        return
    name = sys.argv[2]
    triggers = [t.strip() for t in sys.argv[3].split(",") if t.strip()]
    tags = [t.strip() for t in sys.argv[4].split(",") if t.strip()]
    rules = _load_rules()
    rules[name] = {"triggers": triggers, "tags": tags}
    _save_rules(rules)
    print(json.dumps({"success": True, "action": "rule-added", "name": name,
                       "triggers": triggers, "tags": tags},
                      ensure_ascii=False, indent=2))


def cli_rule_remove():
    """python3 memory_manager.py rule-remove <name>"""
    if len(sys.argv) < 3:
        print("Usage: memory_manager.py rule-remove <name>")
        return
    name = sys.argv[2]
    rules = _load_rules()
    if name in rules:
        del rules[name]
        _save_rules(rules)
        print(json.dumps({"success": True, "action": "rule-removed", "name": name}))
    else:
        print(json.dumps({"success": False, "error": f"Rule '{name}' not found"}))


# ═══════════════════════════════════════════════════════════════════
# Phase 3: User Persona + Memory Variables
# ═══════════════════════════════════════════════════════════════════

MEMORY_VARS_PATH = os.path.join(os.path.dirname(__file__), "memory_vars.json")


def _load_memory_vars() -> dict[str, Any]:
    """Load memory variables from JSON file."""
    if os.path.exists(MEMORY_VARS_PATH):
        with open(MEMORY_VARS_PATH) as f:
            return json.load(f)
    return {}


def _save_memory_vars(vars_data: dict[str, Any]) -> None:
    """Save memory variables to JSON file."""
    with open(MEMORY_VARS_PATH, "w") as f:
        json.dump(vars_data, f, indent=2, ensure_ascii=False)


def cli_var_set():
    """python3 memory_manager.py var-set <key> <value> [--weight <float>]"""
    if len(sys.argv) < 4:
        print("Usage: memory_manager.py var-set <key> <value> [--weight <float>]")
        return
    key = sys.argv[2]
    value = sys.argv[3]
    weight = 1.0
    if "--weight" in sys.argv:
        idx = sys.argv.index("--weight")
        weight = float(sys.argv[idx + 1])

    vars_data = _load_memory_vars()
    vars_data[key] = {"value": value, "weight": weight}
    _save_memory_vars(vars_data)
    print(json.dumps({"success": True, "action": "var-set",
                       "key": key, "value": value, "weight": weight},
                      ensure_ascii=False, indent=2))


def cli_var_list():
    """python3 memory_manager.py var-list"""
    vars_data = _load_memory_vars()
    if not vars_data:
        print("No memory variables defined.")
        return
    for key, info in vars_data.items():
        print(f"  {key} = {info['value']}  (weight={info.get('weight', 1.0)})")


def cli_var_delete():
    """python3 memory_manager.py var-delete <key>"""
    if len(sys.argv) < 3:
        print("Usage: memory_manager.py var-delete <key>")
        return
    key = sys.argv[2]
    vars_data = _load_memory_vars()
    if key in vars_data:
        del vars_data[key]
        _save_memory_vars(vars_data)
        print(json.dumps({"success": True, "action": "var-deleted", "key": key}))
    else:
        print(json.dumps({"success": False, "error": f"Key '{key}' not found"}))


def cli_build_persona():
    """
    Build/refresh user persona from memories.
    Uses LLM to extract stable user attributes.

    python3 memory_manager.py build-persona
    """
    import subprocess as sp

    mm = MemoryManager()

    # Step 1: Collect memories about the user
    # Search for user-related memories across "dev" and "project" scenes
    relevant_memories = mm.search("user preference style", scene="dev", limit=30)
    relevant_memories += mm.search("青衫", scene="dev", limit=20)
    relevant_memories += mm.search("用户 偏好 习惯", scene="project", limit=20)

    # Dedup by content text
    seen = set()
    unique_texts = []
    for r in relevant_memories:
        t = r.get("text", "")[:500]
        if t and t not in seen:
            seen.add(t)
            unique_texts.append(t)

    if not unique_texts:
        return {"success": True, "action": "no_data", "message": "No user-related memories found yet"}

    # Step 2: Also load memory variables (explicit user settings)
    memory_vars = _load_memory_vars()
    var_context = ""
    if memory_vars:
        var_lines = [f"  - {k} = {v['value']} (weight={v.get('weight', 1.0)})"
                     for k, v in memory_vars.items()]
        var_context = "用户已明确的偏好（记忆变量，权重最高）：\n" + "\n".join(var_lines)

    # Step 3: Use hindsight API's LLM to analyze
    # Send as a recall-type analysis prompt to the API
    memories_text = "\n".join(f"{i+1}. {t}" for i, t in enumerate(unique_texts[:40]))

    persona_prompt = (
        "根据以下关于用户的记忆内容，提炼出稳定的用户画像。\n"
        "包括：姓名/称呼、职业、技能、沟通风格、偏好、活跃时段、决策风格。\n"
        "只输出提炼后的画像，不要额外评论。\n"
        "每条属性一行：属性名: 属性值\n\n"
        f"{var_context}\n\n"
        f"记忆内容：\n{memories_text}"
    )

    # Use hindsight reflect (which has LLM capability) to analyze
    reflect_result = mm._curl("POST", "/reflect", {
        "query": persona_prompt,
    })

    if reflect_result and "text" in reflect_result:
        persona_text = reflect_result["text"]
        # Store as persona document
        mm.store(
            f"【用户画像·{datetime.now(UTC).strftime('%Y-%m-%d')}】\n{persona_text}",
            scene="project",
            tags=["persona", f"persona:{datetime.now(UTC).strftime('%Y-%m-%d')}"],
            context="auto-persona",
            dedup=False,
        )
        return {"success": True, "action": "persona_built",
                "persona": persona_text[:500]}
    else:
        return {"success": False, "action": "llm_failed",
                "reflect_result": str(reflect_result)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: memory_manager.py <store|search|daily-summary|weekly-summary|"
              "session-summary|conflict-check|"
              "var-set|var-list|var-delete|build-persona|"
              "rule-list|rule-add|rule-remove> ...")
        sys.exit(1)

    command = sys.argv[1]
    commands = {
        "store": cli_store,
        "search": cli_search,
        "daily-summary": cli_daily_summary,
        "weekly-summary": cli_weekly_summary,
        "session-summary": cli_session_summary,
        "conflict-check": cli_conflict_check,
        "var-set": cli_var_set,
        "var-list": cli_var_list,
        "var-delete": cli_var_delete,
        "build-persona": cli_build_persona,
        "rule-list": cli_rule_list,
        "rule-add": cli_rule_add,
        "rule-remove": cli_rule_remove,
    }
    handler = commands.get(command)
    if handler:
        result = handler()
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
