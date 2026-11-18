import csv, io, json

FIELDS = [
    "run_id", "sha256", "rule_version", "kind", "sample_id", "analyte", "batch", "line",
    "conclusion", "qualifier", "lod", "concentration", "batch_blank", "adjacent_blank",
    "blank_basis", "blank_class", "blank_source", "peak_area", "internal_area",
    "reason", "evidence",
]


def _by_line(result):
    return {row.get("line"): row for row in result.get("quality", [])}


def _row(run, kind, row, conclusion="", qualifier="", lod="", concentration="",
         batch_blank="", adjacent_blank="", blank_basis="", blank_class="",
         blank_source="", peak_area="", internal_area="", reason="", evidence=None):
    return [
        run["id"], run["digest"], run["rule_version"], kind,
        row.get("sample_id", ""), row.get("analyte", ""), row.get("batch", ""),
        row.get("line", ""), conclusion, qualifier, lod, concentration,
        batch_blank, adjacent_blank, blank_basis, blank_class, blank_source,
        peak_area, internal_area, reason,
        json.dumps(evidence or {}, ensure_ascii=False, default=str),
    ]


def evidence_csv(run):
    """唯一证据CSV。回执（kind=result）每样一行、只写最终检出结论；
    空白取用、切换对照与原始峰面积作为证据列保留，便于回看。"""
    result = run["result"]
    quality = _by_line(result)
    amendments = run.get("amendments", [])
    by_line = {}
    for item in amendments:
        payload = item["payload"]
        # 同一实例可能因规则调整留存多条 basis 记录，回执以最新一条为解释。
        by_line[payload.get("line")] = item

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(FIELDS)

    manifest = {
        "balance": result["balance"],
        "decision": run["decision"],
        "revision": run["revision"],
        "blocked": result.get("blocked"),
        "blank_blocks": result.get("blank_blocks", []),
        "amendments": len(amendments),
    }
    writer.writerow([run["id"], run["digest"], run["rule_version"], "manifest",
                     "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
                     json.dumps(manifest, ensure_ascii=False, default=str)])

    for row in result.get("final", []):
        if row.get("kind") != "sample":
            continue
        final = quality.get(row.get("line"), row)
        entry = by_line.get(row.get("line"))
        switch = entry["payload"] if entry else None
        evidence = {
            "peak_area": row.get("area"),
            "internal_area": row.get("internal_area"),
            "response": row.get("response"),
            "net": final.get("net"),
            "batch_blank": final.get("batch_blank"),
            "batch_concentration": final.get("batch_concentration"),
            "adjacent_blank": final.get("adjacent_blank"),
            "adjacent_blank_id": final.get("adjacent_blank_id"),
            "adjacent_blank_line": final.get("adjacent_blank_line"),
            "adjacent_blank_class": final.get("adjacent_blank_class"),
            "adjacent_concentration": final.get("adjacent_concentration"),
            "switch_status": final.get("switch_status"),
            "switch_reason": final.get("switch_reason"),
            "low_concentration": final.get("low_concentration"),
            "conclusion_basis": final.get("conclusion_basis"),
            "amendment_id": entry.get("id") if entry else None,
        }
        writer.writerow(_row(
            run, "result", row,
            # 监管回执：每个样品只留这一条最终检出结论。
            conclusion=final.get("conclusion", ""),
            qualifier=final.get("qualifier", ""),
            lod=final.get("lod", ""),
            concentration=final.get("concentration", ""),
            batch_blank=final.get("batch_blank", ""),
            adjacent_blank=final.get("adjacent_blank", ""),
            blank_basis=final.get("blank_basis", ""),
            blank_class=final.get("adjacent_blank_class", ""),
            blank_source=final.get("blank_source", ""),
            peak_area=row.get("area", ""),
            internal_area=row.get("internal_area", ""),
            reason=final.get("status", ""),
            evidence=evidence,
        ))

    # 切换对照：只作审计证据，不再给出第二条检出结论。
    seen = set()
    for item in amendments:
        payload = item["payload"]
        key = (payload.get("sample_id"), payload.get("analyte"), payload.get("batch"),
               payload.get("line"), payload.get("basis"))
        if key in seen:
            continue
        seen.add(key)
        final = quality.get(payload.get("line"), {})
        writer.writerow(_row(
            run, "amendment", payload,
            lod=payload.get("lod", ""),
            concentration=payload.get("final_concentration", ""),
            batch_blank=payload.get("batch_blank", ""),
            adjacent_blank=payload.get("adjacent_blank", ""),
            blank_basis=payload.get("basis", ""),
            blank_class=payload.get("adjacent_blank_class", ""),
            blank_source=final.get("blank_source", ""),
            peak_area=final.get("area", ""),
            internal_area=final.get("internal_area", ""),
            reason=payload.get("status", ""),
            evidence={
                "amendment_id": item.get("id"),
                "before_concentration": payload.get("batch_concentration"),
                "after_concentration": payload.get("adjacent_concentration"),
                "before_conclusion": payload.get("batch_conclusion"),
                "after_conclusion": payload.get("adjacent_conclusion"),
                "before_status": payload.get("batch_status"),
                "after_status": payload.get("adjacent_status"),
                "before_qualifier": payload.get("batch_qualifier"),
                "after_qualifier": payload.get("adjacent_qualifier"),
                "changed": payload.get("changed"),
                "adjacent_blank_id": payload.get("adjacent_blank_id"),
                "adjacent_blank_line": payload.get("adjacent_blank_line"),
            },
        ))

    for row in result.get("blank_report", []):
        writer.writerow(_row(
            run, "blank", row,
            batch_blank=row.get("blank_value", ""),
            blank_class=row.get("blank_class", ""),
            blank_source=row.get("baseline_source", ""),
            reason="BLANK_EVIDENCE",
            evidence={
                "blank_value": row.get("blank_value"),
                "after_high_standard": row.get("after_high_standard"),
                "used_for_baseline": row.get("used_for_baseline"),
            },
        ))

    for row in result.get("quarantine", []):
        raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
        writer.writerow([
            run["id"], run["digest"], run["rule_version"], "quarantine",
            row.get("id", ""), raw.get("analyte", ""), raw.get("batch", ""),
            row.get("line", ""), "", "", "", "", "", "", "", "", "", "", "",
            row["reason"], json.dumps(row, ensure_ascii=False, default=str),
        ])
    return "﻿" + out.getvalue()
