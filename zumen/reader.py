"""図面PDFから、文字と位置の一覧を取り出す。

PDFは3つの形に分けて扱う。
  文字あり   PDFの中に文字が文字のまま入っている → そのまま取り出す（読み間違いは起きない）
  文字が線   文字が図形に変わっている           → 画像にして文字認識で読む
  画像       スキャンなどの画像だけのPDF       → 画像にして文字認識で読む

どの形かはPDFの中身を見て自動で決める。
取り出した文字は {text, bbox(pt・左上原点), conf(0〜1), src} の一覧で返す。
"""
from __future__ import annotations

import os
import time
import unicodedata

import cv2
import numpy as np
import pdfplumber
import pypdfium2 as pdfium

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

KIND_LABEL = {"text": "文字あり", "outline": "文字が線", "scan": "画像"}
OCR_DPI = 300


# 文字認識の部品は中国語の字も覚えているので、日本の図面に出てくる字の中国の字体を日本の字体に戻す
HANZI = str.maketrans("壳换纳收广气电设备图场务处诊库门户监龙压灯线机钢铁层楼间厨卫阳窗关调风热净给钉实",
                      "売換納収広気電設備図場務処診庫門戸監竜圧灯線機鋼鉄層楼間厨衛陽窓関調風熱浄給釘実")


def norm(s: str) -> str:
    """全角・半角のゆれをそろえる（ＣＨ＝２４００ → CH=2400）。"""
    s = unicodedata.normalize("NFKC", s).translate(HANZI)
    return s.replace("✕", "×").replace("·", "・").replace("（", "(").replace("）", ")").strip()


def is_wide(c: str) -> bool:
    return unicodedata.east_asian_width(c) in "WFA"      # × や φ も図面の字体では全角の幅


def units(text: str) -> float:
    """文字列の幅を「全角1文字=1」で数える。"""
    return sum(1.0 if is_wide(c) else 0.55 for c in text)


def ink_box(crop: np.ndarray):
    """切り出した画像の中で、黒い部分がある範囲（x0, y0, x1, y1）。無ければ None。"""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
    ys, xs = np.where(gray < 140)
    if len(xs) < 12:
        return None
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def looks_short(text: str, crop: np.ndarray, vertical: bool = False) -> bool:
    """読めた文字の数に対して、黒い部分が横に長すぎる = 字が抜けている疑い。"""
    if len(text) < 2 or vertical:
        return False
    b = ink_box(crop)
    if b is None:
        return False
    w, h = b[2] - b[0], b[3] - b[1]
    # 字の背の高さから1文字の大きさを出す。漢字は背が高く、かなと英数字は低い
    kanji = any("\u4e00" <= c <= "\u9fff" for c in text)
    kana = any("\u3040" <= c <= "\u30ff" for c in text)
    em = h / (0.9 if kanji else 0.8 if kana else 0.72)
    # 語と語の間の空白（読めた文字に空白が無くても、黒い部分には隙間がある）
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY) if crop.ndim == 3 else crop
    ink = (gray[b[1]:b[3], b[0]:b[2]] < 140).any(axis=0)
    gaps, run = 0, 0
    for on in ink:
        if not on:
            run += 1
        else:
            if run > em * 0.45:
                gaps += 1
            run = 0
    extra = max(gaps - text.count(" "), 0) * 0.55
    wide = sum(1.0 for c in text if is_wide(c))
    frac = wide / max(units(text), 0.01)
    normal = 0.91 * frac + 0.74 * (1 - frac)          # 正しく読めた字の幅（見本の図面で測った値）
    return w / ((units(text) + extra) * em) > normal * 1.12


# ---------------------------------------------------------------- PDFの形

def classify(page) -> str:
    """page は pdfplumber のページ。"""
    if sum(1 for c in page.chars if c["text"].strip()) >= 20:
        return "text"
    area = page.width * page.height
    covered = 0.0
    for im in page.images:
        w = max(0.0, min(im["x1"], page.width) - max(im["x0"], 0))
        h = max(0.0, min(im["bottom"], page.height) - max(im["top"], 0))
        covered += w * h
    return "scan" if covered > area * 0.5 else "outline"


def render(path: str, dpi: int) -> np.ndarray:
    """1ページ目を RGB の画像にする。"""
    pdf = pdfium.PdfDocument(path)
    try:
        pil = pdf[0].render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        pdf.close()
    return np.asarray(pil).copy()


# ---------------------------------------------------------------- 文字ありPDF

def _lines(page) -> list[dict]:
    """pdfplumber の1文字ずつの情報を、行（向きのそろった文字の並び）にまとめる。"""
    chars = [{"c": c["text"], "bbox": (c["x0"], c["top"], c["x1"], c["bottom"]),
              "up": c["upright"], "m": c.get("matrix") or (1, 0, 0, 1, 0, 0)} for c in page.chars]
    lines = []
    up = sorted([c for c in chars if c["up"]], key=lambda c: (c["bbox"][3], c["bbox"][0]))
    for c in up:
        h = c["bbox"][3] - c["bbox"][1]
        ln = next((l for l in lines if l["dir"] == (1, 0) and abs(l["base"] - c["bbox"][3]) < h * 0.3), None)
        if ln is None:
            ln = {"dir": (1, 0), "base": c["bbox"][3], "chars": []}
            lines.append(ln)
        ln["chars"].append(c)
    rot = [c for c in chars if not c["up"]]
    for c in sorted(rot, key=lambda c: c["bbox"][0]):
        w = c["bbox"][2] - c["bbox"][0]
        ln = next((l for l in lines if l["dir"] != (1, 0) and abs(l["base"] - c["bbox"][0]) < w * 0.3), None)
        if ln is None:
            # 行列の2番目が正なら左に90度回した文字（下から上へ読む）
            ln = {"dir": (0, -1) if c["m"][1] > 0 else (0, 1), "base": c["bbox"][0], "chars": []}
            lines.append(ln)
        ln["chars"].append(c)
    for ln in lines:
        if ln["dir"] == (1, 0):
            ln["chars"].sort(key=lambda c: c["bbox"][0])
        else:
            ln["chars"].sort(key=lambda c: c["bbox"][1], reverse=ln["dir"] == (0, -1))
    return lines


def text_items(page) -> list[dict]:
    items = []
    # 縦の線（表の罫線など）。線をまたぐ文字は別の欄なので、そこで切る
    vlines = [(e["x0"], e["top"], e["bottom"]) for e in page.edges
              if e.get("orientation") == "v" and e["bottom"] - e["top"] > 3]

    def crossed(a, b) -> bool:
        lo, hi = min(a["bbox"][2], b["bbox"][0]) - 0.3, max(a["bbox"][2], b["bbox"][0]) + 0.3
        mid = (a["bbox"][1] + a["bbox"][3]) / 2
        return any(lo <= x <= hi and top <= mid <= bot for x, top, bot in vlines)

    for line in _lines(page):
        if True:
            chars = line["chars"]
            s = "".join(c["c"] for c in chars)
            if not s.strip():
                continue
            # 同じ行の中で大きく離れた文字は別の項目に分ける（表の隣の欄など）
            parts, cur = [], [chars[0]]
            for a, b in zip(chars, chars[1:]):
                if line["dir"][0]:
                    h = max(a["bbox"][3] - a["bbox"][1], 1)
                    gap = max(b["bbox"][0] - a["bbox"][2], a["bbox"][0] - b["bbox"][2], 0)
                else:                              # 90度回した文字は縦方向の隙間を見る
                    h = max(a["bbox"][2] - a["bbox"][0], 1)
                    gap = max(b["bbox"][1] - a["bbox"][3], a["bbox"][1] - b["bbox"][3], 0)
                if gap > h * 1.6 or (line["dir"][0] and crossed(a, b)):
                    parts.append(cur)
                    cur = []
                cur.append(b)
            parts.append(cur)
            for p in parts:
                t = "".join(c["c"] for c in p).strip()
                if not t:
                    continue
                x0 = min(c["bbox"][0] for c in p)
                y0 = min(c["bbox"][1] for c in p)
                x1 = max(c["bbox"][2] for c in p)
                y1 = max(c["bbox"][3] for c in p)
                it = {"text": norm(t), "bbox": [x0, y0, x1, y1], "conf": 1.0, "src": "text",
                      "rotated": not line["dir"][0]}
                if "(cid:" in t or any("\ue000" <= ch <= "\uf8ff" or ch == "\ufffd" for ch in t):
                    it["suspect"] = "文字化けの疑い"
                items.append(it)
    return _merge_vertical(items)


def _merge_horizontal(items: list[dict]) -> list[dict]:
    """字の間が空いていて別々に読まれた文字（「日」「付」）を1つにまとめる。"""
    items = sorted(items, key=lambda i: i["bbox"][0])
    out = []
    for it in items:
        h = it["bbox"][3] - it["bbox"][1]
        prev = next((o for o in reversed(out) if not o.get("vertical") and not it.get("vertical")
                     and len(o["text"]) <= 2 and len(it["text"]) <= 2
                     and abs((o["bbox"][1] + o["bbox"][3]) / 2 - (it["bbox"][1] + it["bbox"][3]) / 2) < h * 0.3
                     and 0.7 < (o["bbox"][3] - o["bbox"][1]) / max(h, 1) < 1.4
                     and -h * 0.2 <= it["bbox"][0] - o["bbox"][2] < h * 0.5), None)
        if prev is None:
            out.append(it)
            continue
        prev["text"] += it["text"]
        prev["bbox"] = [prev["bbox"][0], min(prev["bbox"][1], it["bbox"][1]), it["bbox"][2],
                        max(prev["bbox"][3], it["bbox"][3])]
        prev["conf"] = min(prev["conf"], it["conf"])
    return out


def _merge_vertical(items: list[dict]) -> list[dict]:
    """縦書き（1文字ずつ縦に並んだ文字）を1つの項目にまとめる。"""
    singles = sorted([i for i in items if len(i["text"]) == 1 and not i.get("rotated")], key=lambda i: i["bbox"][1])
    used = set()
    merged = []
    for a in singles:
        if id(a) in used:
            continue
        run = [a]
        while True:
            last = run[-1]
            h = last["bbox"][3] - last["bbox"][1]
            cx = (last["bbox"][0] + last["bbox"][2]) / 2
            nxt = [b for b in singles if id(b) not in used and b is not last and b not in run
                   and abs((b["bbox"][0] + b["bbox"][2]) / 2 - cx) < h * 0.5
                   and -h * 0.2 <= b["bbox"][1] - last["bbox"][3] < h * 0.9]
            if not nxt:
                break
            run.append(min(nxt, key=lambda b: b["bbox"][1]))
        if len(run) > 1:
            for r in run:
                used.add(id(r))
            merged.append({"text": "".join(r["text"] for r in run),
                           "bbox": [min(r["bbox"][0] for r in run), run[0]["bbox"][1],
                                    max(r["bbox"][2] for r in run), run[-1]["bbox"][3]],
                           "conf": round(min(r["conf"] for r in run), 3), "src": run[0]["src"], "vertical": True})
    return [i for i in items if id(i) not in used] + merged


# ---------------------------------------------------------------- 文字認識

class OCR:
    """PaddleOCR（PP-OCRv5 の軽い版）。PCの中だけで動く。最初の1回だけ部品を読み込む。"""

    def __init__(self):
        from paddleocr import TextDetection, TextRecognition
        self.det = TextDetection(model_name="PP-OCRv5_mobile_det", enable_mkldnn=False)
        self.rec = TextRecognition(model_name="PP-OCRv5_mobile_rec", enable_mkldnn=False)

    def recognize(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        if not crops:
            return []
        # 小さい文字は拡大してから読む（見本の図面で、2倍にすると抜ける字が減った）
        big = [cv2.resize(c, None, fx=64 / c.shape[0], fy=64 / c.shape[0], interpolation=cv2.INTER_CUBIC)
               if 8 < c.shape[0] < 64 else c for c in crops]
        return [(o["rec_text"], float(o["rec_score"])) for o in self.rec.predict(big)]

    def confirm(self, best, origs, cleans, lo=0.6, hi=0.9) -> set[int]:
        """確かさが lo〜hi の読みを、別の切り出し（線を消す前の画像を2倍・線を消した画像を3倍）で
        読み直す。3回とも同じ文字なら確かさを 0.92 に上げ、その番号を返す。"""
        idx = [k for k, (t, s) in enumerate(best) if lo <= s < hi and t.strip()]
        if not idx:
            return set()
        v = []
        for k in idx:
            for c, f in ((origs[k], 2.0), (cleans[k], 3.0)):
                v.append(np.ascontiguousarray(cv2.resize(c, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)))
        reads = self.recognize(v)
        done = set()
        for n, k in enumerate(idx):
            a, b = norm(reads[2 * n][0]), norm(reads[2 * n + 1][0])
            if a == b == norm(best[k][0]):
                best[k] = (best[k][0], max(best[k][1], 0.92))
                done.add(k)
        return done

    def read(self, path: str) -> tuple[list[dict], "PageCtx"]:
        img = render(path, OCR_DPI)
        img, self.angle = deskew(img)
        clean = remove_lines(img)
        polys = list(self.det.predict(clean, limit_side_len=4000, limit_type="max"))[0]["dt_polys"]
        boxes = []
        for p in polys:
            x0, y0 = np.min(p, 0).astype(int)
            x1, y1 = np.max(p, 0).astype(int)
            if (x1 - x0) * (y1 - y0) > img.shape[0] * img.shape[1] * 0.05:   # 図面全体を囲む誤検出
                continue
            boxes.append((max(x0 - 3, 0), max(y0 - 3, 0), x1 + 3, y1 + 3))
        crops = [clean[y0:y1, x0:x1] for x0, y0, x1, y1 in boxes]
        first = self.recognize(crops)
        # 縦長の枠は、縦書きか回した文字。回して読む・1字ずつ横に並べ直して読むを試し、確かなほうを採る
        tall = [k for k, (x0, y0, x1, y1) in enumerate(boxes) if (y1 - y0) > (x1 - x0) * 1.4]
        alt_crops, owners = [], []
        for k in tall:
            c = crops[k]
            for v in (np.rot90(c, -1), np.rot90(c, 1), restack(c)):
                alt_crops.append(np.ascontiguousarray(v))
                owners.append(k)
        # 線が重なっていた文字は、線を消す前の画像でも読み、回しても読んで、確かなほうを採る
        weak = [k for k, (t, s) in enumerate(first) if s < 0.85 and k not in tall]
        for k in weak:
            x0, y0, x1, y1 = boxes[k]
            for v in (img[y0:y1, x0:x1], np.rot90(crops[k], -1), np.rot90(crops[k], 1)):
                alt_crops.append(np.ascontiguousarray(v))
                owners.append(k)
        best = list(first)
        for k, (t, s) in zip(owners, self.recognize(alt_crops)):
            if s > best[k][1] and t.strip():
                best[k] = (t, s)
        # それでも確かでない枠は、縦に並んだ別々の文字が1つにまとまっていることがある。
        # 縦の隙間で切り分け、切った列ごとに回して読む（例: 90度回した「WIC」と「3帖」）
        extra = []
        for k in [k for k, (t, s) in enumerate(best) if s < 0.85]:
            x0, y0, x1, y1 = boxes[k]
            cols = split_columns(crops[k])
            if len(cols) < 2:
                continue
            reads = []
            for a, b in cols:
                c = crops[k][:, a:b]
                r = self.recognize([np.ascontiguousarray(np.rot90(c, -1)), np.ascontiguousarray(np.rot90(c, 1))])
                reads.append(max(r, key=lambda v: v[1]))
            if min(s for _, s in reads) > best[k][1]:
                best[k] = ("", 0.0)
                extra += [((x0 + a, y0, x0 + b, y1), r) for (a, b), r in zip(cols, reads)]
        boxes = boxes + [b for b, _ in extra]
        best = best + [r for _, r in extra]
        agreed = self.confirm(best, [img[y0:y1, x0:x1] for x0, y0, x1, y1 in boxes],
                              [clean[y0:y1, x0:x1] for x0, y0, x1, y1 in boxes])
        scale = 72 / OCR_DPI
        items = []
        for k, ((x0, y0, x1, y1), (t, s)) in enumerate(zip(boxes, best)):
            t = norm(t)
            if not t or s < 0.3:
                continue
            vert = len(t) >= 2 and (y1 - y0) > (x1 - x0) * 1.4      # 1文字の枠は縦長でも縦書きではない
            it = {"text": t, "bbox": [x0 * scale, y0 * scale, x1 * scale, y1 * scale],
                  "conf": round(s, 3), "src": "ocr", "vertical": vert}
            if k in agreed:
                it["agreed"] = True
            if looks_short(t, clean[y0:y1, x0:x1], vert):
                it["suspect"] = "字が抜けている疑い"
            items.append(it)
        return _merge_vertical(_merge_horizontal(items)), PageCtx(self, img, clean)


class PageCtx:
    """文字認識で読んだページの画像。表を罫線で区切って読み直すときに使う。"""

    def __init__(self, ocr: OCR, img: np.ndarray, clean: np.ndarray):
        self.ocr, self.img, self.clean = ocr, img, clean
        self.hmask, self.vmask = line_masks(img)
        self.s = OCR_DPI / 72

    def grid_table(self, head_boxes: list[list[float]]):
        """見出しの枠（pt）から表の罫線を探し、欄ごとに切り出して読む。
        返すのは {"xs": 縦線の位置(pt), "rows": [[{text, conf, bbox, suspect}, ...], ...]}。
        rows[0] は見出しの行。"""
        s = self.s
        hx0 = int(min(b[0] for b in head_boxes) * s)
        hx1 = int(max(b[2] for b in head_boxes) * s)
        hy = int(sum((b[1] + b[3]) / 2 for b in head_boxes) / len(head_boxes) * s)
        H, W = self.vmask.shape
        left = max(hx0 - 200, 0)
        # 見出しの行を縦に貫く線 = 欄の区切り
        band = self.vmask[max(hy - 8, 0):hy + 8, left:min(hx1 + 200, W)] > 0
        xs = _cluster(np.where(band.sum(0) >= band.shape[0] * 0.8)[0] + left)
        if len(xs) < 3:
            return None
        # 表の幅いっぱいに引かれた横線 = 行の区切り
        hband = self.hmask[:, xs[0] + 3:xs[-1] - 3] > 0
        rows_y = _cluster(np.where(hband.sum(1) >= hband.shape[1] * 0.9)[0])
        above = [y for y in rows_y if y < hy]
        below = [y for y in rows_y if y > hy]
        if not above or not below:
            return None
        ys = [above[-1]] + below
        head_h = ys[1] - ys[0]
        out_rows, crops, where = [], [], []
        for r, (y0, y1) in enumerate(zip(ys, ys[1:])):
            if r and (y1 - y0 > head_h * 2.2 or not self._has_cols(xs, y0, y1)):
                break
            row = []
            for c, (x0, x1) in enumerate(zip(xs, xs[1:])):
                crop = self.img[y0 + 5:y1 - 4, x0 + 5:x1 - 4]
                cell = {"text": "", "conf": 1.0, "bbox": [x0 / s, y0 / s, x1 / s, y1 / s]}
                ib = ink_box(crop)
                if ib is not None:
                    a0, b0, a1, b1 = ib
                    tight = crop[max(b0 - 4, 0):b1 + 4, max(a0 - 6, 0):a1 + 6]
                    crops.append(np.ascontiguousarray(tight))
                    where.append((r, c))
                    cell["tight"] = tight
                row.append(cell)
            out_rows.append(row)
        reads = self.ocr.recognize(crops)
        pads = []
        for (r, c) in where:
            x0, y0, x1, y1 = [int(v * s) for v in out_rows[r][c]["bbox"]]
            pads.append(self.clean[y0 + 5:y1 - 4, x0 + 5:x1 - 4])
        agreed = self.ocr.confirm(reads, crops, pads)
        for n, ((r, c), (t, conf)) in enumerate(zip(where, reads)):
            cell = out_rows[r][c]
            cell["text"], cell["conf"] = norm(t), round(conf, 3)
            if n in agreed:
                cell["agreed"] = True
            if not cell["text"]:
                cell["conf"] = 0.0
            if looks_short(cell["text"], cell["tight"]):
                cell["suspect"] = "字が抜けている疑い"
        for row in out_rows:
            for cell in row:
                cell.pop("tight", None)
        return {"xs": [x / s for x in xs], "rows": out_rows}

    def _has_cols(self, xs, y0, y1) -> bool:
        """その行にも同じ位置の縦線があるか（無ければ表の外）。"""
        inner = xs[1:-1]
        seg = self.vmask[y0 + 4:y1 - 4] > 0
        hit = sum(1 for x in inner if seg[:, max(x - 3, 0):x + 4].any(axis=1).mean() > 0.8)
        return hit >= max(1, len(inner) * 0.8)


def deskew(img: np.ndarray):
    """スキャンの傾きを直す。横線と文字の行が水平になる角度を ±1.5度 の中で探す。"""
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    h, w = bw.shape
    best, score = 0.0, -1.0
    for a in np.arange(-1.5, 1.51, 0.05):
        m = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0)
        r = cv2.warpAffine(bw, m, (w, h), flags=cv2.INTER_NEAREST)
        v = float(np.var(r.sum(axis=1)))
        if v > score:
            best, score = float(a), v
    if abs(best) < 0.05:
        return img, 0.0
    m = cv2.getRotationMatrix2D((img.shape[1] / 2, img.shape[0] / 2), best, 1.0)
    out = cv2.warpAffine(img, m, (img.shape[1], img.shape[0]), flags=cv2.INTER_CUBIC,
                         borderValue=(255, 255, 255))
    return out, round(best, 2)


def _cluster(pos) -> list[int]:
    out, run = [], []
    for p in pos:
        if run and p - run[-1] > 3:
            out.append(int(np.mean(run)))
            run = []
        run.append(p)
    if run:
        out.append(int(np.mean(run)))
    return out


def line_masks(img: np.ndarray):
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    n = int(OCR_DPI * 0.28)     # 約7mm より長い線
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (n, 1)))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, n)))
    return h, v


def remove_lines(img: np.ndarray) -> np.ndarray:
    """長い縦線・横線（壁・表の罫線・通り芯）を消す。文字の画より長い線だけを対象にする。"""
    h, v = line_masks(img)
    mask = cv2.dilate(cv2.bitwise_or(h, v), np.ones((3, 3), np.uint8))
    out = img.copy()
    out[mask > 0] = 255
    return out


def split_columns(crop: np.ndarray) -> list[tuple[int, int]]:
    """枠の中の文字の列を、縦の白い隙間で切り分ける。"""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    ink = (gray < 128).sum(axis=0) > 0
    cols, start, blank = [], None, 0
    for x, on in enumerate(ink):
        if on:
            if start is None:
                start = x
            blank = 0
        elif start is not None:
            blank += 1
            if blank >= 6:
                cols.append((start, x - blank + 1))
                start, blank = None, 0
    if start is not None:
        cols.append((start, len(ink)))
    return [(max(a - 3, 0), min(b + 3, len(ink))) for a, b in cols if b - a >= 8]


def restack(crop: np.ndarray) -> np.ndarray:
    """縦に並んだ文字を1字ずつ切り、横に並べ直す。"""
    h, w = crop.shape[:2]
    n = max(1, round(h / max(w * 0.95, 1)))
    step = h / n
    parts = [crop[int(k * step):int((k + 1) * step)] for k in range(n)]
    size = max(p.shape[0] for p in parts)
    parts = [cv2.copyMakeBorder(p, 0, size - p.shape[0], 4, 4, cv2.BORDER_CONSTANT, value=(255, 255, 255))
             for p in parts]
    return np.hstack(parts)


# ---------------------------------------------------------------- 入口

_OCR: OCR | None = None


def get_ocr() -> OCR:
    global _OCR
    if _OCR is None:
        _OCR = OCR()
    return _OCR


def read_pdf(path: str) -> dict:
    """1ページ目を読む。形・文字の一覧・かかった秒数・ページの大きさを返す。"""
    t = time.time()
    with pdfplumber.open(path) as pdf:
        page = pdf.pages[0]
        kind = classify(page)
        size = [float(page.width), float(page.height)]
        items = text_items(page) if kind == "text" else None
    ctx = None
    if items is None:
        items, ctx = get_ocr().read(path)
    return {"kind": kind, "kind_label": KIND_LABEL[kind], "items": items, "ctx": ctx,
            "page": size, "seconds": round(time.time() - t, 1)}
