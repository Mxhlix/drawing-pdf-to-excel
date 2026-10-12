"""文字ありPDF（文字が文字のまま入ったPDF）の読み取りを、正解と突き合わせて確かめる。

文字認識を使わないので、数秒で終わる。CI でも毎回動かす。
"""
import pdfplumber
import pytest

from evaluate import HERE, KEYS, evaluate
from zumen.mapper import load_configs, load_settings
from zumen.reader import classify


@pytest.fixture(scope="module")
def env():
    return load_configs(), load_settings()


@pytest.mark.parametrize("key", KEYS)
def test_text_pdf_matches_truth(key, env):
    configs, settings = env
    e = evaluate(key, "text", configs, settings)
    assert e["detected"] == "text"
    assert e["picked"] == key, "様式の自動判定が違う"
    c = e["counts"]
    assert e["total"] > 0
    # 文字ありPDFは読み間違いが起きないので、全欄が正しく、確かめの印も付かないこと
    assert c["正しく入った"] == e["total"], e["problems"]
    assert e["problems"] == []


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("kind", ["text", "outline", "scan"])
def test_pdf_kind_is_detected(key, kind):
    with pdfplumber.open(HERE / "samples" / f"{key}_{kind}.pdf") as pdf:
        assert classify(pdf.pages[0]) == kind
