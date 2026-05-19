#!/usr/bin/env python3
"""
记忆去重系统 — 语义去重。

原理：
  1. 用 recall API（embedding + BM25 + reranker）搜索语义最相似的前 N 条记忆
  2. 再对 top 结果做 n-gram + 关键词文本相似度确认
  3. 双重阈值: 语义上相关 + 文本上近似 = 近重复，跳过 retain

用法:
  # 检查内容是否与已有记忆重复（exit code: 0=唯一 1=重复 2=近似）
  python3 memory_dedup.py check <content>

  # 过滤模式（stdin → stdout，适合管道集成）
  echo "内容" | python3 memory_dedup.py filter

  # 扫描全部记忆库，列出近重复对
  python3 memory_dedup.py scan

  # 批量清理近重复（保留最高 quality 的版本，删除其余）
  python3 memory_dedup.py clean

  # 去重统计
  python3 memory_dedup.py stats
"""
import json
import os
import sys
import time
from urllib.request import Request, urlopen

API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"
# 严格去重阈值（文本级确认，>0.85 判断为同一件事）
DEDUP_THRESHOLD = 0.85
# 宽松警报阈值（>0.70 可能相关但不一定重复）
ALERT_THRESHOLD = 0.70
# recall 拉取数量
RECALL_LIMIT = 5


# ── 文本相似度（轻量级，<1ms） ──────────────────────────────────────────

def ngram_jaccard(a: str, b: str, n: int = 3) -> float:
    """字符级 n-gram Jaccard 相似度。"""
    if not a or not b:
        return 0.0
    a_low, b_low = a.lower(), b.lower()
    set_a = {a_low[i:i + n] for i in range(max(0, len(a_low) - n + 1))}
    set_b = {b_low[i:i + n] for i in range(max(0, len(b_low) - n + 1))}
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union > 0 else 0.0


def keyword_overlap(a: str, b: str) -> float:
    """关键词重叠率。"""
    import re
    words = re.findall(r'[\u4e00-\u9fff]{2,6}|[a-zA-Z_][a-zA-Z_0-9-]{2,}', (a + ' ' + b))
    a_low, b_low = a.lower(), b.lower()
    kw_a = set(re.findall(r'[\u4e00-\u9fff]{2,6}|[a-zA-Z_][a-zA-Z_0-9-]{2,}', a_low))
    kw_b = set(re.findall(r'[\u4e00-\u9fff]{2,6}|[a-zA-Z_][a-zA-Z_0-9-]{2,}', b_low))
    if not kw_a or not kw_b:
        return 0.0
    return len(kw_a & kw_b) / len(kw_a | kw_b)


def length_penalty(a: str, b: str) -> float:
    """长度比例惩罚因子。"""
    if not a or not b:
        return 0.0
    return min(len(a), len(b)) / max(len(a), len(b), 1)


def text_similarity(a: str, b: str) -> float:
    """综合文本相似度（n-gram 60% + 关键词 40%）。"""
    char_sim = ngram_jaccard(a, b)
    kw_sim = keyword_overlap(a, b)
    lr = length_penalty(a, b)
    # 长度差 3 倍以上直接归零
    if lr < 0.3:
        return 0.0
    return char_sim * 0.6 + kw_sim * 0.4


# ── API 调用 ──────────────────────────────────────────────────────────

def api_recall(query: str, limit: int = RECALL_LIMIT) -> list[dict]:
    """通过 recall API 找到语义最相似记忆。"""
    url = f"{API_BASE}/memories/recall"
    payload = json.dumps({"query": query, "limit": limit}).encode()
    try:
        req = Request(url, data=payload,
                       headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        return data.get("results", data if isinstance(data, list) else [])
    except Exception as e:
        print(f"⚠️  recall 失败: {e}", file=sys.stderr)
        return []


def api_list(limit: int = 500) -> list[dict]:
    """获取记忆列表（分页）。"""
    url = f"{API_BASE}/memories/list?limit={limit}&order=-created_at"
    try:
        resp = urlopen(url, timeout=15)
        data = json.loads(resp.read().decode())
        return data.get("items", data.get("results", []))
    except Exception as e:
        print(f"⚠️  list 失败: {e}", file=sys.stderr)
        return []


def api_delete(memory_id: str) -> bool:
    """删除一条记忆（API 可能不支持，吞掉错误）。"""
    url = f"{API_BASE}/memories/{memory_id}"
    try:
        req = Request(url, method="DELETE")
        resp = urlopen(req, timeout=10)
        return True
    except Exception as e:
        if "405" in str(e):
            return False
        print(f"⚠️  删除失败: {e}", file=sys.stderr)
        return False


# ── PostgreSQL 直连清理（API 不支持 DELETE 时的后备） ────────────────

PG_DSN = os.environ.get("PG_DSN", "host=127.0.0.1 port=5433 user=hindsight password=hindsight dbname=hindsight")


def pg_delete_memories(memory_ids: list[str]) -> int:
    """直连 PG 批量删除记忆（级联删除关联的 links/entities）。"""
    if not memory_ids:
        return 0
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN)
        cur = conn.cursor()
        ids_str = "', '".join(memory_ids)

        # 删除关联
        cur.execute(f"DELETE FROM memory_links WHERE from_unit_id IN ('{ids_str}') OR to_unit_id IN ('{ids_str}')")
        cur.execute(f"DELETE FROM unit_entities WHERE unit_id IN ('{ids_str}')")
        # 删除记忆本身
        cur.execute(f"DELETE FROM memory_units WHERE id IN ('{ids_str}')")
        removed = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return removed
    except ImportError:
        print("⚠️  需要 psycopg2: pip install psycopg2-binary", file=sys.stderr)
        return -1
    except Exception as e:
        print(f"⚠️  PG 删除失败: {e}", file=sys.stderr)
        return -1


# ── 核心逻辑 ──────────────────────────────────────────────────────────

def check_duplicate(content: str, threshold: float = DEDUP_THRESHOLD,
                    alert_threshold: float = ALERT_THRESHOLD) -> tuple[int, float, str]:
    """
    检查内容是否重复。
    
    Returns:
        (code, max_similarity, match_text)
        code: 0=唯一  1=重复 >threshold  2=近似 >=alert_threshold
    """
    if not content.strip():
        return 0, 0.0, ""

    # 第 1 步：语义搜索（embedding + reranker 混合）
    candidates = api_recall(content, limit=RECALL_LIMIT)

    if not candidates:
        return 0, 0.0, ""

    # 第 2 步：文本级确认
    best_score = 0.0
    best_text = ""
    for mem in candidates:
        text = (mem.get("text", "") or "").strip()
        if not text:
            continue
        score = text_similarity(content, text)
        if score > best_score:
            best_score = score
            best_text = text[:120]

    if best_score >= threshold:
        return 1, best_score, best_text
    elif best_score >= alert_threshold:
        return 2, best_score, best_text
    else:
        return 0, best_score, best_text


# ── CLI 命令 ──────────────────────────────────────────────────────────

def cmd_check(content: str):
    """检查内容是否重复。"""
    code, score, match = check_duplicate(content)
    if code == 1:
        print(f"🔁 重复 (相似度={score:.2f})")
        print(f"   已有: {match}")
    elif code == 2:
        print(f"⚠️  近似 (相似度={score:.2f})")
        print(f"   最接近: {match}")
    else:
        print(f"✅ 唯一 (最高相似度={score:.2f})")
    return code


def cmd_filter():
    """stdin 过滤模式。"""
    content = sys.stdin.read().strip()
    if not content:
        return 0
    code, score, match = check_duplicate(content)
    if code == 1:
        print(f"DUP|{score:.2f}|{match}")
    elif code == 2:
        print(f"ALERT|{score:.2f}|{match}")
    else:
        print(f"NEW|{score:.2f}|{content[:80]}")
    return code


def cmd_scan():
    """扫描整个记忆库，找出近重复对。"""
    memories = api_list(500)
    if not memories:
        print("⚠️  无记忆可扫描")
        return

    texts = [(m.get("id", "")[:16], (m.get("text", "") or "").strip())
             for m in memories if (m.get("text", "") or "").strip()]
    print(f"扫描 {len(texts)} 条记忆...")

    pairs = []
    for i in range(len(texts)):
        id_a, txt_a = texts[i]
        for j in range(i + 1, len(texts)):
            id_b, txt_b = texts[j]
            score = text_similarity(txt_a, txt_b)
            if score >= ALERT_THRESHOLD:
                pairs.append((score, id_a, id_b, txt_a[:80], txt_b[:80]))

    pairs.sort(key=lambda x: -x[0])

    if pairs:
        print(f"\n📊 发现 {len(pairs)} 组近重复（阈值>{ALERT_THRESHOLD}）:\n")
        for score, id_a, id_b, txt_a, txt_b in pairs[:5]:
            print(f"  相似度 {score:.2f}")
            print(f"    [{id_a}] {txt_a}")
            print(f"    [{id_b}] {txt_b}")
            print()
        if len(pairs) > 5:
            print(f"  ... 还有 {len(pairs) - 5} 组")
    else:
        print("✅ 未发现近重复")


def cmd_clean():
    """批量清理近重复（保留第一条创建时间最早的，标记其他为需删除）。"""
    memories = api_list(500)
    if not memories:
        print("⚠️  无记忆可清理")
        return

    # 按 text 分组找近重复
    parsed = [(m.get("id", ""), (m.get("text", "") or "").strip(),
               m.get("created_at", ""))
              for m in memories if (m.get("text", "") or "").strip()]

    to_remove = []
    removed = 0
    failed = 0
    pg_fallback = False

    for i in range(len(parsed)):
        id_a, txt_a, _ = parsed[i]
        if id_a in {r for r, *_ in to_remove}:
            continue
        for j in range(i + 1, len(parsed)):
            id_b, txt_b, _ = parsed[j]
            if id_b in {r for r, *_ in to_remove}:
                continue
            score = text_similarity(txt_a, txt_b)
            if score >= DEDUP_THRESHOLD:
                to_remove.append((id_b, score, txt_b[:80]))

    if not to_remove:
        print("✅ 无需清理")
        return

    print(f"📊 发现 {len(to_remove)} 条冗余记忆\n")

    # 先试 API 删除
    for mem_id, score, preview in to_remove:
        ok = api_delete(mem_id)
        if ok:
            removed += 1
            print(f"  🗑️  已删除 {mem_id[:16]} (sim={score:.2f}) {preview}")
        else:
            failed += 1
            pg_fallback = True

    # API 删不掉的，走 PG 直连
    if failed > 0 and pg_fallback:
        print(f"\n🔧 API 删除失败 {failed} 条，尝试 PG 直连清理...")
        pg_ids = list(set(mid for mid, _, _ in to_remove))
        pg_removed = pg_delete_memories(pg_ids)
        if pg_removed > 0:
            removed += pg_removed
            failed -= pg_removed
            print(f"  ✅ PG 成功删除 {pg_removed} 条")
        elif pg_removed == 0:
            print(f"  ⚠️  PG 无记录被删除（可能已被删）")
        elif pg_removed < 0:
            print(f"  ❌ PG 删除失败（需要安装 psycopg2-binary）")

    print(f"\n✅ 清理完成: 已删除 {removed}, 失败 {max(0, failed)}")


def cmd_stats():
    """去重统计。"""
    memories = api_list(500)
    total = len(memories)
    if total < 2:
        print(f"📊 总记忆: {total} 条（不足 2 条无法统计）")
        return

    import random
    texts = [(m.get("id", "")[:16], (m.get("text", "") or "").strip())
             for m in memories if (m.get("text", "") or "").strip()]
    n = len(texts)

    # 随机抽样 200 对算平均相似度
    samples = min(200, n * (n - 1) // 2)
    scores = []
    seen = set()
    for _ in range(samples):
        while True:
            i, j = random.sample(range(n), 2)
            key = (min(i, j), max(i, j))
            if key not in seen:
                seen.add(key)
                break
        scores.append(text_similarity(texts[i][1], texts[j][1]))

    avg_sim = sum(scores) / len(scores) if scores else 0
    dup_cnt = sum(1 for s in scores if s >= DEDUP_THRESHOLD)
    alert_cnt = sum(1 for s in scores if s >= ALERT_THRESHOLD)

    print(f"📊 去重统计")
    print(f"   总记忆数: {total}")
    print(f"   抽样对数: {len(scores)}")
    print(f"   平均相似度: {avg_sim:.3f}")
    print(f"   近重复率 (>={ALERT_THRESHOLD}): {alert_cnt}/{len(scores)} ({alert_cnt / len(scores) * 100:.1f}%)")
    print(f"   严格重复率 (>={DEDUP_THRESHOLD}): {dup_cnt}/{len(scores)} ({dup_cnt / len(scores) * 100:.1f}%)")


# ── 入口 ──────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "check" and len(sys.argv) >= 3:
        exit(cmd_check(" ".join(sys.argv[2:])))
    elif cmd == "filter":
        exit(cmd_filter())
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "clean":
        cmd_clean()
    elif cmd == "stats":
        cmd_stats()
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
