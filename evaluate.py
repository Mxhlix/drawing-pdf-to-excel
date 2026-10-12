"""見本の図面15枚（5業種 × 3つの形）を読み、正解と突き合わせて数える。

欄ごとに次の4つに分ける。
  正しく入った        値が正解と同じで、色が付いていない
  色付き（正しい）    値は正解と同じだが、確かめてほしい印が付いた
  色付き（違う）      値が違い、印が付いた（担当者が画面で直す）
  色なしで違う        値が違うのに印が付いていない（見落としにつながる。これを0にしたい）
正解の行が1行も拾えなかった欄は「拾えなかった」に数える。

  $ python evaluate.py            # 結果を results/eval.json と画面に出す
"""
import json
import sys
import time
import unicodedata
from pathlib import Path

from zumen.mapper import load_configs, load_settings, map_items, pick_config
from zumen.reader import read_pdf

HERE = Path(__file__).parent
KEYS = ["house", "aircon", "lighting", "interior", "part"]
KINDS = ["text", "outline", "scan"]


def n(v):
    return unicodedata.normalize("NFKC", str(v)).replace(" ", "").upper()


def row_key(cfg, rec, get):
    rc = cfg["records"]
    if rc["mode"] == "rooms":
        k = [rc["anchor"]["field"]] + [f for a in rc["attrs"] if a.get("multi") for f in a["groups"]]
    else:
        k = [rc["key"]]
    return tuple(n(get(rec, f)) for f in k)


def evaluate(key, kind, configs, settings):
    cfg = configs[key]
    truth = json.loads((HERE / "samples" / f"{key}_truth.json").read_text(encoding="utf-8"))
    r = read_pdf(str(HERE / "samples" / f"{key}_{kind}.pdf"))
    picked = pick_config(r["items"], configs)
    res = map_items(r["items"], cfg, settings, r["ctx"])
    cnt = {"正しく入った": 0, "色付き（正しい）": 0, "色付き（違う）": 0, "色なしで違う": 0, "拾えなかった": 0}
    bad = []

    def judge(label, c, want):
        got = c["value"] if c else ""
        ok = n(got) == n(want)
        if c is None:
            cnt["拾えなかった"] += 1
            bad.append(f"{label}: 拾えなかった（正解 {want}）")
        elif ok and not c["flag"]:
            cnt["正しく入った"] += 1
        elif ok:
            cnt["色付き（正しい）"] += 1
        elif c["flag"]:
            cnt["色付き（違う）"] += 1
            bad.append(f"{label}: 色付き「{got}」（正解 {want}）")
        else:
            cnt["色なしで違う"] += 1
            bad.append(f"{label}: 色なしで違う「{got}」（正解 {want}）")

    for f in cfg["header"]:
        judge(f["field"], res["header"].get(f["field"]), truth["header"].get(f["field"], ""))
    cols = [c for c in cfg["records"]["columns"] if c in truth["records"][0]]
    pred = {row_key(cfg, p, lambda r, f: r.get(f, {}).get("value", "")): p for p in res["records"]}
    used = set()
    for t in truth["records"]:
        k = row_key(cfg, t, lambda r, f: r.get(f, ""))
        p = pred.get(k)
        if p is not None:
            used.add(k)
        for c in cols:
            judge(f"{'/'.join(k)} {c}", p.get(c) if p else None, t[c])
    extra = [p for k, p in pred.items() if k not in used]
    for p in extra:
        flagged = any(c.get("flag") for c in p.values())
        bad.append(f"余分な行 {'/'.join(row_key(cfg, p, lambda r, f: r.get(f, {}).get('value', '')))}"
                   f"（{'色付き' if flagged else '色なし'}）")
        if not flagged:
            cnt["色なしで違う"] += 1
    return {"key": key, "kind": kind, "kind_label": r["kind_label"], "detected": r["kind"], "picked": picked,
            "seconds": r["seconds"], "counts": cnt, "total": sum(cnt.values()), "problems": bad}


def main():
    configs, settings = load_configs(), load_settings()
    keys = sys.argv[1:] or KEYS
    out = []
    for key in keys:
        for kind in KINDS:
            e = evaluate(key, kind, configs, settings)
            out.append(e)
            c = e["counts"]
            print(f"{key:9s} {e['kind_label']:4s} 様式{'○' if e['picked'] == key else '×'} {e['seconds']:5.1f}秒 "
                  f"欄{e['total']:3d} 正{c['正しく入った']:3d} 色正{c['色付き（正しい）']:2d} "
                  f"色違{c['色付き（違う）']:2d} 色なし違{c['色なしで違う']:2d} 拾えず{c['拾えなかった']:2d}")
            for b in e["problems"]:
                print("    ", b)
            sys.stdout.flush()
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "eval.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
