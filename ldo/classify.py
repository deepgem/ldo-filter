"""L3 语义判定：调用 OpenAI 兼容端点，输出结构化 verdict。"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, net

PROMPT = """你是信息筛选器。用户只关心四类内容，其余一律低分。

【share_project 分享项目】作者自己发布/自荐的开源项目、工具、脚本、插件、网站、服务。
  特征：GitHub/Gitee 链接、"我做了个…"、"自荐"、演示地址、可运行的产品。
【share_resource 分享资源】可获取的具体资源：免费额度/福利、镜像站、数据集、
  配置/订阅文件、白嫖渠道、实用链接合集、破解激活思路。
【service_update 服务动态】某个具体平台/服务/工具的规则发生变化，影响用户实际能用多少、
  怎么用：额度调整、限速、涨价降价、封号政策收紧、免费额度取消、功能下线或上线。
  特征：出现具体服务名 + 具体变化（数字/限制/时间）。
  这类内容即使没有链接、没有操作步骤也有价值——它决定用户手头的资源还能不能用。
【tutorial 教程】有实质操作步骤的技术文章或指南（可给中等分）。

其余一律低分：
- question：提问求助、报错排查、求推荐、求证讨论
- news：纯行业新闻、公司战略、融资并购、技术论文、与"我能怎么用"无关的资讯
- chat：情绪吐槽、日常闲聊、晒图记录
- promo：推销自家中转站/API 服务的软文，本质是引流
- spam：无信息量水贴

输入：
话题标题：{topic}
正文：{text}
链接：{links}

评分标准：
0.85-1.00 明确的自荐项目或高价值资源，含可直接访问的链接
0.60-0.84 疑似分享信息不完整；或影响实际使用的服务规则变更
0.35-0.59 教程、实测记录，看情况
0.00-0.34 无关内容

关键区分：
- "workbuddy 免费额度开始限制每小时用量" → service_update 0.72（影响我怎么用）
- "Anthropic 将 API 上限提高到 128MB" → news 0.25（行业播报，跟我没关系）
- "【某中转站】双十一送额度" → promo 0.20（引流软文，不是服务变更）

只输出 JSON，不要任何解释：
{"label":"share_project|share_resource|service_update|tutorial|question|chat|news|promo|spam","score":0.0,"tags":["..."],"reason":"不超过30字","extracted":{"name":"","url":"","summary":"不超过40字"}}
"""


def build_prompt(msg: dict, cfg: dict) -> str:
    max_chars = cfg["llm"]["max_chars"]
    text = (msg["text"] or "")[:max_chars]
    links = msg["links"] if isinstance(msg["links"], list) else json.loads(msg["links"] or "[]")
    p = (
        PROMPT.replace("{topic}", (msg["topic"] or "(无)")[:160])
        .replace("{text}", text or "(无正文)")
        .replace("{links}", "\n".join(links[:8]) or "(无)")
    )
    ex = cfg.get("examples") or []
    if ex:
        block = "\n\n已校准示例（按此标准打分）：\n" + "\n".join(
            f"- [{e['label']} {e['score']}] 标题「{e.get('topic','')}」→ {e.get('why','')}" for e in ex
        )
        p += block
    return p


JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_verdict(content: str) -> dict:
    m = JSON_RE.search(content or "")
    if not m:
        raise ValueError(f"no json in response: {content[:200]}")
    data = json.loads(m.group(0))
    score = float(data.get("score") or 0.0)
    data["score"] = max(0.0, min(1.0, score))
    data.setdefault("label", "unknown")
    data.setdefault("tags", [])
    data.setdefault("reason", "")
    data.setdefault("extracted", {})
    return data


def classify_one(msg: dict, cfg: dict) -> dict:
    llm = cfg["llm"]
    payload = {
        "model": llm["model"],
        "messages": [{"role": "user", "content": build_prompt(msg, cfg)}],
        "temperature": llm.get("temperature", 0),
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {llm['api_key']}"}
    resp = net.http_post_json(
        llm["base_url"].rstrip("/") + "/chat/completions",
        payload,
        timeout=llm["timeout_sec"],
        retries=3,
        headers=headers,
    )
    content = resp["choices"][0]["message"].get("content") or ""
    return parse_verdict(content)


def run_pending(conn, cfg: dict, limit: int = 500, verbose: bool = True) -> int:
    version = cfg["llm"]["prompt_version"]
    min_pre = cfg["rules"]["min_prescore_for_llm"]
    rows = db.pending_for_llm(conn, version, min_pre, limit)
    if not rows:
        if verbose:
            print("[llm] 无待判定消息")
        return 0

    ok = fail = 0
    concurrency = max(1, int(cfg["llm"]["concurrency"]))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(classify_one, dict(r), cfg): dict(r) for r in rows}
        for fut in as_completed(futures):
            msg = futures[fut]
            try:
                v = fut.result()
                db.save_verdict(conn, msg["msg_id"], version, v, cfg["llm"]["model"])
                ok += 1
                if verbose:
                    print(f"[llm] {msg['msg_id']} {v['score']:.2f} {v['label']:<14} {(msg['topic'] or '')[:34]}")
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[llm] FAIL {msg['msg_id']}: {e}")
    if verbose:
        print(f"[llm] done ok={ok} fail={fail}")
    return ok
