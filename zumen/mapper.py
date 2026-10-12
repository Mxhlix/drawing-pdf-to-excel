"""読み取った文字を、様式の欄に振り分ける。

振り分けのしかたは様式ごとの設定ファイル（config/*.yaml）に書く。
部屋名のような決まった言葉の一覧と、部屋名から決まる項目（区分など）は 設定.xlsx に置き、
担当者が Excel で書き足せるようにする。

振り分けは3種類。
  header  表題欄の「項目名 → 右隣の値」（工事名称・図番・日付など）
  rooms   平面図の部屋名の近くにある値（帖数・天井高・器具の数など）
  table   図面の中の表（機器表・仕上表・部品欄）の行

値ごとに「確かさ」と、確かめてほしい理由を付ける。色を付ける基準は次のとおり。
  読み取りが不確か（文字認識の確かさが 0.90 未満）
  形が合わない（図番・日付・台数などの決まった形に合わない）
  一覧で読み替えた（部屋名の一覧で一番近い言葉に置き換えた。近さが 85 未満なら色）
  図面と表が合わない（機器表の台数と、図面上の記号の数が違う）
"""
from __future__ import annotations

import re
from pathlib import Path

import openpyxl
import yaml
from rapidfuzz import fuzz

HERE = Path(__file__).resolve().parent.parent
CONF_OK = 0.90
VOCAB_OK = 85


# ---------------------------------------------------------------- 設定

def load_configs() -> dict[str, dict]:
    out = {}
    for p in sorted((HERE / "config").glob("*.yaml")):
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        out[cfg["key"]] = cfg
    return out


def load_settings() -> dict[str, list[dict]]:
    """設定.xlsx の各シートを「1行目が見出しの表」として読む。"""
    wb = openpyxl.load_workbook(HERE / "設定.xlsx", data_only=True)
    out = {}
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        head = [str(h) for h in rows[0] if h]
        out[ws.title] = [dict(zip(head, [("" if v is None else str(v)) for v in r])) for r in rows[1:] if r[0]]
    return out


def pick_config(items: list[dict], configs: dict[str, dict]) -> str:
    """図面の中の言葉から、どの様式かを選ぶ（担当者が画面で選び直せる）。"""
    text = " ".join(i["text"] for i in items)
    scores = {k: sum(text.count(w) for w in c.get("detect", [])) for k, c in configs.items()}
    return max(scores, key=scores.get)


# ---------------------------------------------------------------- 共通

def center(b):
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def height(i):
    b = i["bbox"]
    return min(b[2] - b[0], b[3] - b[1]) if i.get("vertical") or i.get("rotated") else b[3] - b[1]


def cell(value, item=None, conf=None, reasons=None, note=None, derived=False):
    c = conf if conf is not None else (item["conf"] if item else 0.0)
    rs = list(reasons or [])
    if item is not None and c < CONF_OK:
        rs.insert(0, f"読み取りが不確か（確かさ {c:.2f}）")
    if item is not None and item.get("suspect"):
        rs.append(item["suspect"])
    if note is None and item is not None and item.get("agreed"):
        note = "読み方を変えて3回読み、同じ結果"
    return {"value": value, "conf": round(c, 3), "bbox": item["bbox"] if item else None,
            "raw": item["text"] if item else "", "flag": bool(rs), "reasons": rs, "note": note,
            "derived": derived}


def split_tokens(items: list[dict], patterns: list[str]) -> list[dict]:
    """1つの枠に入った複数の値（例:「押入1.5帖」「A×6 B×2」）を、値の形で切り分ける。"""
    out = []
    rx = [re.compile(p.strip("^$")) for p in patterns]
    for it in items:
        t = it["text"]
        if len(t) > 12:                      # 長い文は注記。切り分けない
            out.append(it)
            continue
        spans = sorted({m.span() for r in rx for m in r.finditer(t)})
        spans = [s for k, s in enumerate(spans) if not any(o[0] <= s[0] and s[1] <= o[1] and o != s for o in spans)]
        if not spans or (len(spans) == 1 and spans[0] == (0, len(t))):
            out.append(it)
            continue
        cuts, pos = [], 0
        for a, b in spans:
            if t[pos:a].strip():
                cuts.append((pos, a))
            cuts.append((a, b))
            pos = b
        if t[pos:].strip():
            cuts.append((pos, len(t)))
        x0, y0, x1, y1 = it["bbox"]
        n = max(len(t), 1)
        for a, b in cuts:
            sub = dict(it)
            sub["text"] = t[a:b].strip()
            if it.get("vertical") or it.get("rotated"):
                sub["bbox"] = [x0, y0 + (y1 - y0) * a / n, x1, y0 + (y1 - y0) * b / n]
            else:
                sub["bbox"] = [x0 + (x1 - x0) * a / n, y0, x0 + (x1 - x0) * b / n, y1]
            out.append(sub)
    return out


def token_fix(text: str, vocab: list[str]):
    """欄の中の日本語の語（かな・漢字の続き）ごとに、一覧の一番近い言葉に置き換える。
    「長尺塩ビートt2.0」→「長尺塩ビシートt2.0」。近さ 75 未満は置き換えない。"""
    changes = []

    def fix(m):
        w = m.group(0)
        if len(w) < 3 or w in vocab:
            return w
        best, score = vocab_match(w, vocab)
        if score >= 75:
            changes.append((w, best, score))
            return best
        return w
    return re.sub(r"[\u3040-\u30ff\u4e00-\u9fffー]+", fix, text), changes


def vocab_match(text: str, vocab: list[str]) -> tuple[str, float]:
    t = text.upper().replace(" ", "")
    best, score = "", 0.0
    for v in vocab:
        s = 100.0 if t == v.upper() else fuzz.ratio(t, v.upper())
        if s > score:
            best, score = v, s
    return best, score


# ---------------------------------------------------------------- 表題欄

def read_header(items, fields, reserved: set[str]):
    out = {}
    for f in fields:
        cands = []
        for it in items:
            if max(fuzz.ratio(it["text"], lb) for lb in f["labels"]) < 85:
                continue
            lx0, ly0, lx1, ly1 = it["bbox"]
            right = [v for v in items if v is not it and v["bbox"][0] > lx1 - 2 and v["bbox"][0] - lx1 < 140
                     and min(v["bbox"][3], ly1) - max(v["bbox"][1], ly0) > 0.5 * min(ly1 - ly0, v["bbox"][3] - v["bbox"][1])]
            if not right:
                continue
            v = min(right, key=lambda v: v["bbox"][0])
            if v["text"] in reserved:
                continue
            cands.append((ly1, v))
        if not cands:
            out[f["field"]] = cell("", reasons=["見つかりません"])
            continue
        _, v = max(cands, key=lambda c: c[0])          # 同じ項目名が2つあれば下のほう（表題欄）
        reasons = []
        if f.get("check") and not re.fullmatch(f["check"], v["text"]):
            reasons.append("形が合わない")
        out[f["field"]] = cell(v["text"], v, reasons=reasons)
    return out


# ---------------------------------------------------------------- 平面図の部屋

def read_rooms(items, rc, settings):
    vocab = [r[rc["anchor"]["vocab_column"]] for r in settings[rc["anchor"]["vocab"]]]
    pats = [a["pattern"] for a in rc["attrs"]]
    toks = split_tokens(items, pats)
    anchors, values = [], []
    for t in toks:
        up = dict(t, text=t["text"].upper())
        kind = next((a for a in rc["attrs"] if re.fullmatch(a["pattern"], up["text"])), None)
        if kind:
            values.append((kind, up))
            continue
        word, score = vocab_match(t["text"], vocab)
        if score >= 60 and abs(len(word) - len(t["text"])) <= 2:
            anchors.append((word, score, t))
    # 同じ部屋名が2か所で見つかったら確かなほうを残す（注記の中の言葉などを除く）
    rooms = {}
    for word, score, t in anchors:
        if word not in rooms or score * t["conf"] > rooms[word][0] * rooms[word][1]["conf"]:
            rooms[word] = (score, t)
    rec_by_room = {w: {"anchor": t, "score": s, "vals": {}} for w, (s, t) in rooms.items()}
    orphans = []
    for kind, t in values:
        cx, cy = center(t["bbox"])
        best, dist = None, 1e9
        for w, r in rec_by_room.items():
            x0, y0, x1, y1 = r["anchor"]["bbox"]
            dx, dy = max(x0 - cx, 0, cx - x1), max(y0 - cy, 0, cy - y1)
            d = (dx ** 2 + dy ** 2) ** 0.5
            if d < dist:
                best, dist = w, d
        if best is None or dist > rc["radius"]:
            if best is not None and dist < rc["radius"] * 3:   # 部屋の中にあるのに部屋名が読めなかった値
                orphans.append((kind, t))
            continue
        rec_by_room[best]["vals"].setdefault(kind["field"], []).append(t)
    records = []
    order = sorted(rec_by_room.items(), key=lambda kv: (round(center(kv[1]["anchor"]["bbox"])[1] / 60),
                                                         center(kv[1]["anchor"]["bbox"])[0]))
    for w, r in order:
        a = r["anchor"]
        reasons = []
        note = None
        if a["text"] != w:
            note = f"「{a['text']}」を一覧の「{w}」に読み替え"
            if r["score"] < VOCAB_OK:
                reasons.append(f"一覧で読み替えた（近さ {r['score']:.0f}）")
        base = {rc["anchor"]["field"]: cell(w, a, reasons=reasons, note=note)}
        multi = [k for k in rc["attrs"] if k.get("multi")]
        singles = [k for k in rc["attrs"] if not k.get("multi")]
        for k in singles:
            ts = r["vals"].get(k["field"], [])
            if ts:
                t = min(ts, key=lambda t: abs(center(t["bbox"])[1] - center(a["bbox"])[1]))
                m = re.fullmatch(k["pattern"], t["text"])
                base[k["field"]] = cell(k.get("format", "{1}").format(*([m.group(0)] + list(m.groups()))), t)
            else:
                base[k["field"]] = cell("", conf=1.0, reasons=["見つかりません"] if k.get("required") else None)
        if multi:
            k = multi[0]
            for t in r["vals"].get(k["field"], []):
                m = re.fullmatch(k["pattern"], t["text"])
                rec = dict(base)
                for f, g in k["groups"].items():
                    rec[f] = cell(m.group(g), t)
                records.append(rec)
        else:
            records.append(base)
    groups = []
    for kind, t in orphans:
        c = center(t["bbox"])
        g = next((g for g in groups if any(abs(center(o["bbox"])[0] - c[0]) < rc["radius"]
                                           and abs(center(o["bbox"])[1] - c[1]) < rc["radius"] * 0.6
                                           for _, o in g)), None)
        if g is None:
            groups.append([(kind, t)])
        else:
            g.append((kind, t))
    for g in groups:
        base = {rc["anchor"]["field"]: cell("", reasons=["部屋名が読めません"])}
        multi = [(k, t) for k, t in g if k.get("multi")]
        for k, t in g:
            if not k.get("multi"):
                m = re.fullmatch(k["pattern"], t["text"])
                base[k["field"]] = cell(k.get("format", "{1}").format(*([m.group(0)] + list(m.groups()))), t)
        if multi:
            for k, t in multi:
                m = re.fullmatch(k["pattern"], t["text"])
                rec = dict(base)
                for f, gi in k["groups"].items():
                    rec[f] = cell(m.group(gi), t)
                records.append(rec)
        else:
            records.append(base)
    return records


# ---------------------------------------------------------------- 表

def find_table(items, headers: dict[str, list[str]]):
    """見出しの並んだ行を探し、その下の行を列ごとに読む。"""
    found = []
    for field, labels in headers.items():
        for it in items:
            if max(fuzz.ratio(it["text"], lb) for lb in labels) >= 85:
                found.append((field, it))
    best_row = []
    for _, it in found:
        cy = center(it["bbox"])[1]
        row = {}
        for f, o in found:
            if abs(center(o["bbox"])[1] - cy) < height(it) * 0.8 and f not in row:
                row[f] = o
        if len(row) > len(best_row):
            best_row = row
    if len(best_row) < max(2, len(headers) * 0.6):
        return None, []
    cols = {f: center(o["bbox"])[0] for f, o in best_row.items()}
    xs = sorted(cols.values())
    gaps = [b - a for a, b in zip(xs, xs[1:])] or [60]
    left, right = xs[0] - gaps[0] * 0.9, xs[-1] + gaps[-1] * 1.2
    head_y = max(o["bbox"][3] for o in best_row.values())
    body = [it for it in items if it["bbox"][1] >= head_y - 1 and left <= center(it["bbox"])[0] <= right
            and it not in best_row.values()]
    body.sort(key=lambda it: center(it["bbox"])[1])
    rows = []
    for it in body:
        cy = center(it["bbox"])[1]
        if rows and abs(rows[-1]["y"] - cy) < height(it) * 0.6:
            rows[-1]["items"].append(it)
        else:
            rows.append({"y": cy, "items": [it]})
    return cols, rows


def grid_rows(items, tc, ctx):
    """罫線で区切った欄を、find_table と同じ形（列の位置と行ごとの項目）にして返す。"""
    cols, _ = find_table(items, tc["headers"])
    if cols is None:
        return None, []
    heads = [it for it in items if any(max(fuzz.ratio(it["text"], lb) for lb in labels) >= 85
                                       for labels in tc["headers"].values())
             and abs(center(it["bbox"])[1] - _head_y(items, tc)) < 8]
    g = ctx.grid_table([h["bbox"] for h in heads]) if heads else None
    if not g:
        return None, []
    xs = g["xs"]
    col_of = {}
    for f, x in cols.items():
        k = next((i for i in range(len(xs) - 1) if xs[i] <= x <= xs[i + 1]), None)
        if k is not None:
            col_of[f] = (xs[k] + xs[k + 1]) / 2
    rows = []
    for row in g["rows"][1:]:
        its = []
        for cellv in row:
            if cellv["text"]:
                its.append({"text": cellv["text"], "bbox": cellv["bbox"], "conf": cellv["conf"],
                            "suspect": cellv.get("suspect"), "agreed": cellv.get("agreed")})
        if its:
            rows.append({"y": center(row[0]["bbox"])[1], "items": its})
    return col_of, rows


def _head_y(items, tc):
    cols, _ = find_table(items, tc["headers"])
    ys = [center(it["bbox"])[1] for it in items
          if any(max(fuzz.ratio(it["text"], lb) for lb in labels) >= 85 for labels in tc["headers"].values())]
    # 見出しの行 = 見出しの言葉が一番多く並んだ高さ
    return max(ys, key=lambda y: sum(1 for v in ys if abs(v - y) < 6)) if ys else 0


def read_table(items, tc, settings=None, ctx=None):
    vocabs = tc.get("vocab") or []
    if isinstance(vocabs, dict):
        vocabs = [vocabs]
    vmap = {v["field"]: [r[v["column"]] for r in settings[v["sheet"]]] for v in vocabs
            if not v.get("tokens")} if settings else {}
    tmap = {}
    for v in vocabs:
        if v.get("tokens") and settings:
            for f in v["fields"]:
                tmap[f] = [r[v["column"]] for r in settings[v["sheet"]]]
    cols, rows = (None, [])
    if ctx is not None:
        cols, rows = grid_rows(items, tc, ctx)
    if cols is None:
        cols, rows = find_table(items, tc["headers"])
    if cols is None:
        return []
    records = []
    pitch = None
    last_y = None
    for r in rows:
        if last_y is not None and pitch and r["y"] - last_y > pitch * 1.8:
            break
        rec_items: dict[str, list] = {}
        for it in sorted(r["items"], key=lambda it: it["bbox"][0]):
            f = min(cols, key=lambda f: abs(cols[f] - center(it["bbox"])[0]))
            # 1つの値が隣の列の真ん中までかかっていたら、2つの欄がつながって読まれた疑い
            if any(g != f and it["bbox"][0] < x < it["bbox"][2] for g, x in cols.items()):
                it = dict(it, suspect="隣の欄とつながっている疑い")
            rec_items.setdefault(f, []).append(it)
        key_items = rec_items.get(tc["key"], [])
        key_ok = key_items and re.fullmatch(tc["checks"].get(tc["key"], ".+"), key_items[0]["text"])
        if not key_ok and len(rec_items) <= len(cols) * 0.6:
            break
        if last_y is not None and pitch is None:
            pitch = r["y"] - last_y
        last_y = r["y"]
        rec = {}
        for f in tc["headers"]:
            its = rec_items.get(f, [])
            if not its:
                rec[f] = cell("", conf=1.0, reasons=["見つかりません"] if f in tc.get("required", []) else None)
                continue
            text = " ".join(i["text"] for i in its)
            conf = min(i["conf"] for i in its)
            reasons = []
            chk = tc.get("checks", {}).get(f)
            if chk and not re.fullmatch(chk, text):
                reasons.append("形が合わない")
            note = None
            if f in tmap:
                fixed, changes = token_fix(text, tmap[f])
                if changes:
                    note = "一覧で読み替え: " + "、".join(f"「{a}」→「{b}」" for a, b, _ in changes)
                    text = fixed
                    low = [sc for _, _, sc in changes if sc < VOCAB_OK]
                    if low:
                        reasons.append(f"一覧で読み替えた（近さ {min(low):.0f}）")
            if f in vmap:
                word, score = vocab_match(text, vmap[f])
                if word != text and score >= 50:
                    note = f"「{text}」を一覧の「{word}」に読み替え"
                    text = word
                    if score < VOCAB_OK:
                        reasons.append(f"一覧で読み替えた（近さ {score:.0f}）")
            box = [min(i["bbox"][0] for i in its), min(i["bbox"][1] for i in its),
                   max(i["bbox"][2] for i in its), max(i["bbox"][3] for i in its)]
            sus = next((i.get("suspect") for i in its if i.get("suspect")), None)
            if note and sus:
                sus = None          # 一覧で読み替えた言葉は、抜けた字も一覧の言葉で埋まっている
            rec[f] = cell(text, {"bbox": box, "conf": conf, "text": " ".join(i["text"] for i in its),
                                 "suspect": sus, "agreed": all(i.get("agreed") or i["conf"] >= CONF_OK for i in its)
                                 and any(i.get("agreed") for i in its)}, reasons=reasons, note=note)
        records.append(rec)
    return records


def table_lookup(items, tc, ctx=None) -> dict[str, dict]:
    """図面の中の表（照明器具表など）を、記号 → 行 の辞書にする。"""
    return {r[tc["key"]]["value"]: {f: c["value"] for f, c in r.items()} for r in read_table(items, tc, ctx=ctx)
            if r[tc["key"]]["value"]}


# ---------------------------------------------------------------- 入口

def map_items(items: list[dict], cfg: dict, settings: dict, ctx=None) -> dict:
    reserved = {lb for f in cfg.get("header", []) for lb in f["labels"]}
    rc = cfg["records"]
    if rc["mode"] == "table":
        reserved |= {lb for labels in rc["headers"].values() for lb in labels}
    header = read_header(items, cfg.get("header", []), reserved)
    if rc["mode"] == "rooms":
        records = read_rooms(items, rc, settings)
    else:
        records = read_table(items, rc, settings, ctx)
    derive(records, rc, items, settings, ctx)
    return {"header": header, "records": records, "columns": rc["columns"]}


def derive(records, rc, items, settings, ctx=None):
    """決まりで埋める欄（区分・消費電力など）と、図面と表の突き合わせ。"""
    for d in rc.get("derive", []):
        if "lookup" in d:
            table = {r[d["key_column"]]: r[d["column"]] for r in settings[d["lookup"]]}
            for rec in records:
                k = rec.get(d["from"], {}).get("value", "")
                v = table.get(k, "")
                rec[d["field"]] = cell(v, conf=1.0, reasons=[] if v or not k else [f"「{k}」が設定の一覧にありません"],
                                       note=f"決まりで補完: 設定.xlsx の一覧で「{k}」は「{v}」", derived=True)
        if "multiply" in d:
            aux = table_lookup(items, rc["aux_tables"][d["table"]], ctx)
            for rec in records:
                sym = rec.get(d["key"], {}).get("value", "")
                qty = rec.get(d["multiply"][0], {}).get("value", "")
                w = aux.get(sym, {}).get(d["multiply"][1], "")
                if sym and qty.isdigit() and w.isdigit():
                    rec[d["field"]] = cell(str(int(qty) * int(w)), conf=1.0,
                                           note=f"決まりで補完: {qty}台 × {w}W（図面の器具表から）", derived=True)
                else:
                    rec[d["field"]] = cell("", reasons=["器具表から計算できません"])
    # 図面の注記にある「特記なき限り◯◯」を、空欄の既定の値として入れる
    for d in rc.get("defaults", []):
        hit = next(((it, m) for it in items for m in [re.search(d["from_note"], it["text"].replace(" ", ""))] if m),
                   None)
        if not hit:
            continue
        it, m = hit
        for rec in records:
            c = rec.get(d["field"])
            if c is not None and not c["value"] and rec.get(rc["anchor"]["field"], {}).get("value"):
                rec[d["field"]] = cell(m.group(1), it, note=f"決まりで補完: 図面の注記「{it['text'].strip()}」から",
                                       derived=True)
                rec[d["field"]]["reasons"] = [r for r in rec[d["field"]]["reasons"] if "不確か" in r]
                rec[d["field"]]["flag"] = bool(rec[d["field"]]["reasons"])
    chk = rc.get("count_on_plan")
    if chk:
        tops = [c["value"] for r in records for f, c in r.items() if f == chk["symbol"]]
        for rec in records:
            sym = rec[chk["symbol"]]["value"]
            qty = rec[chk["count"]]["value"]
            n = sum(1 for it in items if it["text"].replace(" ", "") == sym) - tops.count(sym)
            if sym and qty.isdigit() and n and n != int(qty):
                rec[chk["count"]]["flag"] = True
                rec[chk["count"]]["reasons"].append(f"図面上の記号は {n} 個")
            elif sym and qty.isdigit() and n == int(qty):
                rec[chk["count"]]["note"] = f"図面上の記号 {n} 個と一致"
