"""文字が線のPDF・画像のPDFを文字認識で読み、正解と突き合わせる。

PaddleOCR が要る。1枚15〜25秒ほどかかるので、ふだんの pytest では動かさない。
  $ pip install -r requirements-ocr.txt
  $ pytest -m ocr -s
"""
import pytest

from evaluate import KEYS, evaluate
from zumen.mapper import load_configs, load_settings

pytest.importorskip("paddleocr")
pytestmark = pytest.mark.ocr


@pytest.fixture(scope="module")
def env():
    return load_configs(), load_settings()


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("kind", ["outline", "scan"])
def test_ocr_has_no_silent_errors(key, kind, env):
    configs, settings = env
    e = evaluate(key, kind, configs, settings)
    c = e["counts"]
    print(f"\n{key} {kind} {e['seconds']}秒 {c}")
    for p in e["problems"]:
        print("   ", p)
    assert e["detected"] == kind
    assert e["picked"] == key, "様式の自動判定が違う"
    # 読み違えた欄は、必ず「確かめてほしい」印が付いていること（印なしの誤りは見落としにつながる）
    assert c["色なしで違う"] == 0, e["problems"]
