#!/usr/bin/env python3
"""
自动实体抽取器 — 扫描 Hindsight API 中缺少 entity| 标签的记忆，
用轻量规则抽取中文实体并自动打标。

用法：
  python3 entity_extractor.py              # 完整运行
  python3 entity_extractor.py --dry-run    # 预览
  python3 entity_extractor.py --max 100    # 只处理前 N 条
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"

# spaCy 全局懒加载
_nlp = None

# 实体抽取规则
STOCK_CODE_RE = re.compile(r'\b[0-6]\d{5}\b')  # A股代码

# 已知专业缩写（大小写敏感）
KNOWN_ACRONYMS = {
    "ADX", "BOLL", "MA", "EMA", "MACD", "RSI", "KDJ", "CCI", "WR", "DMI",
    "GSEM", "PPR", "AMAP", "NER", "LTM", "STM", "WM", "ELIM",
    "MCP", "SQL", "API", "JSON", "YAML", "CSV", "PDF", "HTML",
    "MongoDB", "OpenAI", "LLM", "AI",
    "Hermes", "Hindsight", "OpenCode",
    "stockWeeklyAnalyzer", "FutuOpenD", "baostock",
}

# 项目/系统名
KNOWN_PROJECTS = {
    "Hermes Agent", "Hermes", "Hindsight", "OpenCode",
    "stockWeeklyAnalyzer", "FutuOpenD",
}

# 中文公司后缀
CN_COMPANY_SUFFIX = re.compile(r'([\u4e00-\u9fff]{2,10}(?:公司|股份|集团|科技|实业|能源|医药|电子|通信|证券|银行|保险|基金))')

# 中文概念词（长度2-6的常见专业词）
CN_CONCEPT_RE = re.compile(r'([\u4e00-\u9fff]{2,6}(?:板块|概念|行业|产业|指数|基金|ETF|策略|信号|指标|趋势|通道|模型|算法|系统|平台|框架|协议|接口|服务|工具|脚本|脚本|报告|分析|评估|校准|审计|测试|部署|运维|监控|预警|追踪|扫描|筛选|推荐|预测|验证|确认|纠正|更新|同步|备份|恢复|迁移|升级|配置|注册|登录|认证|授权))')


def api_get(path: str, params: str = "") -> dict:
    url = f"{API_BASE}{path}?{params}" if params else f"{API_BASE}{path}"
    try:
        resp = urlopen(url, timeout=15)
        return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


def api_patch_tags(memory_id: str, tags: list[str]) -> bool:
    """更新记忆标签，追加新标签。"""
    url = f"{API_BASE}/memories/{memory_id}/tags"
    try:
        payload = json.dumps({"tags": tags, "mode": "append"}).encode()
        req = Request(url, data=payload, headers={"Content-Type": "application/json"})
        req.method = "PATCH"
        resp = urlopen(req, timeout=15)
        return resp.status == 200
    except Exception as e:
        return False


def _get_nlp():
    """延迟加载 spaCy 中文模型。"""
    global _nlp
    if _nlp is None:
        try:
            import spacy
            _nlp = spacy.load("zh_core_web_sm")
        except Exception:
            _nlp = False  # 标记不可用
    return _nlp if _nlp else None


def extract_entities(text: str) -> list[str]:
    """从文本中抽取实体，返回 entity|auto:xxx 列表。
    
    策略：
    - spaCy NER（若可用）→ ORG/PRODUCT/EVENT/GPE
    - 规则补充 → 股票代码、专业缩写、中文概念词
    """
    entities = set()

    if not text:
        return []

    # ====== A. spaCy NER（如果有模型） ======
    nlp = _get_nlp()
    if nlp:
        try:
            doc = nlp(text[:1000])  # 只处理前1000字符
            for ent in doc.ents:
                label = ent.label_
                etxt = ent.text.strip()
                if len(etxt) < 2 or re.match(r'^\d+$', etxt):
                    continue  # 纯数字/单字跳过
                if label == "ORG":
                    entities.add(f"entity|auto:org_{etxt}")
                elif label == "PRODUCT":
                    entities.add(f"entity|auto:product_{etxt}")
                elif label == "EVENT":
                    entities.add(f"entity|auto:event_{etxt}")
                elif label == "GPE":
                    entities.add(f"entity|auto:location_{etxt}")
                elif label == "PERSON" and len(etxt) >= 3:
                    entities.add(f"entity|auto:person_{etxt}")
        except Exception:
            pass  # NER 失败不影响规则抽取

    # ====== B. 规则抽取（原有的增强版） ======
    # 1. 股票代码
    for code in STOCK_CODE_RE.findall(text):
        entities.add(f"entity|auto:stock_{code}")

    # 2. 专业缩写
    for word in KNOWN_ACRONYMS:
        # 确保是完整词匹配（不是子串）
        if re.search(r'\b' + re.escape(word) + r'\b', text):
            entities.add(f"entity|auto:acronym_{word.lower()}")

    # 3. 项目名（含空格的要特殊处理）
    for proj in KNOWN_PROJECTS:
        if proj in text:
            key = proj.lower().replace(" ", "_")
            entities.add(f"entity|auto:project_{key}")

    # 4. 中文公司/概念
    for m in CN_COMPANY_SUFFIX.finditer(text):
        name = m.group(1)
        entities.add(f"entity|auto:company_{name}")

    for m in CN_CONCEPT_RE.finditer(text):
        concept = m.group(1)
        entities.add(f"entity|auto:concept_{concept}")

    return sorted(entities)


def has_entity_tag(tags: list) -> bool:
    """检查是否已有 entity| 标签（人工的）。"""
    for t in tags:
        ts = str(t).strip()
        if ts.startswith("entity|") and not ts.startswith("entity|auto:"):
            return True
    return False


def has_auto_entity_tag(tags: list) -> bool:
    """检查是否已有 entity|auto: 标签。"""
    for t in tags:
        ts = str(t).strip()
        if ts.startswith("entity|auto:"):
            return True
    return False


def main():
    dry_run = "--dry-run" in sys.argv
    max_items = 500
    if "--max" in sys.argv:
        idx = sys.argv.index("--max")
        if idx + 1 < len(sys.argv):
            max_items = int(sys.argv[idx + 1])

    print(f"{'🔄 实体抽取器 [DRY RUN]' if dry_run else '🔄 实体抽取器'}")
    print(f"时间: {datetime.now(timezone.utc).isoformat()}\n")

    # 获取记忆列表
    resp = api_get("/memories/list", f"limit={max_items}&offset=0")
    items = resp.get("items", resp.get("results", []))

    if not items or "error" in resp:
        print(f"❌ 获取记忆列表失败: {resp.get('error', '空结果')}")
        return 1

    total = len(items)
    skipped_no_text = 0
    skipped_has_entity = 0
    tagged = 0
    total_entities = 0

    print(f"📋 获取到 {total} 条记忆\n")

    for item in items:
        text = item.get("text", "") or ""
        tags = item.get("tags", [])
        mid = item.get("id", "")

        if not text or not mid:
            skipped_no_text += 1
            continue

        # 已有 entity| 标签的跳过（人工标签优先级高）
        if has_entity_tag(tags):
            skipped_has_entity += 1
            continue

        # 已有 entity|auto: 标签的跳过（不重复处理）
        if has_auto_entity_tag(tags):
            continue

        # 抽取实体
        entities = extract_entities(text)
        if not entities:
            continue

        total_entities += len(entities)

        if dry_run:
            print(f"  [{mid[:16]}] {', '.join(entities)}")
            print(f"    → {text[:80]}...")
        else:
            ok = api_patch_tags(mid, entities)
            if ok:
                tagged += 1
            time.sleep(0.05)  # 限速

    print(f"\n📊 统计:")
    print(f"   总扫描: {total} 条")
    print(f"   跳过(无文本/ID): {skipped_no_text}")
    print(f"   跳过(已有实体标签): {skipped_has_entity}")
    print(f"   新增/更新标签: {tagged if not dry_run else 'DRY-RUN'}")
    print(f"   实体抽取总数: {total_entities}")

    if dry_run:
        print(f"\n⚠️  DRY-RUN 模式，未实际修改。去掉 --dry-run 执行。")
    else:
        print(f"\n✅ 完成")

    return 0


if __name__ == "__main__":
    sys.exit(main())
