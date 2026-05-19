#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
from typing import Any, Dict, List, Optional, Tuple


ALIASES: List[Tuple[str, List[str]]] = [
    ("input_price", ["输入价格", "输入单价", "input price", "prompt", "input", "输入"]),
    ("output_price", ["输出价格", "补全价格", "completion", "output", "输出"]),
    ("cache_read_price", ["缓存读取价格", "cache read", "缓存读取", "缓存价", "缓存价格"]),
    ("cache_write_price", ["缓存创建价格", "cache write", "缓存创建"]),
]


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[ \t]+", " ", text)


def extract_model_name(text: str) -> Optional[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        match = re.search(r"\b([A-Za-z][A-Za-z0-9.\-]*gpt[A-Za-z0-9.\-]*|gpt[- ]?\d+(?:\.\d+)?)\b", line, re.I)
        if match:
            return match.group(1).replace(" ", "")
    for line in lines[:2]:
        if len(line) <= 40 and not re.search(r"[¥$]|tokens|/m", line, re.I):
            return line
    return None


def find_price_for_alias(text: str, aliases: List[str]) -> Optional[str]:
    joined = "|".join(re.escape(alias) for alias in aliases)
    pattern = re.compile(
        rf"(?:{joined})\s*[:：]?\s*([¥￥$]?\s*\d+(?:\.\d+)?(?:\s*/\s*(?:1M\s*Tokens|M))?)",
        re.I,
    )
    match = pattern.search(text)
    if match:
        return match.group(1).replace(" ", "")

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.search(joined, line, re.I):
            same_line = re.search(r"([¥￥$]?\s*\d+(?:\.\d+)?(?:\s*/\s*(?:1M\s*Tokens|M))?)", line, re.I)
            if same_line:
                return same_line.group(1).replace(" ", "")
            if index + 1 < len(lines):
                cross_line = re.search(r"([¥￥$]?\s*\d+(?:\.\d+)?)", lines[index + 1], re.I)
                if cross_line:
                    suffix = "/1MTokens" if re.search(r"/\s*1M\s*Tokens|/\s*M", lines[index + 1], re.I) else ""
                    return f"{cross_line.group(1).replace(' ', '')}{suffix}"
    return None


def clean_price_token(value: str) -> str:
    return re.sub(r"\s+", "", value)


def infer_currency(text: str) -> Optional[str]:
    if "¥" in text or "￥" in text:
        return "CNY"
    if "$" in text:
        return "USD"
    return None


def extract_payload(text: str) -> Dict[str, Any]:
    cleaned = normalize_text(text)
    payload: Dict[str, Any] = {
        "model_name": extract_model_name(cleaned) or "gpt5.4",
        "multiplier": 1,
        "recharge_ratio": "1:1",
        "detected_currency": infer_currency(cleaned) or "UNKNOWN",
    }

    for field, aliases in ALIASES:
        value = find_price_for_alias(cleaned, aliases)
        if value:
            payload[field] = clean_price_token(value)

    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract model pricing fields from raw text.")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--text", help="Raw text to parse")
    source_group.add_argument("--text-file", help="Path to a text file to parse")
    args = parser.parse_args()

    if args.text_file:
        with open(args.text_file, "r", encoding="utf-8-sig") as file:
            text = file.read()
    else:
        text = args.text

    print(json.dumps(extract_payload(text), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
