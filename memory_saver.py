#!/usr/bin/env python3
"""
Memory Saver — 强制两步法
Step 1: 全文存 hindsight
Step 2: 指针存 memory

防止直接往 memory 里写全文。
用法:
    python3 memory_saver.py store <content> <memory_key>
    例: python3 memory_saver.py store "用户偏好中文" user-lang-preference
"""

import json
import os
import subprocess
import sys
from datetime import datetime

HINDSIGHT_URL = "http://127.0.0.1:9177/v1/default/banks/hermes/memories"


def store(content: str, memory_key: str, tags: list[str] | None = None,
          scene: str = "dev") -> dict:
    """强制两步法存储。"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    full_content = f"[{now}] {content}"

    # Step 1: hindsight_retain 存全文
    payload = {
        "items": [{
            "content": full_content,
            "tags": tags or [],
            "context": f"auto-store:{memory_key}",
        }]
    }
    result = subprocess.run(
        ["curl", "-s", "-X", "POST", HINDSIGHT_URL,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload, ensure_ascii=False)],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return {"success": False, "step": "hindsight", "error": result.stderr}

    try:
        hindsight_result = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"success": False, "step": "hindsight", "error": result.stdout[:200]}

    if not hindsight_result.get("success"):
        return {"success": False, "step": "hindsight",
                "error": str(hindsight_result)}

    # Step 2: 只输出 memory 指针格式，由调用者用 memory 工具存
    pointer = f"→ h:{memory_key}"
    print(json.dumps({
        "success": True,
        "hindsight_status": "stored",
        "pointer": pointer,
        "memory_command": f'memory(action="add", target="memory", '
                          f'content="[LTM] {memory_key} {pointer}")',
        "full_content": full_content,
    }, ensure_ascii=False, indent=2))
    return {"success": True}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: memory_saver.py store <全文内容> <memory_key> [--tags t1 t2]")
        sys.exit(1)

    command = sys.argv[1]
    if command == "store":
        content = sys.argv[2]
        memory_key = sys.argv[3]
        tags = []
        if "--tags" in sys.argv:
            idx = sys.argv.index("--tags")
            tags = sys.argv[idx+1:]
        store(content, memory_key, tags)
    else:
        print(f"未知命令: {command}")
