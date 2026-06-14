#!/usr/bin/env python3
"""
记忆交叉验证引擎 — 主动质检，不依赖用户纠正信号

三阶段:
  Phase 1: 信号提取 — 对每条记忆算 specificity / verifiability / connectedness / recency
  Phase 2: 矛盾检测 — 按股票/配置/主题分组找冲突对
  Phase 3: 质量更新 — 综合评分写入 memory_quality.json

用法:
  python3 cross_validate_memories.py                  # 完整运行
  python3 cross_validate_memories.py --dry-run        # 预览不写入
  python3 cross_validate_memories.py --suspicions     # 只看怀疑清单
  python3 cross_validate_memories.py --bootstrap      # 全量初始化（覆盖所有未评分记忆）
"""
import json
import os
import re
import sys
import time
import math
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional
from urllib.request import Request, urlopen

HOMES_DIR = os.path.expanduser("~/.hermes")
API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"
QUALITY_FILE = os.path.join(HOMES_DIR, "hindsight", "memory_quality.json")
CALIBRATE_SCRIPT = os.path.join(HOMES_DIR, "hindsight", "calibrate_memory.py")
DOUBT_LIST = os.path.join(HOMES_DIR, "hindsight", "dreaming_doubt_list.json")

# —— 来源置信度分级 ——
# 基础分 × 时间衰减 + 微调因子 = 最终质量分
SOURCE_BASE = {
    "soul": 0.85,       # S级: SOUL.md — 配置文件，你亲自反复迭代
    "user": 0.80,       # A级: USER.md — 用户自画像
    "agent": 0.75,      # B级: AGENTS.md / CLAUDE.md / 项目规范
    "obsidian": 0.65,   # C级: Obsidian 知识库（半 curated）
    "hindsight": 0.50,  # D级: Hindsight 自动记忆（对话存档）
    "daily": 0.40,      # E级: 每日摘要 / 批量生成（机器产物）
    "default": 0.45,    # 其他
}

# 各来源半衰期（天）— 半衰期内分数衰减到一半
SOURCE_HALF_LIFE = {
    "soul": 180,
    "user": 120,
    "agent": 90,
    "obsidian": 60,
    "hindsight": 30,
    "daily": 14,
    "default": 45,
}

# —— 微调因子（在基础分±范围内调整） ——
ADJ_SPECIFICITY = 0.10    # 文本具体程度 ±0.10
ADJ_CONNECTED = 0.06      # 关联度 ±0.06
ADJ_CONFIRMED = 0.08      # 用户显式确认 +0.08
PENALTY_CONTRADICTION = 0.02  # 每条矛盾 -0.02（上限0.15）

# —— 用户验证过的文档（高置信度参考依据） ——
RE_SOUL = re.compile(r'SOUL\.?[Mm][Dd]', re.IGNORECASE)
RE_USER_DOC = re.compile(r'(SOUL\.?[Mm][Dd]|USER\.?[Mm][Dd]|AGENTS\.?[Mm][Dd]|CLAUDE\.?[Mm][Dd])')

# —— 信号正则 ——
RE_STOCK_CODE = re.compile(r'\b[036]\d{5}\b')  # A股代码
RE_PATH = re.compile(r'(?:/home/[\w./-]+|~[\w./-]+)')
RE_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
RE_NUMBER = re.compile(r'\d+\.?\d*')
RE_VAGUE = re.compile(r'(好像|可能|似乎|maybe|perhaps|大概|估计|猜测|我觉得|我认为)')
RE_NEGATION = re.compile(r'(已清仓|已卖出|已平仓|不再持有|清掉|不再关注|错误|错的|不正确|过期)')
RE_AFFIRMATION = re.compile(r'(持有|加仓|买入|建仓|持仓|看好|不错|正确|确认|已验证)')


# ════════════════════════════════════════════════════════════════════
# Phase 1: 信号提取
# ════════════════════════════════════════════════════════════════════

def api_get_all(limit: int = 500) -> list[dict]:
    """从 Hindsight API 拉取全部记忆（分页）。"""
    all_items = []
    offset = 0
    while True:
        url = f"{API_BASE}/memories/list?limit={limit}&offset={offset}"
        try:
            resp = urlopen(url, timeout=15)
            data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"  ⚠️  API 请求失败: {e}")
            break
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)
        offset += len(items)
        if offset >= data.get("total", offset):
            break
    return all_items


def compute_specificity(mem: dict) -> float:
    """文本具体程度 0~1。

    把基准从 0.5 降到 0.4，让信号差异拉开。
    """
    text = mem.get("text", "") or ""
    tags = mem.get("tags", [])
    score = 0.4  # 低基准

    # 正向信号（累积）
    if RE_STOCK_CODE.search(text):
        score += 0.20  # 有股票代码 → 很具体
    if RE_PATH.search(text):
        score += 0.15  # 有路径 → 可追溯
    if RE_DATE.search(text):
        score += 0.08
    if RE_USER_DOC.search(text):
        score += 0.18  # 引用用户验证过的文档(SOUL.md等) → 高置信
    if len(text) > 120:
        score += 0.10
    elif len(text) > 60:
        score += 0.05
    # 实体字段非空
    if mem.get("entities", ""):
        score += 0.10
    # 显式前缀标记
    if "[CORE]" in text or "[LTM]" in text:
        score += 0.15

    # 负向信号
    if RE_VAGUE.search(text.lower()):
        score -= 0.20  # 模糊词 → 强力扣分
    if len(text) < 30:
        score -= 0.15  # 太短 → 信息量低
    if "auto-memory" in tags and not ("[CORE]" in text or "[LTM]" in text):
        score -= 0.10  # 纯自动记忆，无人工标记
    # 只有1个tag (通常是 auto-memory) → 信息少
    if len(tags) <= 1:
        score -= 0.08

    return max(0.0, min(1.0, score))


def compute_verifiability(mem: dict) -> float:
    """可验证性 0~1。路径/配置/代码/用户文档可被实际验证。"""
    text = mem.get("text", "") or ""
    score = 0.5

    # 检查是否引用 SOUL.md 等用户验证过的文档
    if RE_USER_DOC.search(text):
        # SOUL.md 等文件被用户反复迭代 → 高置信
        soul_paths = [
            os.path.expanduser("~/.hermes/SOUL.md"),
            os.path.expanduser("~/.hermes/profiles/xiaoma/SOUL.md"),
            os.path.expanduser("~/.hermes/profiles/xiaojin/SOUL.md"),
            os.path.expanduser("~/.hermes/profiles/xiaozhuan/SOUL.md"),
        ]
        exists = sum(1 for p in soul_paths if os.path.exists(p))
        if exists == len(soul_paths):
            score = 0.85  # 全部存在 → 记忆引用的是活跃文档
        elif exists > 0:
            score = 0.70  # 部分存在
        else:
            score = 0.50  # 都不存在（不太可能）
    else:
        # 没有SOUL引用时，才检查文件路径是否存在
        paths = RE_PATH.findall(text)
        if paths:
            exist_count = 0
            for p in paths:
                expanded = os.path.expanduser(p)
                target = expanded.split(" ")[0]
                if os.path.exists(target):
                    exist_count += 1
            ratio = exist_count / len(paths) if paths else 0
            score = 0.5 + 0.4 * ratio  # 全在=0.9，全不在=0.5

    return max(0.0, min(1.0, score))


def compute_connectedness(mem: dict, tag_clusters: dict) -> float:
    """关联度 0~1。跟其他记忆共享的tag越多 → 越靠谱。"""
    tags = set(mem.get("tags", []))
    if not tags:
        return 0.3  # 孤立，低分

    # 算重叠度：有多少其他记忆共享这些tag
    connected_count = 0
    for tag in tags:
        connected_count += len(tag_clusters.get(tag, set())) - 1  # 减掉自己
    if connected_count <= 0:
        return 0.3

    # 归一化：到 100 就算满分
    return min(1.0, connected_count / 100 * 0.7 + 0.3)


def compute_recency(mem: dict) -> float:
    """时效性 0~1。7天内=满分，指数衰减。"""
    date_str = mem.get("date") or mem.get("mentioned_at")
    if not date_str:
        return 0.5
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        days_ago = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except (ValueError, TypeError):
        return 0.5

    if days_ago < 0:
        return 1.0  # 未来时间（有时区问题），保守满分
    if days_ago <= 7:
        return 1.0
    # 指数衰减: 30天→0.8, 90天→0.5, 180天→0.3
    return max(0.1, math.exp(-days_ago / 90) * 1.0)


def extract_entity(mem: dict) -> str:
    """从记忆中提取核心实体，用于矛盾检测分组。

    优先级: stock_code > entities字段 > 核心tag > 文本关键词
    避免用文本前N字这种容易误分的策略。
    """
    text = mem.get("text", "") or ""
    entities = mem.get("entities", "") or ""
    tags = mem.get("tags", [])

    # 1) 股票代码最精确
    stock_match = RE_STOCK_CODE.search(text)
    if stock_match:
        return f"stock:{stock_match.group()}"

    # 2) 实体字段中第一个实体
    if entities:
        parts = [e.strip() for e in entities.replace("，", ",").split(",")]
        clean = [p for p in parts if len(p) > 1]  # 过滤单字
        if clean:
            return f"entity:{clean[0][:30]}"

    # 3) entity|object:xxx 类tag
    obj_tags = [t for t in tags if t.startswith("entity|object:")]
    if obj_tags:
        return obj_tags[0]

    # 4) entity|concept:xxx 类tag
    concept_tags = [t for t in tags if t.startswith("entity|concept:")]
    if concept_tags:
        return concept_tags[0]

    # 5) 文本中提取股票名（A股通常带数字/公司名）
    company_pattern = re.compile(r'[^\d\s]{2,6}(?:股份|集团|科技|医药|银行|证券|能源|制造|智能|数据)')
    company_match = company_pattern.search(text)
    if company_match:
        return f"company:{company_match.group()}"

    # 6) 项目路径
    path_match = RE_PATH.search(text)
    if path_match:
        short = path_match.group().replace("/home/john/", "").replace("~", "")
        return f"path:{short[:40]}"

    # 7) 兜底：用tag中的第一个非通用tag
    generic_tags = {"auto-memory", "daily-summary", "refill", "dev", "session-summary"}
    for t in tags:
        if t not in generic_tags and not t.startswith("date:") and not t.startswith("session:"):
            return f"tag:{t[:30]}"

    return None  # 无法分组


# ════════════════════════════════════════════════════════════════════
# 实体类型判断（用于矛盾检测过滤）
# ════════════════════════════════════════════════════════════════════

TRADING_KEYWORDS = {
    "持仓", "买入", "卖出", "止损", "止盈", "加仓", "减仓", "清仓",
    "涨停", "跌停", "K线", "k线", "均线", "BOLL", "ADX", "MACD",
    "量能", "放量", "缩量", "成交量", "换手率", "趋势", "突破",
    "反弹", "回踩", "支撑", "压力", "仓位", "浮亏", "浮盈",
    "技术面", "基本面", "策略", "回测", "评分", "评级",
    "减持", "增持", "持仓股", "新仓", "建仓",
}

STOCK_ENTITY_PREFIXES = {"stock:", "company:stock:", "entity|stock:"}
NON_TRADING_ENTITY_PREFIXES = {
    "entity|concept:auto_retain", "entity:用户", "entity:User",
    "entity:Hermes Agent", "entity:助理", "entity:MCTS",
    "entity:MongoDB", "entity:Hindsight", "entity:WebUI",
    "entity:LMDB", "entity|object:sync_turn",
    "entity:db_schema_reference.md", "entity:state.db",
    "entity|object:balance_sheet", "entity|object:project_update",
    "entity|object:memory_arch", "entity|object:skills",
}


def _is_trading_entity(entity: str, group: list[dict]) -> tuple[bool, str]:
    """判断实体组是否适合做矛盾检测（仅股票相关实体需要）。"""
    # 1. 实体名前缀检查
    if any(entity.startswith(p) for p in STOCK_ENTITY_PREFIXES):
        return True, "stock_prefix"
    if entity.startswith("company:"):
        return True, "company_name"

    # 2. 已知的非交易实体直接跳过
    for prefix in NON_TRADING_ENTITY_PREFIXES:
        if entity == prefix or entity.startswith(prefix):
            return False, "known_non_trading"

    # 3. 文本内容检查：如果组内大部分成员含交易关键词，视为交易实体
    trading_count = 0
    for m in group:
        text = (m.get("text", "") or "").lower()
        if any(kw in text for kw in TRADING_KEYWORDS):
            trading_count += 1
    ratio = trading_count / max(len(group), 1)
    if ratio >= 0.6:
        return True, "trading_content"

    # 4. 按实体名前缀分类
    if entity.startswith("entity|concept:") or entity.startswith("entity|object:"):
        # 概念/对象记忆 → 不是交易实体
        return False, "concept_object"
    if entity.startswith("entity:"):
        # entity:xxx — 如果有股票代码或交易关键词才判定为交易
        code_pattern = re.compile(r'[036]\d{5}')
        for m in group:
            if code_pattern.search(m.get("text", "") or ""):
                return True, "entity_with_stock_code"
        return False, "general_entity"
    if entity.startswith("tag:"):
        return False, "tag_fallback"
    if entity.startswith("path:"):
        return False, "path_entity"

    # 5. 兜底：非交易实体
    return False, "default"


# ════════════════════════════════════════════════════════════════════
# 时间感知工具
# ════════════════════════════════════════════════════════════════════

def _extract_date(mem: dict) -> Optional[str]:
    """提取记忆的日期字符串（YYYY-MM-DD）。"""
    date_str = mem.get("date") or mem.get("mentioned_at")
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _same_calendar_day(m1: dict, m2: dict) -> bool:
    """两条记忆是否在同一天。"""
    d1 = _extract_date(m1)
    d2 = _extract_date(m2)
    if d1 is None or d2 is None:
        return True  # 无时间戳视为同天（不跳过矛盾检测）
    return d1 == d2


def _date_distance_days(mem: dict) -> Optional[int]:
    """记忆距今的天数（仅用于统计输出）。"""
    date_str = mem.get("date") or mem.get("mentioned_at")
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - dt).days
    except (ValueError, TypeError):
        return None


def detect_contradictions(memories: list[dict], max_group_size: int = 20) -> dict[str, list[str]]:
    """矛盾检测 — 按实体分组，找冲突对。

    仅对股票/交易相关实体做矛盾检测，其他实体只记录分组不触发惩罚。
    返回: {实体: [矛盾方记忆ID列表]}
    """
    contradictions = defaultdict(list)
    entity_groups = defaultdict(list)

    for m in memories:
        entity = extract_entity(m)
        if entity is None:
            continue  # 无法分组的记忆不参与矛盾检测
        entity_groups[entity].append(m)

    # 限制分组大小：太大的组（通用tag）不做矛盾检测
    filtered_groups = {e: g for e, g in entity_groups.items()
                       if 2 <= len(g) <= max_group_size}

    contradictions = defaultdict(list)
    trading_groups = 0
    skipped_groups = 0
    temporal_skipped = 0
    true_contradictions = 0

    for entity, group in filtered_groups.items():
        if len(group) < 2:
            continue

        # 判断是否为交易实体
        is_trading, reason = _is_trading_entity(entity, group)
        if not is_trading:
            skipped_groups += 1
            continue  # 非交易实体跳过矛盾检测
        trading_groups += 1

        # 对每个组内成员，检查是否存在否定 vs 肯定的冲突
        for i, m1 in enumerate(group):
            t1 = (m1.get("text", "") or "").lower()
            has_neg1 = bool(RE_NEGATION.search(t1))
            has_aff1 = bool(RE_AFFIRMATION.search(t1))

            for j, m2 in enumerate(group):
                if j <= i:
                    continue
                t2 = (m2.get("text", "") or "").lower()
                has_neg2 = bool(RE_NEGATION.search(t2))
                has_aff2 = bool(RE_AFFIRMATION.search(t2))

                # 一个说有、一个说没有 → 矛盾对
                if (has_neg1 and has_aff2) or (has_aff1 and has_neg2):
                    # 时间感知：不同天的冲突视为时序快照，不标记为矛盾
                    if not _same_calendar_day(m1, m2):
                        temporal_skipped += 1
                        continue  # 不同天，是持仓变化，不是矛盾
                    true_contradictions += 1
                    contradictions[m1["id"]].append(m2["id"])
                    contradictions[m2["id"]].append(m1["id"])

    print(f"   分组: {len(entity_groups)} 个实体, "
          f"{len(filtered_groups)} 个可检测组 "
          f"(排除 {len(entity_groups)-len(filtered_groups)} 个过大组)")
    print(f"   交易实体组: {trading_groups}, 跳过: {skipped_groups} (非交易实体)")
    if temporal_skipped > 0:
        print(f"   时间感知过滤: {temporal_skipped} 对（不同天的持仓变化，视为快照不标记）")
    print(f"   最终矛盾对: {true_contradictions}")
    return dict(contradictions)


def get_memory_age(mem: dict) -> int:
    """获取记忆天数（从创建到现在的天数，不满1天算0）。"""
    date_str = mem.get("date") or mem.get("mentioned_at")
    if not date_str:
        return 0
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        return max(0, int(days))
    except (ValueError, TypeError):
        return 0


def detect_source(mem: dict) -> str:
    """检测记忆来源层级。

    返回 SOURCE_BASE 中的一个 key。
    优先级: 标签(排除性) > 文本(包含性) > 标签(包含性)
    
    关键原则：日报/摘要类记忆即使文本引用了配置文件，
    仍归为 daily 级别（因为它们是机器批量产物，不是配置本身）。
    """
    text = mem.get("text", "") or ""
    tags = set(mem.get("tags", []))

    # 0) 排除式检查：先检查已知的批量/机器生成标签
    if "daily-summary" in tags or "session-summary" in tags:
        return "daily"
    if "auto-memory" in tags and not ("[CORE]" in text or "[LTM]" in text):
        return "hindsight"

    # 1) S级: SOUL.md — 配置文件
    if RE_SOUL.search(text) or \
       "entity|object:profile_setup" in tags or \
       "entity|object:config_change" in tags:
        return "soul"

    # 2) A级: USER.md
    if "USER.md" in text or "USER.MD" in text:
        return "user"

    # 3) B级: AGENTS.md / CLAUDE.md / 项目规则
    if "AGENTS.md" in text or "CLAUDE.md" in text or "DEVELOPMENT_RULES" in text:
        return "agent"
    if "entity|object:project_rules" in tags or "entity|object:project_update" in tags:
        return "agent"

    # 4) C级: Obsidian 知识库
    if any(t.startswith("ref_obsidian") for t in tags) or "obsidian" in tags:
        return "obsidian"

    return "default"


# ════════════════════════════════════════════════════════════════════
# Phase 2: 综合评分（来源锚定 + 时间衰减 + 微调）
# ════════════════════════════════════════════════════════════════════

def assign_quality(mem: dict, specifics: dict, verifiables: dict,
                   connectedness: dict, contradictions: dict,
                   tag_clusters: dict) -> float:
    """三段式质量分：来源锚定值 × 时间衰减 + 微调因子。

    1. 来源锚定：检测记忆来源，查表得基础分 + 半衰期
    2. 时间衰减：按记忆天数指数衰减（半衰期内降到一半）
    3. 微调：specificity / connectedness / contradiction 小幅度修正
    """
    mid = mem["id"]

    # 1️⃣ 来源锚定
    source = detect_source(mem)
    base = SOURCE_BASE.get(source, 0.45)
    half_life = SOURCE_HALF_LIFE.get(source, 45)

    # 2️⃣ 时间衰减
    days = get_memory_age(mem)
    decay = math.exp(-days / half_life * math.log(2))  # 半衰期公式
    quality = base * decay

    # 3️⃣ 微调：具体程度
    spec = specifics.get(mid, 0.5)
    spec_tuning = (spec - 0.5) * ADJ_SPECIFICITY * 2  # 0~1映射到±0.10
    quality += spec_tuning

    # 4️⃣ 微调：关联度
    conn = connectedness.get(mid, 0.5)
    conn_tuning = (conn - 0.5) * ADJ_CONNECTED * 2  # 0~1映射到±0.06
    quality += conn_tuning

    # 5️⃣ 微调：矛盾惩罚
    if mid in contradictions:
        contra_count = len(contradictions[mid])
        quality -= min(0.15, contra_count * PENALTY_CONTRADICTION)

    # 6️⃣ 保底：被用户显式确认过的记忆不衰减到 0.25 以下
    # （用户确认过的记忆，即使很旧也保留基本分）
    if source in ("soul", "user") and quality < 0.25:
        quality = 0.25

    return max(0.05, min(0.95, quality))


# ════════════════════════════════════════════════════════════════════
# Phase 3: 写入质量文件
# ════════════════════════════════════════════════════════════════════

def load_existing_quality() -> dict:
    if os.path.exists(QUALITY_FILE):
        with open(QUALITY_FILE) as f:
            data = json.load(f)
        return data.get("memories", {})
    return {}


def save_quality(quality: dict):
    data = {
        "version": 2,
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "memories": quality,
    }
    with open(QUALITY_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"   写入 {len(quality)} 条到 {QUALITY_FILE}")


def build_suspicion_report(memories: list[dict], quality: dict,
                           contradictions: dict) -> list[dict]:
    """生成怀疑清单 — 低分记忆 + 矛盾对。

    quality 可以是 {id: float}（原始评分）或 {id: dict}（质量文件格式），
    兼容两种调用场景。
    """
    suspicions = []
    seen_ids = set()  # 去重

    # 低分记忆 (< 0.35)
    for m in memories:
        mid = m["id"]
        if mid in seen_ids:
            continue
        q_raw = quality.get(mid, 0.5)
        if isinstance(q_raw, dict):
            q = q_raw.get("quality", 0.5)
        else:
            q = q_raw
        if q < 0.35:
            text = (m.get("text", "") or "")[:100]
            seen_ids.add(mid)
            suspicions.append({
                "id": mid,
                "quality": q,
                "reason": f"低置信度({q:.2f})",
                "text": text,
                "tags": m.get("tags", [])[:3],
            })

    # 矛盾对（跳过已在怀疑清单中的 ID）
    for mid, conflicting in contradictions.items():
        if mid in seen_ids:
            continue
        if len(conflicting) > 0:
            m = next((x for x in memories if x["id"] == mid), None)
            if m:
                text = (m.get("text", "") or "")[:80]
                q_raw = quality.get(mid, 0.5)
                if isinstance(q_raw, dict):
                    q = q_raw.get("quality", "?")
                else:
                    q = q_raw
                suspicions.append({
                    "id": mid,
                    "quality": q,
                    "reason": f"矛盾(与{len(conflicting)}条冲突)",
                    "text": text,
                    "conflicts_with": conflicting[:3],
                })

    return suspicions


# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════

def main():
    dry_run = "--dry-run" in sys.argv
    show_suspicions_only = "--suspicions" in sys.argv
    bootstrap_mode = "--bootstrap" in sys.argv
    export_suspicions = "--export-suspicions" in sys.argv

    print(f"{'🔍 记忆交叉验证' + (' [DRY RUN]' if dry_run else '')}")
    print(f"时间: {datetime.now(timezone.utc).isoformat()}\n")

    # Step 1: 拉取记忆
    print("📥 拉取 Hindsight API 记忆...")
    memories = api_get_all()
    print(f"   获取 {len(memories)} 条记忆\n")

    # Step 2: 计算信号
    print("📊 Phase 1: 计算质量信号...")

    # 构建 tag 集群（用于 connectedness）
    tag_clusters = defaultdict(set)
    for m in memories:
        for t in m.get("tags", []):
            tag_clusters[t].add(m["id"])

    specifics = {}
    verifiables = {}
    connectedness = {}
    for m in memories:
        mid = m["id"]
        specifics[mid] = compute_specificity(m)
        verifiables[mid] = compute_verifiability(m)
        connectedness[mid] = compute_connectedness(m, tag_clusters)

    print(f"   specificity: min={min(specifics.values()):.2f}, "
          f"max={max(specifics.values()):.2f}, "
          f"avg={sum(specifics.values())/len(specifics):.2f}")
    print(f"   verifiability: min={min(verifiables.values()):.2f}")
    print(f"   connectedness: min={min(connectedness.values()):.2f}, "
          f"max={max(connectedness.values()):.2f}")

    # Step 3: 矛盾检测
    print("\n🔀 Phase 2: 矛盾检测...")
    contradictions = detect_contradictions(memories)
    contra_mems = set(contradictions.keys())
    print(f"   发现 {len(contra_mems)} 条记忆涉及矛盾")

    # Step 4: 计算综合质量分
    print("\n⚖️ Phase 3: 综合评分...")
    quality_scores = {}
    for m in memories:
        mid = m["id"]
        q = assign_quality(m, specifics, verifiables, connectedness,
                           contradictions, tag_clusters)
        quality_scores[mid] = q

    print(f"   质量分布:")
    buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
    for q in quality_scores.values():
        if q < 0.2: buckets["0.0-0.2"] += 1
        elif q < 0.4: buckets["0.2-0.4"] += 1
        elif q < 0.6: buckets["0.4-0.6"] += 1
        elif q < 0.8: buckets["0.6-0.8"] += 1
        else: buckets["0.8-1.0"] += 1
    for k, v in buckets.items():
        bar = "█" * max(1, v * 40 // len(memories))
        print(f"     {k}: {v:4d} {bar}")

    # Step 5: 调查疑清单
    if show_suspicions_only or dry_run or export_suspicions:
        existing = load_existing_quality()
        reports = build_suspicion_report(memories, quality_scores, contradictions)
        print(f"\n🔎 怀疑清单 ({len(reports)} 条):")
        for r in sorted(reports, key=lambda x: x["quality"] if isinstance(x["quality"], float) else 0)[:20]:
            q_str = f"q={r['quality']:.2f}" if isinstance(r['quality'], float) else f"q={r['quality']}"
            print(f"  [{r['id']}] {q_str} | {r['reason']}")
            print(f"     {r['text'][:70]}")
            if "conflicts_with" in r:
                print(f"     ↕ 冲突: {', '.join(r['conflicts_with'])}")
            print()

        # --export-suspicions: 写入怀疑清单供 calibrate_runner.py 消费
        if export_suspicions:
            doubt_data = {
                "suspicions": reports[:50],  # 最多50条，防止过大
                "confirmed": [],
                "corrected": [],
                "total_suspicions": len(reports),
                "low_quality_count": sum(1 for r in reports if "低置信度" in r.get("reason", "")),
                "contradiction_count": sum(1 for r in reports if "矛盾" in r.get("reason", "")),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            try:
                # 保留已有的 confirmed/corrected（来自之前的 LLM 审查结果）
                if os.path.exists(DOUBT_LIST):
                    with open(DOUBT_LIST) as f:
                        existing_doubt = json.load(f)
                    doubt_data["confirmed"] = existing_doubt.get("confirmed", [])
                    doubt_data["corrected"] = existing_doubt.get("corrected", [])
                with open(DOUBT_LIST, "w") as f:
                    json.dump(doubt_data, f, indent=2, ensure_ascii=False)
                print(f"📝 已导出 {len(reports)} 条怀疑到 {DOUBT_LIST}")
            except Exception as e:
                print(f"  ⚠️ 导出怀疑清单失败: {e}")

        if show_suspicions_only:
            return 0

    # Step 6: 更新 quality 文件
    if dry_run:
        print("\n⏸️   Dry-run 模式，未写入")
        return 0

    existing = load_existing_quality()
    updated = dict(existing)  # 保留已有的（如 visit_count, status 等）

    updates_count = 0
    for m in memories:
        mid = m["id"]
        q = quality_scores[mid]

        if mid in updated:
            old_q = updated[mid].get("quality", 0.5)
            # 如果已有自定义分（非 0.5），保留，除非 bootstrap 模式
            if not bootstrap_mode and abs(old_q - 0.5) > 0.01:
                continue  # 已有质量分的不覆盖
            # 更新
            updated[mid]["quality"] = round(q, 4)
            if "auto_assessed" not in updated[mid]:
                updated[mid]["auto_assessed"] = True
            updates_count += 1
        else:
            # 新增
            text = (m.get("text", "") or "")[:100]
            tags = m.get("tags", [])[:5]
            updated[mid] = {
                "quality": round(q, 4),
                "status": "active",
                "visit_count": 0,
                "text_preview": text,
                "tags": tags,
                "auto_assessed": True,
            }
            updates_count += 1

    save_quality(updated)
    print(f"\n✅ 已更新 {updates_count} 条记忆的质量分")
    print(f"   当前总量: {len(updated)}")

    # 最终统计
    qs = [v["quality"] for v in updated.values()]
    print(f"   平均质量: {sum(qs)/len(qs):.4f}")
    print(f"   唯一分值: {len(set(round(q,4) for q in qs))}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
