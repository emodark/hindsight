#!/usr/bin/env python3
"""输出矛盾记忆的machine-readable ID列表，便于批量处理。"""
import json, os, re
from collections import defaultdict
from urllib.request import urlopen

API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"
QUALITY_FILE = os.path.expanduser("~/.hermes/hindsight/memory_quality.json")

RE_NEGATION = re.compile(r'(已清仓|已卖出|已平仓|不再持有|清掉|不再关注|错误|错的|不正确|过期)')
RE_AFFIRMATION = re.compile(r'(持有|加仓|买入|建仓|持仓|看好|不错|正确|确认|已验证)')
RE_STOCK_CODE = re.compile(r'\b[036]\d{5}\b')
RE_COMPANY = re.compile(r'[^\d\s]{2,6}(?:股份|集团|科技|医药|银行|证券|能源|制造|智能|数据)')


def api_get_all():
    all_items = []
    offset = 0
    while True:
        url = f"{API_BASE}/memories/list?limit=500&offset={offset}"
        try:
            resp = urlopen(url, timeout=15)
            data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"⚠️  API error at offset {offset}: {e}", file=sys.stderr)
            break
        items = data.get("items", [])
        if not items:
            break
        all_items.extend(items)
        offset += len(items)
        if offset >= data.get("total", offset):
            break
    return all_items


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
    for t in tags:
        if t.startswith("entity|object:") or t.startswith("entity|concept:"):
            return t
    company_match = RE_COMPANY.search(text)
    if company_match:
        return f"company:{company_match.group()}"
    return None


def detect_contradictions(memories):
    entity_groups = defaultdict(list)
    for m in memories:
        entity = extract_entity(m)
        if entity:
            entity_groups[entity].append(m)
    contradictions = defaultdict(list)
    for entity, group in entity_groups.items():
        if len(group) < 2:
            continue
        for i, m1 in enumerate(group):
            t1 = (m1.get("text", "") or "").lower()
            h1n = bool(RE_NEGATION.search(t1))
            h1a = bool(RE_AFFIRMATION.search(t1))
            for j, m2 in enumerate(group):
                if j <= i:
                    continue
                t2 = (m2.get("text", "") or "").lower()
                h2n = bool(RE_NEGATION.search(t2))
                h2a = bool(RE_AFFIRMATION.search(t2))
                if (h1n and h2a) or (h1a and h2n):
                    contradictions[m1["id"]].append(m2["id"])
                    contradictions[m2["id"]].append(m1["id"])
    return dict(contradictions), entity_groups


def classify_entry(mem, contra_ids):
    """分类矛盾记忆。"""
    text = (mem.get("text", "") or "").lower()
    full_text = mem.get("text", "") or ""
    tags = mem.get("tags", [])
    
    # 1. 格式错误
    if full_text.startswith("User: [IMPORTANT: Background process"):
        return "format_error"
    if "content" in full_text and "User: [IMPORTANT" in full_text:
        return "format_error"
    
    # 2. 非持仓类误报
    entities = mem.get("entities", "") or ""
    is_stock_related = bool(RE_STOCK_CODE.search(full_text)) or bool(RE_COMPANY.search(full_text))
    stock_keywords = ["持仓", "买入", "卖出", "止损", "止盈", "涨", "跌", "k线", "趋势", "突破"]
    has_stock_content = any(kw in full_text.lower() for kw in stock_keywords)
    
    if not is_stock_related and not has_stock_content:
        return "false_positive"
    
    # 3. 时序快照（持仓分析报告 — 良性内容）
    if is_stock_related and has_stock_content:
        if any(kw in text for kw in ["持仓", "分析", "技术面", "基本面", "持有", "加仓"]):
            return "benign_snapshot"
    
    # 4. 需要关注的
    return "needs_attention"


def main():
    memories = api_get_all()
    print(f"Total: {len(memories)}", file=sys.stderr)
    
    contradictions, entity_groups = detect_contradictions(memories)
    contra_ids = set(contradictions.keys())
    mem_by_id = {m["id"]: m for m in memories}
    quality = {}
    if os.path.exists(QUALITY_FILE):
        with open(QUALITY_FILE) as f:
            quality = json.load(f).get("memories", {})
    
    # 按实体分组输出
    id_to_entity = {}
    for m in memories:
        e = extract_entity(m)
        if e:
            id_to_entity[m["id"]] = e
    
    entity_contra = defaultdict(lambda: {"ids": [], "neg": [], "aff": [], "classified": []})
    for mid in contra_ids:
        e = id_to_entity.get(mid, "unknown")
        entity_contra[e]["ids"].append(mid)
        text = (mem_by_id[mid].get("text", "") or "").lower()
        if RE_NEGATION.search(text):
            entity_contra[e]["neg"].append(mid)
        if RE_AFFIRMATION.search(text):
            entity_contra[e]["aff"].append(mid)
    
    total_fp = 0
    total_benign = 0
    total_format = 0
    total_attention = 0
    confirm_ids = []  # 需要确认提高质量的
    delete_ids = []   # 需要删除的
    
    sorted_entities = sorted(entity_contra.items(), key=lambda x: len(x[1]["ids"]), reverse=True)
    
    print(f"contradictions_total={len(contra_ids)}")
    
    for entity, data in sorted_entities:
        print(f"\n{'='*40}")
        print(f"entity={entity} | count={len(data['ids'])}")
        for mid in sorted(data["ids"]):
            m = mem_by_id.get(mid, {})
            full_text = m.get("text", "") or ""
            q_entry = quality.get(mid, {})
            q = q_entry.get("quality", 0.5) if isinstance(q_entry, dict) else 0.5
            
            cat = classify_entry(m, contradictions)
            
            if cat == "format_error":
                total_format += 1
                delete_ids.append(mid)
                print(f"  DELETE id={mid} q={q:.3f} [{cat}]")
                print(f"    text={full_text[:80]}")
            elif cat == "false_positive":
                total_fp += 1
                confirm_ids.append(mid)
                print(f"  CONFIRM id={mid} q={q:.3f} [{cat}]")
            elif cat == "benign_snapshot":
                total_benign += 1
                confirm_ids.append(mid)
                print(f"  CONFIRM id={mid} q={q:.3f} [{cat}]")
            else:
                total_attention += 1
                print(f"  REVIEW id={mid} q={q:.3f} [{cat}]")
                print(f"    text={full_text[:100]}")
    
    print(f"\n\n{'='*40}")
    print(f"SUMMARY:")
    print(f"  format_error (删除): {total_format}")
    print(f"  false_positive (确认提升): {total_fp}")
    print(f"  benign_snapshot (确认提升): {total_benign}")
    print(f"  needs_attention: {total_attention}")
    print(f"  TOTAL confirm: {len(confirm_ids)}")
    print(f"  TOTAL delete: {len(delete_ids)}")
    
    if confirm_ids:
        print(f"\nCONFIRM_IDS: {','.join(confirm_ids)}")
    if delete_ids:
        print(f"\nDELETE_IDS: {','.join(delete_ids)}")


if __name__ == "__main__":
    import sys
    main()
