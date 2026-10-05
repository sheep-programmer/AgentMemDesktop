"""对正在运行的后端做一次端到端冒烟：真实模型、真实摄取、真实检索与引用定位。

用法::

    uv run agentmem serve                  # 先起后端
    uv run python scripts/e2e_live.py      # 再跑本脚本

会新建一个临时 Space，跑完删掉（加 ``--keep`` 保留，方便在界面里看）。
检查的不只是「能答」：每条引用都要落在正确的章节、原文区间要和切片逐字一致。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

BASE = "http://127.0.0.1:8765/api/v1"

DOCUMENT = """# 星澜 X3 储能系统技术手册

## 一、产品概述

星澜 X3 是一款面向家庭与小型商铺的磷酸铁锂储能系统，额定容量 15.36 kWh，
由 3 个 5.12 kWh 电池模块串联组成，支持离网与并网两种工作模式。

## 二、安装要求

### 2.1 环境条件

安装位置的环境温度需保持在 -10℃ 至 45℃ 之间，相对湿度不超过 85%，
并且远离热源至少 1.5 米。设备不得安装在阳光直射的位置。

### 2.2 接线规范

直流侧电缆截面积不得小于 35 mm²，交流输出端必须配置 63A 的空气断路器。
接地电阻应小于 4 欧姆，接地线采用黄绿双色线。

## 三、故障代码

### 3.1 E07 过温保护

当电芯温度超过 58℃ 时系统报 E07 并自动降额至 50% 功率；
温度回落到 50℃ 以下后 10 分钟自动恢复。若一小时内触发三次，系统锁定并需人工复位。

### 3.2 E12 绝缘异常

E12 表示直流侧对地绝缘电阻低于 100 kΩ。处理方法：断开直流开关，
用 1000V 兆欧表逐串测量，找到绝缘破损的线缆后更换。

## 四、质保条款

电池模块质保 10 年或 6000 次循环（以先到者为准），循环结束时容量保持率不低于 70%。
逆变器质保 5 年。人为进水、私自拆机不在质保范围内。
"""

#: 问题 → 答案应当出自的章节（heading_path 里要出现这个片段）与答案必须含的事实
QUESTIONS = [
    ("E07 报警是什么意思？触发后系统会怎样，多久恢复？", "E07", ["58", "50%", "10 分钟"]),
    ("交流输出端的断路器要多大？接地电阻有什么要求？", "接线规范", ["63A", "4"]),
    ("电池模块质保多久？", "质保", ["10 年", "6000"]),
]

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(("  ✓ " if ok else "  ✗ ") + message)
    if not ok:
        failures.append(message)


def sse(
    client: httpx.Client, url: str, body: dict[str, Any]
) -> tuple[list[tuple[str, Any]], float | None, float]:
    """发一次 SSE 请求，返回（事件列表，首字耗时，总耗时）。"""
    events: list[tuple[str, Any]] = []
    started = time.perf_counter()
    first_delta: float | None = None
    with client.stream("POST", url, json=body, timeout=300) as response:
        response.raise_for_status()
        name = "message"
        for line in response.iter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                raw = line[5:].strip()
                try:
                    data: Any = json.loads(raw)
                except json.JSONDecodeError:
                    data = raw
                if name == "delta" and first_delta is None:
                    first_delta = time.perf_counter() - started
                events.append((name, data))
    return events, first_delta, time.perf_counter() - started


def bind_chat(client: httpx.Client, provider_id: str) -> None:
    roles = client.get(f"{BASE}/providers/roles").json()
    roles = roles.get("roles", roles)
    roles["chat"] = provider_id
    client.put(f"{BASE}/providers/roles", json={"roles": roles}).raise_for_status()


def ask(
    client: httpx.Client,
    space_id: str,
    model_label: str,
    question: str,
    section: str,
    facts: list[str],
    content: str,
) -> str | None:
    conversation = client.post(
        f"{BASE}/spaces/{space_id}/conversations",
        json={"space_id": space_id, "title": question[:20]},
    ).json()
    events, ttft, total = sse(
        client,
        f"{BASE}/conversations/{conversation['id']}/chat",
        {"content": question, "deep_thinking": False},
    )
    names = [name for name, _ in events]
    errors = [data for name, data in events if name == "error"]
    answer = "".join(
        (data.get("text") or data.get("content") or "") if isinstance(data, dict) else str(data)
        for name, data in events
        if name == "delta"
    )
    citations = [data for name, data in events if name == "citation"]
    citations = [
        item
        for data in citations
        for item in (data if isinstance(data, list) else data.get("citations", [data]))
    ]
    done = next((data for name, data in events if name == "done"), {}) or {}
    print(f"\n[{model_label}] {question}")
    print(f"  首字 {ttft:.1f}s · 总耗时 {total:.1f}s · 事件 {sorted(set(names))}")
    print(f"  回答：{answer[:160].replace(chr(10), ' ')}{'…' if len(answer) > 160 else ''}")
    check(not errors, f"没有 error 事件 {errors[:1]}")
    check(bool(answer.strip()), "回答非空")
    for fact in facts:
        check(fact.replace(" ", "") in answer.replace(" ", ""), f"回答包含事实「{fact}」")
    check(bool(citations), f"带引用（{len(citations)} 条）")
    for citation in citations:
        heading = citation.get("heading_path") or ""
        start, end = citation.get("char_start"), citation.get("char_end")
        title, ordinal = citation.get("document_title"), citation.get("ordinal")
        where = f"{title} · {heading} · 第{ordinal}段 · [{start},{end})"
        print(f"    [{citation.get('marker')}] {where}")
        quote = citation.get("quote")
        if quote:
            q0, q1 = citation.get("quote_start"), citation.get("quote_end")
            print(f"        原句 [{q0},{q1}) 「{quote[:70]}」")
            check(content[q0:q1] == quote, f"{citation.get('marker')} 原句区间与全文逐字一致")
        if citation.get("kind") == "body" and start is not None and end is not None:
            snippet_source = content[start:end]
            check(bool(snippet_source.strip()), f"{citation.get('marker')} 原文区间非空")
    check(
        any(section in (citation.get("heading_path") or "") for citation in citations),
        f"至少一条引用落在「{section}」章节",
    )
    usage = done.get("usage") or done.get("token_usage") or {}
    if usage:
        print(f"  token：{usage}")
    return done.get("trace_id") or next(
        (
            data.get("trace_id")
            for name, data in events
            if name == "trace_start" and isinstance(data, dict)
        ),
        None,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="跑完保留测试 Space")
    args = parser.parse_args()
    client = httpx.Client(timeout=60)

    print("== 模型连通")
    roles = client.get(f"{BASE}/providers/roles").json()
    roles = roles.get("roles", roles)
    print(f"  角色绑定：{roles}")
    for provider_id in ("mirasim-deepseek", "mirasim-kimi"):
        started = time.perf_counter()
        health = client.post(f"{BASE}/providers/{provider_id}/health", timeout=120).json()
        check(
            bool(health.get("ok")),
            f"{provider_id} 健康检查 {time.perf_counter() - started:.1f}s "
            f"{health.get('error') or ''}",
        )

    print("\n== 摄取")
    space = client.post(
        f"{BASE}/spaces", json={"name": "E2E 储能手册", "domain": "储能系统运维"}
    ).json()
    space_id = space["id"]
    try:
        document = client.post(
            f"{BASE}/spaces/{space_id}/documents/paste",
            json={"title": "星澜 X3 技术手册", "content": DOCUMENT},
        ).json()
        started = time.perf_counter()
        while True:
            document = client.get(f"{BASE}/spaces/{space_id}/documents/{document['id']}").json()
            if document["status"] in ("ready", "failed") or time.perf_counter() - started > 300:
                break
            time.sleep(1)
        print(f"  摄取耗时 {time.perf_counter() - started:.1f}s，状态 {document['status']}")
        check(document["status"] == "ready", f"文档摄取成功 {document.get('error') or ''}")
        meta = document.get("meta") or {}
        print(f"  文档概要：{(meta.get('context_summary') or '')[:120]}")
        print(f"  抽取：{meta.get('extraction')} {meta.get('extraction_error') or ''}")
        chunks = client.get(f"{BASE}/spaces/{space_id}/documents/{document['id']}/chunks").json()
        chunks = chunks.get("items", chunks)
        body = [chunk for chunk in chunks if chunk.get("kind", "body") == "body"]
        check(bool(body), f"切片 {len(chunks)} 条")
        stored = client.get(f"{BASE}/spaces/{space_id}/documents/{document['id']}/content").json()
        content = stored.get("content") or stored.get("markdown") or DOCUMENT
        check(
            all(
                content[chunk["char_start"] : chunk["char_end"]] == chunk["content"]
                for chunk in body
            ),
            "每条正文切片与原文区间逐字一致",
        )

        print("\n== 检索")
        started = time.perf_counter()
        result = client.post(
            f"{BASE}/spaces/{space_id}/search", json={"query": "E12 绝缘异常怎么处理"}
        ).json()
        hits = result.get("hits") or result.get("items") or []
        print(f"  {time.perf_counter() - started:.2f}s，命中 {len(hits)}")
        top = hits[0] if hits else {}
        top_chunk = next((chunk for chunk in chunks if chunk["id"] == top.get("chunk_id")), {})
        check("E12 表示" in (top_chunk.get("content") or ""), "第一条命中包含 E12 的处理说明")

        traces: list[str] = []
        for provider_id, label in (
            ("mirasim-deepseek", "deepseek-flash"),
            ("mirasim-kimi", "kimi-k3"),
        ):
            bind_chat(client, provider_id)
            for question, section, facts in QUESTIONS:
                trace = ask(client, space_id, label, question, section, facts, content)
                if trace:
                    traces.append(trace)
        bind_chat(client, "mirasim-deepseek")

        print("\n== 评审（judge 角色 = kimi-k3）")
        if traces:
            started = time.perf_counter()
            judged = client.post(f"{BASE}/traces/{traces[0]}/judge", timeout=300)
            print(
                f"  {time.perf_counter() - started:.1f}s "
                f"HTTP {judged.status_code} {judged.text[:300]}"
            )
            check(judged.status_code == 200, "judge 打分成功")
        else:
            check(False, "拿到 trace_id 用于评审")
    finally:
        if args.keep:
            print(f"\n保留测试 Space：{space_id}")
        else:
            client.delete(f"{BASE}/spaces/{space_id}")

    print(f"\n{'全部通过' if not failures else f'{len(failures)} 项未通过'}")
    for item in failures:
        print(f"  - {item}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
