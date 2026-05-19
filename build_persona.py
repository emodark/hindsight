#!/usr/bin/env python3
"""
结构化画像构建器 — 从 USER.md 提取用户画像，用于指导小虾米行为。

数据流:
  USER.md (用户对自己的描述)
    → build_persona.py (规则提取，逐段分析)
    → structured_persona.json (结构化画像：用户的风格/偏好/准则)
    → 小虾米读取后影响决策和行为

输出: ~/.hermes/hindsight/structured_persona.json
用法:
  python3 build_persona.py          # 构建画像
  python3 build_persona.py show     # 显示当前画像
"""
import json
import os
import re
import sys
import time

USER_FILE = os.path.expanduser("~/.hermes/memories/USER.md")
PERSONA_FILE = os.path.expanduser("~/.hermes/hindsight/structured_persona.json")

KNOWN_INDICATORS = {
    "BOLL", "ADX", "BOLL+ADX", "MA", "EMA", "MACD", "RSI", "KDJ", "CCI",
    "成交量", "一目均衡云",
}

# ============================================================
# USER.md 解析器 — 按 § 分段逐段分析
# ============================================================

def load_user_md() -> str:
    if os.path.exists(USER_FILE):
        with open(USER_FILE) as f:
            return f.read().strip()
    return ""


def _has(section: str, *keywords: str) -> bool:
    """检查段落是否包含所有关键词。"""
    return all(kw in section for kw in keywords)


def extract_from_user_md(text: str) -> dict:
    """从 USER.md 提取结构化信息。"""
    if not text:
        return {}

    sections = [s.strip() for s in text.split("§") if s.strip()]

    info = {
        "role": "",
        "investment_style": [],
        "core_indicators": [],
        "risk_rules": [],
        "preferences": {},
    }

    for sec in sections:
        # --- 1. 身份与框架段 ---
        if "分析师" in sec or "工程师" in sec:
            role_m = re.search(
                r"((?:资深|高级|首席|初级)?[^，。]+(?:分析师|工程师|经理|顾问|专家))",
                sec,
            )
            if role_m:
                info["role"] = role_m.group(1).strip()
            # 核心指标 — 白名单过滤英文大写指标
            raw_indicators = re.findall(r"[A-Z]{2,}(?:\+[A-Z]{2,})*", sec)
            for i in raw_indicators:
                if i in KNOWN_INDICATORS and i not in info["core_indicators"]:
                    info["core_indicators"].append(i)
            # 中文指标名
            for cn in ["成交量", "一目均衡云"]:
                if cn in sec and cn in KNOWN_INDICATORS and cn not in info["core_indicators"]:
                    info["core_indicators"].append(cn)
            # 投资风格
            for kw in ["中长期", "短期", "趋势", "价值", "成长"]:
                if kw in sec and kw not in info["investment_style"]:
                    info["investment_style"].append(kw)
            # 持仓纪律
            if "宁缺勿滥" in sec:
                info["preferences"]["discipline"] = "宁缺勿滥，少而精"

        # --- 2. 风险管控段 ---
        elif "风险" in sec or "预期管理" in sec:
            if _has(sec, "预期", "清仓"):
                info["risk_rules"].append("预期管理优先（不达预期清仓）")
            if "金字塔" in sec:
                info["risk_rules"].append("金字塔加仓")
            if "动态止损" in sec:
                info["risk_rules"].append("动态止损")
            if "试运行" in sec:
                info["preferences"]["approach"] = "试运行阶段，愿尝试新策略"

        # --- 3. 协作模式段 ---
        elif "Agent" in sec or "数据驱动" in sec:
            if "数据质量零容忍" in sec:
                info["preferences"]["data_quality"] = "零容忍：自动交叉验证+修复"

        # --- 4. 核心原则段 ---
        elif "核心原则" in sec or "先摸清" in sec or "充分复用" in sec:
            info["preferences"]["workflow"] = "先查工具再动手，充分复用不重造轮子"

        # --- 5. 分批与验证段 ---
        elif ("分批" in sec) or ("验证" in sec and "手动" in sec):
            if "手动立即验证" in sec:
                info["preferences"]["verification"] = "改完手动立即验证，不等cron"
            if "全量" in sec and "分组统计" in sec:
                info["preferences"]["validation"] = "全量级分组统计对比+参数敏感度扫描"

        # --- 6. 用户要求段 ---
        elif "用户要求" in sec:
            if "不开新话题" in sec:
                info["preferences"]["interaction"] = "不开新话题，已完成自然结束"
            if "量化评估" in sec:
                info["preferences"]["evaluation"] = "成本/收益/风险量化评估，不主观判断"

        # --- 7. 用户纠正段 ---
        elif "用户纠正" in sec or "搜索确认" in sec:
            if "搜索确认" in sec:
                info["preferences"]["correction"] = "说'不能动'前先搜索确认文件位置"

        # --- 8. 工作风格段 ---
        elif "实验驱动" in sec or "工作风格" in sec:
            if "迭代优化" in sec:
                info["preferences"]["approach"] = "实验驱动，迭代优化而非追求完美"
            if "投资书轮读" in sec:
                info["preferences"]["learning"] = "每晚21:00投资书轮读，元知识框架分析"

        # --- 9. 猫段（个人偏好） ---
        elif "福宝" in sec:
            info["preferences"]["pet"] = "养德文猫名福宝"

    # 去重 risk_rules
    info["risk_rules"] = list(dict.fromkeys(info["risk_rules"]))

    return info


# ============================================================
# 画像构建
# ============================================================

def build_persona() -> dict:
    """构建完整结构化画像。"""
    user_text = load_user_md()
    user_info = extract_from_user_md(user_text)

    persona = {
        "identity": {
            "name": "小虾米",      # 小虾米是用户的投影
            "language": "中文",
            "timezone": "Asia/Shanghai",
            "role": user_info.get("role", ""),
        },
        "behavioral": {
            "communication_style": "直接简洁",
            "response_language": "中文简体",
            "analysis_focus": "股票技术分析",
        },
        "professional": {
            "role": user_info.get("role", ""),
            "investment_style": user_info.get("investment_style", []),
            "core_indicators": user_info.get("core_indicators", []),
            "risk_rules": user_info.get("risk_rules", []),
        },
        "preferences": user_info.get("preferences", {}),
        "meta": {
            "version": int(time.time()),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": "USER.md",
            "sections_parsed": len([s for s in user_text.split("§") if s.strip()])
                if user_text else 0,
            "confidence": 0.8,
        },
    }

    return persona


# ============================================================
# CLI 命令
# ============================================================

def cmd_build():
    persona = build_persona()
    with open(PERSONA_FILE, "w") as f:
        json.dump(persona, f, indent=2, ensure_ascii=False)
    pref_count = len(persona.get("preferences", {}))
    print(f"✅ 画像已构建 → {PERSONA_FILE}")
    print(f"   角色: {persona['professional'].get('role', '?')}")
    print(f"   指标: {', '.join(persona['professional'].get('core_indicators', []))}")
    print(f"   偏好: {pref_count} 项")
    print(f"   置信度: {persona['meta']['confidence']}")
    print(f"   来源: {persona['meta']['source']} | {persona['meta']['sections_parsed']} 段")


def cmd_show():
    if not os.path.exists(PERSONA_FILE):
        print("画像不存在，先运行 build")
        return
    with open(PERSONA_FILE) as f:
        persona = json.load(f)

    for section, data in persona.items():
        if section == "meta":
            continue
        print(f"\n## {section.upper()}")
        if isinstance(data, dict):
            for key, val in data.items():
                if not val:
                    continue
                if isinstance(val, list):
                    print(f"  {key}: {', '.join(str(v) for v in val)}")
                elif isinstance(val, dict):
                    print(f"  {key}:")
                    for k, v in val.items():
                        print(f"    {k}: {v}")
                else:
                    print(f"  {key}: {val}")

    m = persona.get("meta", {})
    print(f"\n  置信度: {m.get('confidence', '?')} "
          f"| 来源: {m.get('source', '?')} "
          f"| 解析段落: {m.get('sections_parsed', 0)}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "show":
        cmd_show()
    else:
        cmd_build()


if __name__ == "__main__":
    main()
