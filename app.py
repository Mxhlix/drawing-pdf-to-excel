"""図面PDF → Excel 転記ツール（画面）。

PCの中だけで動く。起動すると http://127.0.0.1:8961 で画面が開く。
図面PDFを開く → 読んだ値を欄に並べる → 色の付いた欄を図面と見比べて直す → Excel に書き出す。

  $ python app.py
"""
from __future__ import annotations

import io
import uuid
from pathlib import Path

import cv2
import openpyxl
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from zumen.excel_out import write
from zumen.mapper import load_configs, load_settings, map_items, pick_config
from zumen.reader import get_ocr, read_pdf, render

HERE = Path(__file__).parent
UPLOAD = HERE / "work" / "uploads"
UPLOAD.mkdir(parents=True, exist_ok=True)
app = FastAPI()
SESS: dict[str, dict] = {}
VIEW_DPI = 110


def page_png(sess) -> bytes:
    """画面に出す図面の画像。文字認識で読んだページは、読んだときの画像（傾きを直したもの）を使う。"""
    if sess["ctx"] is not None:
        img = sess["ctx"].img
        k = VIEW_DPI / 300
        small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".png", cv2.cvtColor(small, cv2.COLOR_RGB2BGR))
        return buf.tobytes()
    ok, buf = cv2.imencode(".png", cv2.cvtColor(render(sess["path"], VIEW_DPI), cv2.COLOR_RGB2BGR))
    return buf.tobytes()


def payload(sid: str) -> dict:
    s = SESS[sid]
    cfg = s["configs"][s["key"]]
    res = map_items(s["items"], cfg, load_settings(), s["ctx"])
    s["result"] = res
    return {"id": sid, "name": s["name"], "kind": s["kind"], "kind_label": s["kind_label"],
            "seconds": s["seconds"], "key": s["key"], "page": s["page"],
            "header_fields": [f["field"] for f in cfg["header"]],
            "header": res["header"], "columns": res["columns"], "records": res["records"]}


@app.get("/api/configs")
def configs():
    return [{"key": k, "name": c["name"]} for k, c in load_configs().items()]


@app.post("/api/read")
async def read(file: UploadFile = File(...)):
    sid = uuid.uuid4().hex[:10]
    path = UPLOAD / f"{sid}.pdf"
    path.write_bytes(await file.read())
    r = read_pdf(str(path))
    cfgs = load_configs()
    SESS[sid] = {"path": str(path), "name": file.filename, "items": r["items"], "ctx": r["ctx"],
                 "kind": r["kind"], "kind_label": r["kind_label"], "seconds": r["seconds"],
                 "page": r["page"], "configs": cfgs, "key": pick_config(r["items"], cfgs)}
    return payload(sid)


class Remap(BaseModel):
    key: str


@app.post("/api/remap/{sid}")
def remap(sid: str, body: Remap):
    SESS[sid]["key"] = body.key
    return payload(sid)


@app.get("/api/page/{sid}.png")
def page(sid: str):
    return Response(page_png(SESS[sid]), media_type="image/png")


class Export(BaseModel):
    header: dict
    records: list


@app.post("/api/export/{sid}")
def export(sid: str, body: Export):
    s = SESS[sid]
    cfg = s["configs"][s["key"]]
    out = write({"header": body.header, "records": body.records}, cfg, s["name"])
    # 書き出したファイルを読み直して、画面に中身を出す
    ws = openpyxl.load_workbook(out)[cfg["sheet"]]
    rows = []
    for row in ws.iter_rows(min_row=1, max_row=cfg["start_row"] + len(body.records) - 1):
        rows.append([{"v": "" if c.value is None else str(c.value),
                      "y": bool(c.fill and c.fill.fgColor and c.fill.fgColor.rgb in ("00FFF2A8", "FFFFF2A8")),
                      "h": bool(c.fill and c.fill.fgColor and c.fill.fgColor.rgb in ("00DDE5EE", "FFDDE5EE")),
                      "b": bool(c.font and c.font.bold)} for c in row[:len(cfg["records"]["columns"]) + 3]])
    return {"file": out.name, "folder": str(out.parent), "sheet": cfg["sheet"], "rows": rows}


@app.get("/api/download/{name}")
def download(name: str):
    # 書き出したフォルダの中のファイルだけを返す（..\ などで外のファイルを取り出せないようにする）
    out_dir = (HERE / "output").resolve()
    path = (out_dir / name).resolve()
    if path.parent != out_dir or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=path.name)


app.mount("/samples", StaticFiles(directory=HERE / "samples"), name="samples")   # 見本の図面（試す用）
app.mount("/", StaticFiles(directory=HERE / "static", html=True), name="static")

if __name__ == "__main__":
    import os
    import threading
    import webbrowser
    get_ocr()          # 文字認識の部品を先に読み込んでおく
    if os.environ.get("ZUMEN_NO_BROWSER") != "1":   # ZUMEN_NO_BROWSER=1 のときはブラウザを開かない
        threading.Timer(1.5, webbrowser.open, ["http://127.0.0.1:8961"]).start()
    uvicorn.run(app, host="127.0.0.1", port=8961, log_level="warning")
