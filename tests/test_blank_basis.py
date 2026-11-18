import csv, io
from decimal import Decimal
import pytest
from app.pipeline import run
from app import store
from app.exporter import evidence_csv

FIELDS = ["sample_id","kind","analyte","area","internal_area","level","dilution","unit","batch"]


def row(*cells):
    return dict(zip(FIELDS, cells))


def text(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# 标曲响应比 1,3,5 -> 斜率2、截距1；空白值 = (响应比-1)/2。
CURVE = [
    row("C0", "standard", "Pb", "100", "100", "0", "1", "ug/L", "B01"),
    row("C1", "standard", "Pb", "300", "100", "1", "1", "ug/L", "B01"),
    row("C2", "standard", "Pb", "500", "100", "2", "1", "ug/L", "B01"),
    row("C3", "standard", "Pb", "900", "100", "4", "1", "ug/L", "B01"),
    row("C4", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
]


def make(*rows):
    return run(text(CURVE + list(rows)))


def sample(name, area, dilution="1"):
    return row(name, "sample", "Pb", area, "100", "0", dilution, "ug/L", "B01")


def blank(name, area):
    return row(name, "blank", "Pb", area, "100", "0", "1", "ug/L", "B01")


def test_carryover_blank_is_attributed_to_high_standard():
    r = make(
        blank("B0", "120"),          # 0.1
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "220"),          # 0.6，紧跟最高标样
        blank("B2", "120"),          # 0.1
        sample("S001", "122"),       # 原始值 0.1
    )
    blanks = {b["sample_id"]: b for b in r["blank_report"]}
    assert blanks["B1"]["after_high_standard"] is True
    assert blanks["B1"]["blank_class"] == "CARRYOVER"
    assert blanks["B0"]["blank_class"] == "CLEAN"
    assert r["blank_blocks"] == []


def test_unknown_high_blank_is_isolated_and_blocks_batch():
    r = make(
        blank("B0", "120"),
        blank("B1", "320"),          # 0.6 无高标样前因
        blank("B2", "120"),
        sample("S001", "122"),
    )
    assert any(x["reason"] == "BLANK_UNKNOWN_HIGH" for x in r["quarantine"])
    assert r["blocked"] is True
    assert r["blank_blocks"][0]["sample_id"] == "B1"


def test_low_concentration_uses_adjacent_blank():
    r = make(
        blank("B0", "120"),          # 0.1
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "220"),          # 0.6 归因残留，不进批中位数
        blank("B2", "110"),          # 0.05 紧邻低浓度样品
        sample("S001", "122"),       # 原始 0.1，低于最低标样 1
    )
    result = next(x for x in r["final"] if x["sample_id"] == "S001")
    assert result["low_concentration"] is True
    assert result["blank_basis"] == "adjacent"
    assert result["adjacent_blank_id"] == "B2"
    assert Decimal(result["net"]) == Decimal("0.06")
    assert Decimal(result["concentration"]) == Decimal("0.06")
    # 批中位数会把它判成未检出，相邻空白改判检出——这正是要纠正的误判。
    assert result["batch_conclusion"] == "未检出"
    assert result["conclusion"] == "检出"


def test_batch_blank_median_excludes_carryover():
    r = make(
        blank("B0", "120"),          # 0.1
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "220"),          # 0.6 残留
        blank("B2", "120"),          # 0.1
        sample("S002", "520"),       # 高于最低标样
    )
    assert Decimal(r["blanks"]["('B01', 'Pb')"]) == Decimal("0.1")


def test_high_concentration_sample_keeps_batch_blank():
    r = make(
        blank("B0", "120"),
        blank("B1", "160"),
        sample("S001", "520"),       # 原始 2.0 > 最低标样 1
    )
    result = next(x for x in r["final"] if x["sample_id"] == "S001")
    assert result["low_concentration"] is False
    assert result["blank_basis"] == "batch"


def test_switch_failure_keeps_original_conclusion():
    # 相邻空白本身来源不明、被隔离 -> 低浓度样品不得切换，沿用批中位数。
    r = make(
        blank("B0", "120"),
        blank("B1", "120"),
        blank("B2", "320"),          # 来源不明的偏高空白，紧邻低浓度样品
        sample("S001", "122"),
    )
    result = next(x for x in r["final"] if x["sample_id"] == "S001")
    assert r["blocked"] is True       # 来源不明空白仍阻断整批放行
    assert result["switch_status"] == "KEPT_ORIGINAL"
    assert result["switch_reason"] == "ADJACENT_BLANK_ISOLATED"
    assert result["blank_basis"] == "batch"
    assert result["conclusion_basis"] == "batch"


def test_adjacent_blank_without_batch_baseline():
    # 只有残留空白与一个来源不明空白：不能形成批基线，一并隔离。
    r = make(
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "220"),
        blank("B2", "320"),
        sample("S001", "122"),
    )
    assert r["blocked"] is True
    assert any(x["reason"] == "BLANK_UNKNOWN_HIGH" for x in r["quarantine"])


def test_single_final_conclusion_per_sample():
    text_value = text(CURVE + [
        blank("B0", "120"),
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "220"),
        blank("B2", "100"),
        sample("S001", "122"),
        sample("S002", "520"),
    ])
    item = store.save(text_value, run(text_value))
    out = evidence_csv(item)
    rows = list(csv.DictReader(io.StringIO(out.lstrip("﻿"))))
    receipt = [x for x in rows if x["kind"] == "result"]
    ids = [x["sample_id"] for x in receipt]
    assert ids == ["S001", "S002"]
    for entry in receipt:
        assert entry["conclusion"] in {"检出", "未检出", "超范围需复核", "质控失败"}
        assert entry["evidence"]


def test_amendments_idempotent_across_rerun():
    text_value = text(CURVE + [
        blank("B0", "120"),
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "220"),
        blank("B2", "100"),
        sample("S001", "122"),
    ])
    a = store.save(text_value, run(text_value))
    b = store.save(text_value, run(text_value))
    assert a["id"] == b["id"]
    assert len(a["amendments"]) == len(b["amendments"]) == 1
    assert a["amendments"][0]["payload"]["sample_id"] == "S001"


def test_normal_batch_variation_not_flagged_as_unknown():
    # 两个空白点，MAD 退化；0.15 与 0.1 的差异属正常波动，不应隔离。
    r = make(
        blank("B0", "120"),          # 0.1
        blank("B1", "130"),          # 0.15
        sample("S001", "520"),
    )
    assert r["blank_blocks"] == []
    assert r["blocked"] is False
    assert {b["blank_class"] for b in r["blank_report"]} == {"CLEAN"}


def test_carryover_only_batch_baseline_is_degraded():
    r = make(
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "220"),          # 0.6，唯一空白且紧跟高标样
        sample("S001", "520"),
    )
    assert r["blocked"] is False
    assert r["blank_report"][0]["blank_class"] == "CARRYOVER"
    assert r["blank_report"][0]["baseline_source"] == "CARRYOVER_ONLY"


def test_adjacent_carryover_blank_limits_inflated_concentration():
    # 高标样带出后：后段本底抬升到 0.3；批中位数仍偏低（0.05），
    # 共用批中位数会把低浓度样品的浓度抬高。
    r = make(
        blank("B0", "110"),          # 0.05，进入批中位数
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "160"),          # 0.3，紧跟最高标样，归因残留
        sample("S001", "170"),       # 原始 0.35
    )
    result = next(x for x in r["final"] if x["sample_id"] == "S001")
    assert result["low_concentration"] is True
    assert result["adjacent_blank_class"] == "CARRYOVER"
    assert Decimal(result["batch_concentration"]) == Decimal("0.30")
    assert result["batch_conclusion"] == "检出"
    assert Decimal(result["concentration"]) == Decimal("0.05")
    assert result["conclusion"] == "检出"   # 0.05 恰在检出限，不下调为未检出
    assert result["qualifier"] == ""


def test_adjacent_carryover_blank_can_turn_detection_to_not_detected():
    r = make(
        blank("B0", "110"),          # 0.05
        row("C5", "standard", "Pb", "1100", "100", "5", "1", "ug/L", "B01"),
        blank("B1", "160"),          # 0.3 残留
        sample("S001", "160"),       # 原始 0.3 -> 批净 0.25 检出；相邻净 0 未检出
    )
    result = next(x for x in r["final"] if x["sample_id"] == "S001")
    assert result["batch_conclusion"] == "检出"
    assert result["conclusion"] == "未检出"
    assert result["blank_basis"] == "adjacent"
