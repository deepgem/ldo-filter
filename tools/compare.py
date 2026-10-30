"""对比测试：同一批消息，通用 LLM vs TypeSafe Jev。

用法：
    python3 -m tools.compare --n 20          # 用 v3 已有判定做参照
    python3 -m tools.compare --n 20 --time 5 # 额外对 5 条重新计时两种后端
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import time

from ldo import classify, classify_jev
from ldo.config import load_config

PAD = 42


def _short(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s[: n - 1] + "…" if len(s) > n else s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="对比条数")
    ap.add_argument("--time", type=int, default=5, help="额外计时条数")
    ap.add_argument("--version", default="v3", help="作为参照的 LLM 判定版本")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg["jev"]["api_key"]:
        raise SystemExit("TYPESAFE_API_KEY 未配置")

    conn = sqlite3.connect(cfg["db_path"])
    conn.row_factory = sqlite3.Row

    # 挑覆盖面广的样本：每种 label 都取几条
    rows = conn.execute(
        """SELECT m.*, v.label AS llm_label, v.score AS llm_score
           FROM messages m JOIN verdicts v ON v.msg_id=m.msg_id AND v.version=?
           ORDER BY v.score DESC LIMIT ?""",
        (args.version, args.n // 2),
    ).fetchall()
    rows += conn.execute(
        """SELECT m.*, v.label AS llm_label, v.score AS llm_score
           FROM messages m JOIN verdicts v ON v.msg_id=m.msg_id AND v.version=?
           ORDER BY v.score ASC LIMIT ?""",
        (args.version, args.n - len(rows)),
    ).fetchall()

    print(f"对比 {len(rows)} 条 | LLM 参照版本 {args.version}\n")
    print(f"{'msg_id':<8}{'LLM':<22}{'JEV':<26}{'一致':<6}话题")
    print("-" * 110)

    agree = 0
    score_gaps: list[float] = []
    jev_times: list[float] = []
    detail: list[tuple] = []

    for r in rows:
        msg = dict(r)
        t0 = time.perf_counter()
        try:
            jv = classify_jev.classify_one(msg, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"{msg['msg_id']:<8}JEV 失败: {e}")
            continue
        jev_times.append(time.perf_counter() - t0)

        llm_s = f"{r['llm_label']} {r['llm_score']:.2f}"
        jev_s = f"{jv['label']} {jv['score']:.2f}"
        same = r["llm_label"] == jv["label"]
        agree += same
        score_gaps.append(abs(r["llm_score"] - jv["score"]))
        detail.append((msg, r, jv, same))

        print(
            f"{msg['msg_id']:<8}{llm_s:<22}{jev_s:<26}{'✓' if same else '✗':<6}"
            f"{_short(msg['topic'] or msg['text'], 44)}"
        )

    n = len(detail)
    print("-" * 110)
    print(f"分类一致率 : {agree}/{n} = {agree / n * 100:.0f}%")
    print(f"分数平均差 : {statistics.mean(score_gaps):.3f}")
    print(f"Jev 平均耗时: {statistics.mean(jev_times) * 1000:.0f} ms/条")

    # 推送决策一致性：以 push 阈值 0.55 为界
    cfg_thr = cfg["thresholds"]["push"]
    llm_push = {d[0]["msg_id"] for d in detail if d[1]["llm_score"] >= cfg_thr}
    jev_push = {d[0]["msg_id"] for d in detail if d[2]["score"] >= cfg_thr}
    print(f"\n推送决策（阈值 {cfg_thr}）:")
    print(f"  LLM 推 {len(llm_push)} 条 / Jev 推 {len(jev_push)} 条")
    print(f"  两者都推 {len(llm_push & jev_push)} 条，仅 LLM 推 {len(llm_push - jev_push)} 条，仅 Jev 推 {len(jev_push - llm_push)} 条")

    if args.time:
        print(f"\n--- 计时对比（{args.time} 条，两边都重新调用）---")
        llm_t, jev_t = [], []
        for msg, _r, _jv, _s in detail[: args.time]:
            t0 = time.perf_counter()
            classify.classify_one(msg, cfg)
            llm_t.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            classify_jev.classify_one(msg, cfg)
            jev_t.append(time.perf_counter() - t0)
        print(f"LLM : {statistics.mean(llm_t) * 1000:.0f} ms/条（{min(llm_t)*1000:.0f}~{max(llm_t)*1000:.0f}）")
        print(f"Jev : {statistics.mean(jev_t) * 1000:.0f} ms/条（{min(jev_t)*1000:.0f}~{max(jev_t)*1000:.0f}）")
        print(f"提速: {statistics.mean(llm_t) / statistics.mean(jev_t):.1f}x")


if __name__ == "__main__":
    main()
