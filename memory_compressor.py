#!/usr/bin/env python3
"""
Semantic Memory Compressor
==========================
Pre-process memory content before calling hindsight_retain.
Converts relative time references to absolute dates and
enforces self-contained format (no pronouns, no vague references).

Usage:
    from memory_compressor import compress
    compressed = compress(content, context_hint="")
"""

import re
from datetime import datetime, timedelta, UTC


def _now() -> datetime:
    """Get current UTC time (overridable for testing)."""
    return datetime.now(UTC)


# Relative time patterns (Chinese + English)
_RELATIVE_TIME_PATTERNS = [
    # Chinese time references
    (re.compile(r'(?<![\\w])今天(?![\\w])'), lambda n: n.strftime('%Y-%m-%d')),
    (re.compile(r'(?<![\\w])昨天(?![\\w])'), lambda n: (n - timedelta(days=1)).strftime('%Y-%m-%d')),
    (re.compile(r'(?<![\\w])前天(?![\\w])'), lambda n: (n - timedelta(days=2)).strftime('%Y-%m-%d')),
    (re.compile(r'(?<![\\w])明天(?![\\w])'), lambda n: (n + timedelta(days=1)).strftime('%Y-%m-%d')),
    (re.compile(r'(?<![\\w])后天(?![\\w])'), lambda n: (n + timedelta(days=2)).strftime('%Y-%m-%d')),
    (re.compile(r'(?<![\\w])上周(?![\\w])'), lambda n: f"{n.isocalendar()[0]}-W{n.isocalendar()[1]-1:02d}"),
    (re.compile(r'(?<![\\w])这周(?![\\w])'), lambda n: f"{n.isocalendar()[0]}-W{n.isocalendar()[1]:02d}"),
    (re.compile(r'(?<![\\w])下周(?![\\w])'), lambda n: f"{n.isocalendar()[0]}-W{n.isocalendar()[1]+1:02d}"),
    (re.compile(r'(?<![\\w])上个月(?![\\w])'), lambda n: (n.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')),
    (re.compile(r'(?<![\\w])这个月(?![\\w])'), lambda n: n.strftime('%Y-%m')),
    (re.compile(r'(?<![\\w])下个月(?![\\w])'), lambda n: (n.replace(day=28) + timedelta(days=7)).strftime('%Y-%m')),
    # English time references
    (re.compile(r'(?i)\\byesterday\\b'), lambda n: (n - timedelta(days=1)).strftime('%Y-%m-%d')),
    (re.compile(r'(?i)\\btoday\\b'), lambda n: n.strftime('%Y-%m-%d')),
    (re.compile(r'(?i)\\btomorrow\\b'), lambda n: (n + timedelta(days=1)).strftime('%Y-%m-%d')),
    (re.compile(r'(?i)\\blast week\\b'), lambda n: f"{n.isocalendar()[0]}-W{n.isocalorean()[1]-1:02d}"),
    (re.compile(r'(?i)\\bthis week\\b'), lambda n: f"{n.isocalendar()[0]}-W{n.isocalendar()[1]:02d}"),
    (re.compile(r'(?i)\\bnext week\\b'), lambda n: f"{n.isocalendar()[0]}-W{n.isocalendar()[1]+1:02d}"),
]


def resolve_relative_times(text: str, now: datetime | None = None) -> str:
    """Replace relative time references with absolute dates."""
    if now is None:
        now = _now()
    result = text
    for pattern, replacer in _RELATIVE_TIME_PATTERNS:
        result = pattern.sub(lambda m: replacer(now), result)
    return result


def check_pronouns(text: str) -> list[str]:
    """Check for forbidden pronouns and vague references. Returns list of issues."""
    issues = []
    pronouns = {
        '他': 'his/her (use specific name)',
        '她': 'his/her (use specific name)',
        '它': 'it (use specific noun)',
        '他们': 'they (use specific names)',
        '她们': 'they (use specific names)',
        '它们': 'they (use specific nouns)',
        '这': 'this (use specific reference)',
        '那': 'that (use specific reference)',
        '那里': 'there (use specific location)',
        '这里': 'here (use specific location)',
        '那天': 'that day (use specific date)',
        '那次': 'that time (use specific reference)',
        '那个': 'that one (use specific reference)',
    }
    for pronoun, hint in pronouns.items():
        if pronoun in text:
            issues.append(f"  ⚠️ Contains '{pronoun}' — {hint}")
    return issues


def compress(content: str, context_hint: str = "", now: datetime | None = None) -> str:
    """
    Compress memory content: resolve relative times and validate self-containment.

    Args:
        content: Raw memory content to store
        context_hint: Optional context to help with pronoun resolution
        now: Override current time (for testing)

    Returns:
        Compressed, self-contained memory content
    """
    # Step 1: Resolve relative times
    result = resolve_relative_times(content, now)

    # Step 2: Check for pronouns (log only, don't modify automatically)
    issues = check_pronouns(result)
    if issues:
        issue_text = "\n".join(issues)
        import logging
        logging.getLogger(__name__).warning(
            f"Memory content may contain vague references:\n{issue_text}\n"
            f"  Content: {result[:200]}..."
        )

    return result


if __name__ == "__main__":
    # Quick test
    now = datetime(2026, 4, 28, 14, 0, 0, tzinfo=UTC)
    tests = [
        "昨天修复了retain bug",
        "今天开始Phase 2",
        "上周分析了5只股票",
        "他说这个项目下个月完成",
    ]
    for t in tests:
        compressed = compress(t, now=now)
        issues = check_pronouns(compressed)
        print(f"  IN:  {t}")
        print(f"  OUT: {compressed}")
        if issues:
            for i in issues:
                print(f"  {i}")
        print()
