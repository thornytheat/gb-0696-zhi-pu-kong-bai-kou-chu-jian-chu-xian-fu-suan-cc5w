from decimal import Decimal
from app.report import report

CONCLUSIONS = {
    "PASS": "检出",
    "BELOW_LOD": "未检出",
    "NEGATIVE_BLANK": "未检出",
    "OVER_RANGE": "超范围需复核",
    "CONTROL_FAIL": "质控失败",
}


def classify(value, lod, overrange=False, control_error=None):
    if overrange:
        return "OVER_RANGE"
    if control_error is not None and control_error > Decimal("0.20"):
        return "CONTROL_FAIL"
    if value < 0:
        return "NEGATIVE_BLANK"
    if value < lod:
        return "BELOW_LOD"
    return "PASS"


def qualifier_for(status):
    return "<" if status == "BELOW_LOD" else ""


def conclusion_for(status):
    """监管回执的最终检出结论：每个样品只给一条。"""
    return CONCLUSIONS.get(status, "需复核")


def _judge(r, curve, net, dilution, lod, error):
    value = net * dilution
    status = classify(value, lod, r["response"] > curve["max_response"], error)
    return {
        "concentration": value,
        "status": status,
        "qualifier": qualifier_for(status),
        "conclusion": conclusion_for(status),
    }


def apply(ctx):
    out = []
    for r in ctx["concentrations"]:
        c = ctx["curves"][(r["batch"], r["analyte"])]
        x = dict(r)
        error = (
            abs(r["concentration"] - r["level_value"]) / r["level_value"]
            if r["kind"] == "control" and r["level_value"]
            else None
        )
        lod = r["lod"]
        dilution = r["dilution_value"]
        batch = _judge(r, c, r["net_batch"], dilution, lod, error)
        adjacent = (
            _judge(r, c, r["net_adjacent"], dilution, lod, error)
            if r["net_adjacent"] is not None
            else None
        )
        switched = r["blank_basis"] == "adjacent" and adjacent is not None

        x.update({
            "lod": lod,
            "batch_concentration": batch["concentration"],
            "batch_status": batch["status"],
            "batch_qualifier": batch["qualifier"],
            "batch_conclusion": batch["conclusion"],
            "adjacent_concentration": adjacent["concentration"] if adjacent else None,
            "adjacent_status": adjacent["status"] if adjacent else None,
            "adjacent_qualifier": adjacent["qualifier"] if adjacent else None,
            "adjacent_conclusion": adjacent["conclusion"] if adjacent else None,
            "concentration": r["net"] * dilution,
            "status": (adjacent or batch)["status"],
            "qualifier": (adjacent or batch)["qualifier"],
            "conclusion": (adjacent or batch)["conclusion"],
        })
        if r["switch_status"] == "KEPT_ORIGINAL":
            # 切换失败保留原结论，并显式标注原结论可用。
            x["conclusion"] = batch["conclusion"]
            x["qualifier"] = batch["qualifier"]
            x["status"] = batch["status"]
        x["conclusion_basis"] = (
            "adjacent" if switched else "batch"
        )
        out.append(x)
    ctx["quality"] = out

    by_line = {row["line"]: row for row in out}
    for record in ctx["corrections"]:
        row = by_line.get(record["line"])
        if row is None:
            continue
        record.update({
            "batch_concentration": row["batch_concentration"],
            "batch_status": row["batch_status"],
            "batch_qualifier": row["batch_qualifier"],
            "batch_conclusion": row["batch_conclusion"],
            "adjacent_concentration": row["adjacent_concentration"],
            "adjacent_status": row["adjacent_status"],
            "adjacent_qualifier": row["adjacent_qualifier"],
            "adjacent_conclusion": row["adjacent_conclusion"],
            "final_concentration": row["concentration"],
            "final_status": row["status"],
            "final_qualifier": row["qualifier"],
            "final_conclusion": row["conclusion"],
            "conclusion_basis": row["conclusion_basis"],
            "lod": row["lod"],
            "changed": row["batch_conclusion"] != row["conclusion"],
        })
        if record["status"] == "KEPT_ORIGINAL":
            # 切换失败：回执沿用批中位数结论，但不能出现两条结论。
            record["final_conclusion"] = record["batch_conclusion"]
            record["final_qualifier"] = record["batch_qualifier"]
            record["final_status"] = record["batch_status"]
            record["conclusion_basis"] = "batch"

    changed = sum(1 for record in ctx["corrections"] if record["changed"])
    kept = sum(1 for record in ctx["corrections"] if record["status"] == "KEPT_ORIGINAL")
    return report(
        out,
        通过=sum(r["status"] == "PASS" for r in out),
        复核=sum(r["status"] != "PASS" for r in out),
        相邻空白=sum(1 for r in out if r["blank_basis"] == "adjacent"),
        原结论保留=kept,
        结论变更=changed,
    )
