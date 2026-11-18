from statistics import median
from decimal import Decimal
from app.report import report, quarantine
from app.numbers import number
from app.errors import BusinessError
from app.modules.m2_internal.rules import UNITS

# 空白判定参数：紧跟在最高标样之后的空白若明显抬升，可归因为进样残留；
# 没有高标样前因的抬升空白无法解释，必须隔离并阻止整批放行。
HIGH_STANDARD_RATIO = Decimal("0.8")
ELEVATED_STANDARD_RATIO = Decimal("0.2")
MAD_FACTOR = Decimal(3)


def blank_value(response, curve):
    return (response - curve["intercept"]) / curve["slope"]


def corrected(response, curve, blank):
    return blank_value(response, curve) - blank


def _level_of(row, normalized):
    if normalized is not None:
        return normalized.get("level_value")
    try:
        unit = row.get("unit")
        if unit not in UNITS:
            return None
        return number(row.get("level")) * UNITS[unit]
    except BusinessError:
        return None


def _blank_records(rows, norm_by_line, curve, high_cut):
    """按进样顺序为组内每个空白建档，并记录其前一行是否为高标样。"""
    records = []
    for index, row in enumerate(rows):
        if row.get("kind") != "blank":
            continue
        normalized = norm_by_line.get(row.get("line"))
        valid = normalized is not None and normalized.get("kind") == "blank"
        value = None
        if valid:
            if curve is None:
                valid = False
            else:
                value = blank_value(normalized["response"], curve)
        previous = rows[index - 1] if index else None
        after_high = False
        if previous is not None and previous.get("kind") == "standard":
            level = _level_of(previous, norm_by_line.get(previous.get("line")))
            if level is not None and high_cut is not None and level >= high_cut:
                after_high = True
        records.append({
            "sample_id": row.get("sample_id", ""),
            "line": row.get("line"),
            "valid": valid,
            "blank_value": value,
            "after_high_standard": after_high,
            "blank_class": None,
        })
    return records


def _fence(records):
    """稳健上限：中位数 + 3MAD；样本太少时以其余空白单独核对。"""
    values = [r["blank_value"] for r in records]
    if not values:
        return None
    center = median(values)
    spread = median([abs(value - center) for value in values])
    return center + MAD_FACTOR * spread


def _limit_for(record, records, lowest_level):
    """逐条留一计算抬升阈值，避免单个异常空白把自己的上限抬高。

    阈值取「其余空白稳健上限」与「最低正校准点20%」中的较小者：
    后者保证整批空白均匀偏高时仍能被识别为抬升。
    其余空白少于3个时 MAD 退化（两点时离散度为0），只依赖20%准则，
    避免把正常的批内波动误判为来源不明偏高。
    """
    others = [r for r in records if r is not record and r["blank_value"] is not None]
    limits = []
    if len(others) >= 3:
        fence = _fence(others)
        if fence is not None:
            limits.append(fence)
    if lowest_level is not None and lowest_level > 0:
        limits.append(ELEVATED_STANDARD_RATIO * lowest_level)
    return min(limits) if limits else None


def _classify(records, lowest_level):
    limits = {}
    for record in records:
        limit = _limit_for(record, records, lowest_level)
        limits[record["line"]] = limit
        if record["blank_value"] is None:
            record["blank_class"] = "INVALID"
        elif limit is not None and record["blank_value"] > limit:
            record["blank_class"] = (
                "CARRYOVER" if record["after_high_standard"] else "UNKNOWN_HIGH"
            )
        else:
            record["blank_class"] = "CLEAN"
        record["elevation_limit"] = limit
    return limits


def _nearest_blank(records, line):
    """相邻空白：优先取进样顺序上最近的已注入空白，没有前置时取最近后置。"""
    if not records:
        return None
    before = [record for record in records if record["line"] < line]
    if before:
        return max(before, key=lambda record: record["line"])
    return min(records, key=lambda record: record["line"])


def _source_label(kind, records):
    refs = "|".join(
        "{}:{}".format(record["sample_id"], record["line"]) for record in records
    )
    return "{}:{}".format(kind, refs) if refs else kind


def apply(ctx):
    groups = {}
    for row in ctx["raw"]:
        groups.setdefault((row["batch"], row["analyte"]), []).append(row)

    norm_by_line = {}
    norm_groups = {}
    for row in ctx["normalized"]:
        norm_by_line[row["line"]] = row
        norm_groups.setdefault((row["batch"], row["analyte"]), []).append(row)

    ctx["corrections"] = []
    ctx["blank_blocks"] = []
    blank_report = []
    blank_groups = {}
    baselines = {}
    out = []

    for key, rows in groups.items():
        curve = ctx["curves"].get(key)
        if not curve:
            for record in _blank_records(rows, norm_by_line, None, None):
                record.update({
                    "batch": key[0],
                    "analyte": key[1],
                    "blank_class": "CURVE_MISSING",
                    "baseline_source": "MISSING",
                    "used_for_baseline": False,
                })
                blank_report.append(record)
            for row in norm_groups.get(key, []):
                if row["kind"] in {"sample", "control"}:
                    quarantine(ctx, row, "CURVE_MISSING")
            continue

        standards = [r for r in norm_groups.get(key, []) if r["kind"] == "standard"]
        levels = [r["level_value"] for r in standards if r["level_value"] > 0]
        max_level = max(levels) if levels else None
        lowest_level = min(levels) if levels else None
        high_cut = max_level * HIGH_STANDARD_RATIO if max_level is not None else None

        records = _blank_records(rows, norm_by_line, curve, high_cut)
        limits = _classify(records, lowest_level)
        clean = [r for r in records if r["blank_class"] == "CLEAN"]
        carryover = [r for r in records if r["blank_class"] == "CARRYOVER"]
        unknown = [r for r in records if r["blank_class"] == "UNKNOWN_HIGH"]

        batch_blank = None
        baseline_source = "MISSING"
        if clean:
            batch_blank = median([r["blank_value"] for r in clean])
            baseline_source = "CLEAN"
        elif carryover:
            # 仅剩可归因于高标样残留的空白：仍可作为整批基线，但降级登记。
            batch_blank = median([r["blank_value"] for r in carryover])
            baseline_source = "CARRYOVER_ONLY"

        for record in records:
            item = dict(record)
            item.update({
                "batch": key[0],
                "analyte": key[1],
                "baseline_source": baseline_source,
                "used_for_baseline": (
                    (baseline_source == "CLEAN" and record["blank_class"] == "CLEAN")
                    or (baseline_source == "CARRYOVER_ONLY"
                        and record["blank_class"] == "CARRYOVER")
                ),
            })
            blank_report.append(item)

        for record in unknown:
            row = norm_by_line.get(record["line"])
            if row is not None:
                quarantine(ctx, row, "BLANK_UNKNOWN_HIGH")
            ctx["blank_blocks"].append({
                "batch": key[0],
                "analyte": key[1],
                "sample_id": record["sample_id"],
                "line": record["line"],
                "blank_value": record["blank_value"],
                "elevation_limit": limits.get(record["line"]),
            })

        blank_groups[key] = {
            "batch_blank": batch_blank,
            "baseline_source": baseline_source,
            "elevation_limit": min(
                (value for value in limits.values() if value is not None),
                default=None,
            ),
            "lowest_level": lowest_level,
            "clean": clean,
            "carryover": carryover,
            "unknown_high": unknown,
            "invalid": [r for r in records if r["blank_class"] == "INVALID"],
        }
        if batch_blank is not None:
            baselines[key] = batch_blank

        for row in norm_groups.get(key, []):
            if row["kind"] not in {"sample", "control"}:
                continue
            if batch_blank is None:
                quarantine(ctx, row, "BLANK_MISSING")
                continue

            raw_value = blank_value(row["response"], curve)
            net_batch = raw_value - batch_blank
            adjacent = _nearest_blank(records, row["line"])
            low = (
                row["kind"] == "sample"
                and lowest_level is not None
                and raw_value <= lowest_level
            )

            item = dict(row)
            item.update({
                "batch_blank": batch_blank,
                "adjacent_blank": adjacent["blank_value"] if adjacent else None,
                "adjacent_blank_id": adjacent["sample_id"] if adjacent else "",
                "adjacent_blank_line": adjacent["line"] if adjacent else None,
                "adjacent_blank_class": adjacent["blank_class"] if adjacent else "NONE",
                "blank_basis": "batch",
                "blank_source": _source_label(
                    baseline_source, clean if baseline_source == "CLEAN" else carryover
                ),
                "net_batch": net_batch,
                "net_adjacent": None,
                "low_concentration": low,
                "switch_status": "NOT_SAMPLE" if row["kind"] == "control" else "NOT_LOW",
                "switch_reason": "",
                "net": net_batch,
            })

            if low:
                reason = ""
                if adjacent is None:
                    item["switch_status"], reason = "KEPT_ORIGINAL", "NO_ADJACENT_BLANK"
                elif adjacent["blank_value"] is None:
                    item["switch_status"], reason = (
                        "KEPT_ORIGINAL", "ADJACENT_BLANK_INVALID"
                    )
                elif adjacent["blank_class"] == "UNKNOWN_HIGH":
                    item["switch_status"], reason = (
                        "KEPT_ORIGINAL", "ADJACENT_BLANK_ISOLATED"
                    )
                else:
                    item["switch_status"] = "APPLIED"
                    item["blank_basis"] = "adjacent"
                    item["net_adjacent"] = raw_value - adjacent["blank_value"]
                    item["net"] = item["net_adjacent"]
                    item["blank_source"] = _source_label("ADJACENT", [adjacent])
                item["switch_reason"] = reason

                ctx["corrections"].append({
                    "sample_id": item["sample_id"],
                    "analyte": item["analyte"],
                    "batch": item["batch"],
                    "line": item["line"],
                    "kind": item["kind"],
                    "dilution": item["dilution_value"],
                    "basis": item["blank_basis"],
                    "status": item["switch_status"],
                    "reason": reason,
                    "batch_blank": batch_blank,
                    "adjacent_blank": item["adjacent_blank"],
                    "adjacent_blank_id": item["adjacent_blank_id"],
                    "adjacent_blank_line": item["adjacent_blank_line"],
                    "adjacent_blank_class": item["adjacent_blank_class"],
                    "net_batch": net_batch,
                    "net_adjacent": item["net_adjacent"],
                })
            out.append(item)

    ctx["blanks"] = baselines
    ctx["blank_groups"] = blank_groups
    ctx["blank_report"] = blank_report
    ctx["corrected"] = out
    return report(
        out,
        样品=len(out),
        空白组=len(groups),
        修正=len(ctx["corrections"]),
        隔离空白=len(ctx["blank_blocks"]),
        空白=blank_report,
    )
