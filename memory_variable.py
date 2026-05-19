#!/usr/bin/env python3
"""
记忆变量系统 — 手动 key:value 校准，权重高于自动提取。

概念来自阿里云百炼：当自动提取的结果与用户明确声明冲突时，
手动定义的关键变量以最高权重覆盖自动结果。

用法:
  # 设置变量（权重默认 1.0）
  python3 memory_variable.py set language 中文 --weight 1.0
  
  # 获取变量值
  python3 memory_variable.py get language
  
  # 列出所有变量
  python3 memory_variable.py list
  
  # 删除变量
  python3 memory_variable.py delete language
  
  # 批量初始化常用变量
  python3 memory_variable.py init
  
  # 检查变量与记忆的冲突
  python3 memory_variable.py check
"""
import json
import os
import sys
import time
from urllib.request import urlopen

VARIABLE_FILE = os.path.expanduser("~/.hermes/hindsight/memory_variables.json")
QUALITY_FILE = os.path.expanduser("~/.hermes/hindsight/memory_quality.json")
API_BASE = "http://127.0.0.1:9177/v1/default/banks/hermes"


def load():
    if os.path.exists(VARIABLE_FILE):
        with open(VARIABLE_FILE) as f:
            return json.load(f)
    return {"version": 1, "variables": {}}


def save(data):
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(VARIABLE_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def cmd_set(key, value, weight=1.0):
    data = load()
    data["variables"][key] = {
        "value": value,
        "weight": weight,
        "source": "manual",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    save(data)
    print(f"✅ 变量 [{key}] = {value} (权重={weight})")


def cmd_get(key):
    data = load()
    v = data["variables"].get(key)
    if v:
        print(f"{key} = {v['value']} (权重={v['weight']})")
    else:
        print(f"变量 [{key}] 未定义")


def cmd_list():
    data = load()
    vars = data.get("variables", {})
    if not vars:
        print("无变量定义")
        return
    print(f"记忆变量 ({len(vars)} 个):")
    for key, v in sorted(vars.items()):
        print(f"  [{key:30s}] = {str(v['value']):20s}  w={v['weight']}")


def cmd_delete(key):
    data = load()
    if key in data["variables"]:
        del data["variables"][key]
        save(data)
        print(f"🗑️ 已删除 [{key}]")
    else:
        print(f"变量 [{key}] 不存在")


def cmd_init():
    """批量初始化常用变量。"""
    defaults = {
        "language": {"value": "中文", "weight": 1.0},
        "timezone": {"value": "Asia/Shanghai", "weight": 1.0},
        "communication_style": {"value": "直接简洁", "weight": 0.9},
        "response_language": {"value": "中文简体", "weight": 1.0},
        "analysis_focus": {"value": "股票技术分析", "weight": 0.8},
        "risk_preference": {"value": "中长期持有", "weight": 0.8},
        "name": {"value": "小虾米", "weight": 0.9},
        "role": {"value": "资深证券分析师+工程师", "weight": 0.9},
    }
    data = load()
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for key, cfg in defaults.items():
        if key not in data["variables"]:
            cfg["source"] = "manual"
            cfg["created_at"] = now
            data["variables"][key] = cfg
    save(data)
    print(f"✅ 已初始化 {len(defaults)} 个默认变量")


def cmd_check():
    """检查变量与记忆的冲突。"""
    data = load()
    vars = data.get("variables", {})

    # 从 hindsight 搜索可能冲突的记忆
    conflicts = []
    for key, v in vars.items():
        if v["weight"] < 0.7:
            continue
        # 对高权重变量，搜索记忆库中是否包含相反的事实
        query = key.replace("_", " ")
        try:
            url = f"{API_BASE}/memories/recall"
            payload = json.dumps({"query": query, "limit": 5}).encode()
            req = __import__("urllib.request").Request(
                url, data=payload,
                headers={"Content-Type": "application/json"}
            )
            resp = urlopen(req, timeout=10)
            results = json.loads(resp.read().decode()).get("results", [])
            for r in results:
                text = (r.get("text", "") or "").lower()
                val = str(v["value"]).lower()
                if val not in text and key.lower() in text:
                    conflicts.append({
                        "key": key,
                        "variable": v["value"],
                        "memory": (r.get("text", "") or "")[:120],
                    })
        except Exception:
            pass

    if conflicts:
        print(f"⚠️ 发现 {len(conflicts)} 个潜在冲突:")
        for c in conflicts:
            print(f"  [{c['key']}] 变量={c['variable']} ↔ 记忆:{c['memory']}")
    else:
        print("✅ 无检测到冲突")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "set" and len(sys.argv) >= 4:
        weight = 1.0
        if "--weight" in sys.argv:
            idx = sys.argv.index("--weight")
            if idx + 1 < len(sys.argv):
                weight = float(sys.argv[idx + 1])
        cmd_set(sys.argv[2], sys.argv[3], weight)
    elif cmd == "get" and len(sys.argv) >= 3:
        cmd_get(sys.argv[2])
    elif cmd == "list":
        cmd_list()
    elif cmd == "delete" and len(sys.argv) >= 3:
        cmd_delete(sys.argv[2])
    elif cmd == "init":
        cmd_init()
    elif cmd == "check":
        cmd_check()
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
