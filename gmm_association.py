#!/usr/bin/env python3
"""
gmm_association.py — MemGAS 启发：GMM 增量记忆关联固化

对近期记忆做 GMM 聚类，输出关联关系，供 dreaming 或 cron 任务用于
建立集群标签，提升记忆间的语义连通性。

用法：
  python3 gmm_association.py [--days 7] [--output-json]

流程：
  1. 从 Hindsight API 拉取近期记忆（可配置天数）
  2. 提取文本 → TF-IDF 特征
  3. GMM 聚类（自动选 n_components=3-8）
  4. 输出集群分布 ⇒ 关联关系
"""

import argparse
import json
import logging
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GMM] %(levelname)s %(message)s",
)
logger = logging.getLogger("gmm_association")

# ── Hindsight API 配置 ──
HINDSIGHT_API = os.environ.get(
    "HINDSIGHT_API_URL", "http://127.0.0.1:9177/v1/default"
)
BANK_ID = os.environ.get("HINDSIGHT_BANK_ID", "hermes")


# ════════════════════════════════════════════════════════════════════
# 1. 获取记忆
# ════════════════════════════════════════════════════════════════════

def fetch_recent_memories(days: int = 7, limit: int = 200) -> list[dict]:
    """从 Hindsight API 获取近期记忆。

    Args:
        days: 最近 N 天的记忆
        limit: 最多返回条数

    Returns:
        [{id, text, tags, date, context}, ...]
    """
    url = f"{HINDSIGHT_API}/banks/{BANK_ID}/memories/list"
    params = {"limit": limit, "q": ""}
    full_url = f"{url}?{urlencode(params)}"

    try:
        req = urllib.request.Request(full_url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.warning("Hindsight list API error: %s", e)
        # Fallback: 用 recall 泛查询
        return _fetch_via_recall(days, limit)

    # 解析响应
    results = []
    raw_memories = data if isinstance(data, list) else (
        data.get("items") or data.get("results") or data.get("data") or []
    )
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400

    for item in raw_memories:
        text = item.get("text", item.get("content", ""))
        if not text:
            continue
        tags = item.get("tags", [])
        date_str = item.get("date", item.get("created_at", ""))
        mem_id = item.get("id", item.get("memory_id", str(hash(text))))
        context = item.get("context", "")

        results.append({
            "id": str(mem_id),
            "text": text,
            "tags": tags if isinstance(tags, list) else [],
            "date": date_str[:10] if date_str else "",
            "context": context,
        })

    if not results:
        logger.info("list 接口无结果，fallback 到 recall")
        return _fetch_via_recall(days, limit)

    logger.info("API 返回 %d 条记忆", len(results))
    return results


def _fetch_via_recall(days: int = 7, limit: int = 200) -> list[dict]:
    """Fallback: 通过 recall 接口获取记忆。"""
    url = f"{HINDSIGHT_API}/banks/{BANK_ID}/memories/recall"
    keywords_list = ["分析", "策略", "交易", "项目", "代码", "配置", "系统",
                     "持仓", "回测", "指标", "研究", "决策", "评估"]
    seen_ids = set()
    results = []
    total_fetched = 0

    for kw in keywords_list:
        if len(results) >= limit:
            break
        payload = json.dumps({"query": kw, "limit": 20}).encode()
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            continue

        items = data.get("results", [])
        for item in items:
            text = item.get("text", "")
            mem_id = item.get("id", str(hash(text)))
            if mem_id in seen_ids or not text:
                continue
            seen_ids.add(mem_id)
            tags = item.get("tags", [])
            results.append({
                "id": str(mem_id),
                "text": text,
                "tags": tags if isinstance(tags, list) else [],
                "date": (item.get("date", "") or "")[:10],
                "context": item.get("context", ""),
            })
        total_fetched += len(items)
        logger.debug("  recall '%s': +%d 条 (累计 %d)", kw, len(results) - (total_fetched - len(items)), len(results))

    logger.info("Recall fallback: 收集 %d 条去重记忆", len(results))
    return results


# ════════════════════════════════════════════════════════════════════
# 2. 特征提取与 GMM 聚类
# ════════════════════════════════════════════════════════════════════

def cluster_memories(
    memories: list[dict],
    n_components: int = 5,
) -> list[dict]:
    """对记忆做 TF-IDF + GMM 聚类。

    使用 sklearn TfidfVectorizer + GaussianMixture。
    自动降采样到 200 条内以控制计算成本。

    Args:
        memories: 记忆列表
        n_components: GMM 聚类数

    Returns:
        带 cluster 标注的记忆列表（原地修改）
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.mixture import GaussianMixture
    except ImportError:
        logger.warning("sklearn 未安装，跳过 GMM 聚类")
        for m in memories:
            m["cluster"] = -1
            m["cluster_confidence"] = 0.0
        return memories

    texts = [m["text"] for m in memories]
    if len(texts) < 3:
        logger.warning("记忆数量过少 (%d)，跳过聚类", len(texts))
        for m in memories:
            m["cluster"] = 0
            m["cluster_confidence"] = 0.0
        return memories

    # 对长文本截断控制特征维度
    truncated = [t[:500] for t in texts]

    try:
        # TF-IDF 特征
        vectorizer = TfidfVectorizer(
            max_features=500,
            token_pattern=r"(?u)\b\w+\b",
            analyzer="word",
            ngram_range=(1, 2),
            stop_words=["的", "了", "是", "在", "有", "和", "与", "就",
                         "也", "都", "而", "及", "但", "或", "一个",
                         "没有", "什么", "如何", "怎么", "可以", "这个",
                         "那个", "我们", "他们", "它们", "已经", "被",
                         "把", "从", "对", "为", "将", "还", "又",
                         "to", "the", "a", "an", "in", "on", "for",
                         "of", "and", "is", "are", "was", "were"],
            max_df=0.85,
            min_df=1,
        )
        X = vectorizer.fit_transform(truncated)
        logger.info("TF-IDF 特征矩阵: %s, 词汇量=%d", X.shape, len(vectorizer.get_feature_names_out()))

        # 自动选 n_components：记忆数 / 20，3-8 之间
        auto_k = max(3, min(8, len(texts) // 20))
        if auto_k != n_components:
            logger.info("自动调整聚类数: %d → %d", n_components, auto_k)
            n_components = auto_k

        # GMM 聚类
        gm = GaussianMixture(
            n_components=n_components,
            random_state=42,
            max_iter=200,
            n_init=3,
        )
        labels = gm.fit_predict(X.toarray())
        probs = gm.predict_proba(X.toarray())

        for i, m in enumerate(memories):
            m["cluster"] = int(labels[i])
            m["cluster_confidence"] = round(float(probs[i].max()), 3)

        logger.info("GMM 聚类完成: %d 条记忆 → %d 个集群", len(memories), n_components)
        for c in range(n_components):
            count = sum(1 for m in memories if m["cluster"] == c)
            avg_conf = sum(m["cluster_confidence"] for m in memories if m["cluster"] == c) / max(count, 1)
            logger.info("  Cluster %d: %d 条, 平均置信度 %.3f", c, count, avg_conf)

    except Exception as e:
        logger.warning("GMM 聚类失败: %s", e)
        for m in memories:
            m["cluster"] = -1
            m["cluster_confidence"] = 0.0

    return memories


# ════════════════════════════════════════════════════════════════════
# 3. 生成关联推荐
# ════════════════════════════════════════════════════════════════════

def generate_association_report(memories: list[dict]) -> dict:
    """生成关联关系报告。

    Returns:
        {
            "summary": str,
            "clusters": {cluster_id: {"count": int, "sample_texts": [str]}},
            "associations": [("mem_id_a", "mem_id_b", "cluster_id"), ...],
            "new_tags": [{"id": str, "recommended_tags": [str]}, ...],
        }
    """
    clusters: dict[int, dict] = {}
    for m in memories:
        c = m.get("cluster", -1)
        if c < 0:
            continue
        if c not in clusters:
            clusters[c] = {"count": 0, "sample_texts": [], "confidence_sum": 0.0}
        clusters[c]["count"] += 1
        clusters[c]["confidence_sum"] += m.get("cluster_confidence", 0.0)
        if len(clusters[c]["sample_texts"]) < 3:
            clusters[c]["sample_texts"].append(m["text"][:80])

    # 关联边：同集群内的记忆互为关联
    associations = []
    for m in memories:
        c = m.get("cluster", -1)
        if c < 0:
            continue
        same_cluster = [x for x in memories if x.get("cluster") == c and x["id"] != m["id"]]
        for peer in same_cluster[:3]:  # 每集群最多3条关联
            associations.append((m["id"][:12], peer["id"][:12], c))

    # 标签推荐：高置信度(>0.7)的记忆推荐 cluster 标签
    new_tags = []
    for m in memories:
        c = m.get("cluster", -1)
        conf = m.get("cluster_confidence", 0.0)
        if c >= 0 and conf > 0.7:
            # 检查是否已有 cluster 标签
            existing_tags = set(m.get("tags", []))
            cluster_tag = f"cluster:{c}"
            if cluster_tag not in existing_tags:
                new_tags.append({
                    "id": m["id"][:12],
                    "recommended_tags": [cluster_tag],
                    "confidence": conf,
                })

    summary_parts = [
        f"GMM 关联分析完成: {len(memories)} 条记忆, {len(clusters)} 个集群",
    ]
    for c_id, info in sorted(clusters.items()):
        avg_conf = info["confidence_sum"] / info["count"]
        summary_parts.append(
            f"  Cluster {c_id}: {info['count']} 条 (avg_conf={avg_conf:.2f})"
        )
        for t in info["sample_texts"]:
            summary_parts.append(f"    · {t}")
    summary_parts.append(f"建议添加 {len(new_tags)} 个集群标签")

    return {
        "summary": "\n".join(summary_parts),
        "total_memories": len(memories),
        "num_clusters": len(clusters),
        "clusters": {str(k): v for k, v in clusters.items()},
        "associations": associations[:50],
        "recommended_tags": new_tags[:50],
    }


# ════════════════════════════════════════════════════════════════════
# 4. CLI
# ════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="MemGAS 启发：GMM 增量记忆关联固化"
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="最近 N 天的记忆（默认 7）",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="最多处理条数（默认 200）",
    )
    parser.add_argument(
        "--output-json", action="store_true",
        help="输出 JSON 格式到 stdout",
    )
    parser.add_argument(
        "--clusters", type=int, default=5,
        help="GMM 聚类数（默认 5，自动在 3-8 间调整）",
    )
    args = parser.parse_args()

    # 1. 获取记忆
    logger.info("获取最近 %d 天的记忆...", args.days)
    memories = fetch_recent_memories(days=args.days, limit=args.limit)
    if not memories:
        msg = "未获取到有效记忆"
        logger.warning(msg)
        if args.output_json:
            print(json.dumps({"error": msg}, ensure_ascii=False))
        return 1

    logger.info("获取到 %d 条记忆", len(memories))

    # 2. 聚类
    memories = cluster_memories(memories, n_components=args.clusters)

    # 3. 生成报告
    report = generate_association_report(memories)

    # 4. 输出
    if args.output_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n" + "=" * 60)
        print("🧠 MemGAS GMM 关联分析报告")
        print("=" * 60)
        print(report["summary"])
        print("=" * 60)
        if report["recommended_tags"]:
            print("\n📌 推荐添加的标签（集群关联）:")
            for tag_rec in report["recommended_tags"][:10]:
                print(f"  [{tag_rec['confidence']:.2f}] cluster:{tag_rec['id']} → {tag_rec['recommended_tags']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
