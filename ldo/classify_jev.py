"""L3 判定后端 B：TypeSafe Jev（System One 模型）。

与 classify.py（通用 LLM + JSON 生成）互补：
- Jev 不生成文本，直接返回结构化判定，无 output token 开销，延迟低
- 一次请求打包多个问题，成本几乎不随问题数增加
- 返回 confidence，可用于置信度路由（低置信度回退给 LLM 复判）

端点：POST https://api.typesafe.ai/v1/systemone
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, net

# 与 classify.py 保持同一套标签，保证下游 publish/db 不用改
CATEGORIES = {
    "share_project": "作者自己做出来并公开分享的项目、工具、脚本、插件、网站、免费服务，可直接使用，通常带链接或演示地址。作者自荐的免费工具即使带宣传口吻也算这一类",
    "share_resource": "第三方视角分享的可获取资源：免费额度渠道、镜像站、数据集、配置订阅文件、白嫖思路、实用链接合集。必须是分享者或用户实测，不是商家自发的活动",
    "service_update": "某个具体平台或服务的规则已真实发生变化，影响用户实际能用多少、怎么用：额度调整、限速、涨价降价、封号政策收紧、免费额度取消、功能下线或上线。注意：用户吐槽、段子、假设性描述、营销话术不算，必须是真实发生的服务变更",
    "tutorial": "有实质操作步骤的技术文章或指南",
    "question": "提问求助、报错排查、求推荐、求证讨论，只有疑问没有给出方案",
    "news": "纯行业新闻、公司战略、融资并购、技术论文，与用户实际怎么用无关",
    "chat": "情绪吐槽、日常闲聊、晒图记录、生活分享",
    "promo": "商家自己发的拉新广告：充值折扣、注册送额度、回帖领奖、进群领福利、代充代付。linux.do 社区里带【富可敌国】标签的帖子一律属于此类。作者免费分享自己做的工具不算 promo",
    "spam": "无信息量的水贴、灌水、广告",
}

# 这些类别即使 worth 判高，也不该越过推送线
LOW_VALUE_LABELS = {"question", "news", "chat", "promo", "spam"}
LOW_VALUE_CAP = 0.35

WORTH_INSTRUCTIONS = (
    "这条内容值得推送给一个只关心三类信息的用户吗："
    "①别人分享的自荐项目或开源工具；②可获取的具体资源（免费额度、白嫖渠道、教程、链接合集）；"
    "③影响实际使用的服务规则变更（某平台限速了、额度取消了、涨价了）。"
    "提问求助、纯行业新闻、情绪闲聊、引流软文都不值得推送。"
)


def build_state(msg: dict, cfg: dict) -> str:
    """拼成 Jev 的 state 输入。"""
    max_chars = cfg["llm"]["max_chars"]
    text = (msg.get("text") or "")[:max_chars]
    links = msg.get("links")
    if isinstance(links, str):
        try:
            links = json.loads(links or "[]")
        except json.JSONDecodeError:
            links = []
    parts = [f"话题标题：{msg.get('topic') or '(无)'}"]
    if text:
        parts.append(f"正文：{text}")
    if links:
        parts.append("链接：" + " ".join(links[:6]))
    return "\n".join(parts)


def build_payload(msg: dict, cfg: dict) -> dict:
    return {
        "model": cfg["jev"]["model"],
        "state": build_state(msg, cfg),
        "questions": {
            "category": {
                "type": "choice",
                "instructions": "这条 Telegram 频道消息属于哪一类？",
                "criteria": CATEGORIES,
            },
            "worth": {
                "type": "noul",
                "instructions": WORTH_INSTRUCTIONS,
            },
        },
    }


# linux.do 社区广告标签与拉新话术，Jev 不懂这类黑话，代码兜底
AD_TAG = "富可敌国"          # 商家广告专用标签，只出现在标题
LURE_PHRASES = ("回帖留id", "回帖留 id", "回帖领", "进群送", "进群领", "留言送")


def _is_promo(msg: dict) -> bool:
    topic = (msg.get("topic") or "").lower()
    text = (msg.get("text") or "").lower()
    if AD_TAG in topic:
        return True
    return any(p in text for p in LURE_PHRASES)


def classify_one(msg: dict, cfg: dict) -> dict:
    """返回与 classify.classify_one 同构的 verdict。"""
    jev = cfg["jev"]
    resp = net.http_post_json(
        jev["base_url"].rstrip("/") + "/systemone",
        build_payload(msg, cfg),
        timeout=jev.get("timeout_sec", 30),
        retries=3,
        headers={"Authorization": f"Bearer {jev['api_key']}"},
    )

    answers = resp.get("answers") or {}
    cat = answers.get("category") or {}
    worth = answers.get("worth") or {}

    label = cat.get("choice") or "unknown"
    conf = float(cat.get("confidence") or 0.0)
    score = float(worth.get("noul") or 0.0)

    # 低置信度：分类不可靠，只轻微打折，不硬压
    if conf < cfg["jev"].get("min_confidence", 0.55):
        score *= 0.85

    # 类别一致性：低价值类别一律不越过推送线
    # 高置信度压到 LOW_VALUE_CAP；低置信度只压到阈值以下，保留分数供人工查看
    if label in LOW_VALUE_LABELS:
        cap = LOW_VALUE_CAP if conf >= cfg["jev"].get("cap_confidence", 0.55) else 0.54
        score = min(score, cap)

    if _is_promo(msg):
        label = "promo"
        score = min(score, 0.25)

    return {
        "label": label,
        "score": max(0.0, min(1.0, score)),
        "tags": [],
        "reason": f"jev conf={conf:.2f}",
        "extracted": {},
    }


def run_pending(conn, cfg: dict, limit: int = 15, verbose: bool = True) -> int:
    """并发判定待处理消息。写库留在主线程，避免 SQLite 跨线程问题。"""
    version = cfg["llm"]["prompt_version"]
    min_pre = cfg["rules"]["min_prescore_for_llm"]
    rows = db.pending_for_llm(conn, version, min_pre, limit)
    if not rows:
        if verbose:
            print("[jev] 无待判定消息")
        return 0

    ok = fail = 0
    concurrency = max(1, int(cfg["jev"].get("concurrency", 6)))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(classify_one, dict(r), cfg): dict(r) for r in rows}
        for fut in as_completed(futures):
            msg = futures[fut]
            try:
                v = fut.result()
                db.save_verdict(conn, msg["msg_id"], version, v, cfg["jev"]["model"])
                ok += 1
                if verbose:
                    print(f"[jev] {msg['msg_id']} {v['score']:.2f} {v['label']:<14} {(msg['topic'] or '')[:36]}")
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[jev] FAIL {msg['msg_id']}: {e}")
    if verbose:
        print(f"[jev] done ok={ok} fail={fail}")
    return ok
