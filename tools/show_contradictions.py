#!/usr/bin/env python3
"""提取矛盾记忆对，生成人类可读的摘要。"""
import json, os, re
from collections import defaultdict
from urllib.request import urlopen

API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"
QUALITY_FILE = os.path.expanduser("~/.hermes/hindsight/memory_quality.json")

RE_NEGATION = re.compile(r'(已清仓|已卖出|已平仓|不再持有|清掉|不再关注|错误|错的|不正确|过期)')
RE_AFFIRMATION = re.compile(r'(持有|加仓|买入|建仓|持仓|看好|不错|正确|确认|已验证)')
RE_STOCK_CODE = re.compile(r'\b[036]\d{5}\b')


def api_get_all():
    """拉取全部记忆。"""
    all_items = []
    offset = 0
    while True:
        url = f"{API_BASE}/memories/list?limit=500&offset={offset}"
        try:
            resp = urlopen(url, timeout=15)
            data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"  ⚠️  API 请求失败: {e}", file=sys.stderr)
            break
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)
        offset += len(items)
        if offset >= data.get("total", offset):
            break
    return all_items


def load_quality():
    if os.path.exists(QUALITY_FILE):
        with open(QUALITY_FILE) as f:
            data = json.load(f)
        return data.get("memories", {})
    return {}


def extract_entity(mem):
    text = mem.get("text", "") or ""
    entities = mem.get("entities", "") or ""
    tags = mem.get("tags", [])
    stock_match = RE_STOCK_CODE.search(text)
    if stock_match:
        return f"stock:{stock_match.group()}"
    if entities:
        parts = [e.strip() for e in entities.replace("，", ",").split(",")]
        clean = [p for p in parts if len(p) > 1]
        if clean:
            return f"entity:{clean[0][:30]}"
    obj_tags = [t for t in tags if t.startswith("entity|object:")]
    if obj_tags:
        return obj_tags[0]
    concept_tags = [t for t in tags if t.startswith("entity|concept:")]
    if concept_tags:
        return concept_tags[0]
    company_pattern = re.compile(r'[^\d\s]{2,6}(?:股份|集团|科技|医药|银行|证券|能源|制造|智能|数据)')
    company_match = company_pattern.search(text)
    if company_match:
        return f"company:{company_match.group()}"
    return None


def detect_contradictions(memories, max_group_size=20):
    contradictions = defaultdict(list)
    entity_groups = defaultdict(list)
    for m in memories:
        entity = extract_entity(m)
        if entity is None:
            continue
        entity_groups[entity].append(m)
    filtered_groups = {e: g for e, g in entity_groups.items() if 2 <= len(g) <= max_group_size}
    contradictions = defaultdict(list)
    for entity, group in filtered_groups.items():
        if len(group) < 2:
            continue
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
                if (has_neg1 and has_aff2) or (has_aff1 and has_neg2):
                    contradictions[m1["id"]].append(m2["id"])
                    contradictions[m2["id"]].append(m1["id"])
    return dict(contradictions), entity_groups, filtered_groups


def main():
    print("📥 拉取全部记忆...", file=sys.stderr)
    memories = api_get_all()
    print(f"   获取 {len(memories)} 条记忆\n", file=sys.stderr)

    quality = load_quality()

    print("🔀 矛盾检测...", file=sys.stderr)
    contradictions, all_groups, filtered_groups = detect_contradictions(memories)

    mem_by_id = {m["id"]: m for m in memories}

    # 找所有涉及矛盾的记忆
    contra_ids = set(contradictions.keys())
    print(f"   发现 {len(contra_ids)} 条记忆涉及矛盾\n", file=sys.stderr)

    # 按实体分组显示
    # 反向映射：记忆ID → 实体
    id_to_entity = {}
    for m in memories:
        e = extract_entity(m)
        if e:
            id_to_entity[m["id"]] = e

    # 按实体分组矛盾对
    entity_contra = defaultdict(lambda: {"ids": set(), "neg": [], "aff": []})
    for mid in contra_ids:
        e = id_to_entity.get(mid, "unknown")
        entity_contra[e]["ids"].add(mid)
        text = (mem_by_id[mid].get("text", "") or "").lower()
        if RE_NEGATION.search(text):
            entity_contra[e]["neg"].append(mid)
        if RE_AFFIRMATION.search(text):
            entity_contra[e]["aff"].append(mid)

    # 按矛盾数量排序
    sorted_entities = sorted(entity_contra.items(), key=lambda x: len(x[1]["ids"]), reverse=True)

    total_contra_pairs = sum(len(v) for v in contradictions.values()) // 2

    print(f"===== 矛盾记忆摘要 =====")
    print(f"总涉及记忆: {len(contra_ids)} 条")
    print(f"矛盾对数量: ~{total_contra_pairs} 对")
    print(f"涉及实体组: {len(sorted_entities)} 个")
    print()

    for entity, data in sorted_entities:
        print(f"━━━ [{entity}] — {len(data['ids'])} 条参与矛盾 ━━━")
        for mid in sorted(data["ids"]):
            m = mem_by_id.get(mid, {})
            text = (m.get("text", "") or "")[:120]
            q_entry = quality.get(mid, {})
            q = q_entry.get("quality", "?") if isinstance(q_entry, dict) else q_entry
            tags = m.get("tags", [])[:3]

            # 标记哪方
            side = ""
            full_text = (m.get("text", "") or "").lower()
            if RE_NEGATION.search(full_text):
                side = " ❌否定方"
            elif RE_AFFIRMATION.search(full_text):
                side = " ✅肯定方"

            # 引出矛盾ID
            conflict_ids = contradictions.get(mid, [])
            conflict_summary = ""
            if conflict_ids:
                cid = conflict_ids[0][:16]
                c = mem_by_id.get(conflict_ids[0], {})
                ctext = (c.get("text", "") or "")[:60]
                conflict_summary = f"\n     ↕ 冲突方: [{cid}] {ctext}"

            print(f"  [{mid[:16]}]{side} q={q}")
            print(f"     {text}")
            if conflict_summary:
                print(conflict_summary)
            print()

    print(f"===== 统计 =====")
    print(f"可检测实体组: {len(filtered_groups)} / {len(all_groups)}")
    print(f"矛盾活跃实体: {len(sorted_entities)}")
    print(f"矛盾记忆总数: {len(contra_ids)}")


if __name__ == "__main__":
    import sys
    main()
