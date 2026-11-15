"""空间聚集扫描。

方法口径（与 docs/cluster.md 一致）：

* 暴露单位是已完成配送订单，未完成配送不进入分母；事件单位是发生过
  ``退货`` 联系的去重订单。同一申请的多次联系、同一订单的多个申请都
  不会制造多个事件，联系、申请、订单三种计数分别报告。
* 按品种与履约周分层间接标准化：窗口期望事件数由各层基准率加总，
  控制品种与履约周的构成差异。
* Poisson 扫描统计量（Kulldorff 型单侧高值 LLR），枚举圆心、半径与
  履约周时间前缀；推断使用在同一套完整搜索网格上进行的分层置换
  蒙特卡洛，因此 p 值对应整个搜索过程的族错误率，而不是挑中窗口的
  局部检验。
* 街道级位置有多个候选坐标时按权重抽取定位实现，服务边界按三种情景
  处理（时效边界、早期固定、晚期固定），两者共同进入线索稳定性判断。
* 没有完成配送订单的位置没有可见暴露，不参与扫描，也不得解释为零
  风险。结论只支持街道/路段级分辨率，不产生任何住户级标签。
"""

import math
import re
from datetime import date

import numpy as np

RETURN_ACTION = "退货"
DEFAULT_SEED = 7
DEFAULT_REPLICATIONS = 399
DEFAULT_REALIZATIONS = 100
DEFAULT_MIN_EVENTS = 2
MAX_EXPOSURE_FRACTION = 0.5
MAX_REPORTED_CLUSTERS = 10
WEEK_PATTERN = re.compile(r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")
TIME_VARYING = "time_varying"
FIXED_EARLIEST = "fixed_earliest"
FIXED_LATEST = "fixed_latest"
BOUNDARY_SCENARIOS = (TIME_VARYING, FIXED_EARLIEST, FIXED_LATEST)
SCENARIO_LABELS = {
    TIME_VARYING: "时效边界（按配送日生效的服务范围）",
    FIXED_EARLIEST: "早期固定边界（最早版本服务范围用于全时段）",
    FIXED_LATEST: "晚期固定边界（最晚版本服务范围用于全时段）",
}


class ScanError(ValueError):
    pass


def week_key(day):
    """履约日的 ISO 周键，例如 2026-W31。"""
    year, week, _ = date.fromisoformat(day).isocalendar()
    return f"{year}-W{week:02d}"


def _centroid(candidates):
    weights = np.array([float(c["weight"]) for c in candidates], dtype=float)
    total = weights.sum()
    if total <= 0:
        raise ScanError("候选位置权重之和必须为正")
    weights = weights / total
    xs = np.array([float(c["x_km"]) for c in candidates], dtype=float)
    ys = np.array([float(c["y_km"]) for c in candidates], dtype=float)
    return [float((xs * weights).sum()), float((ys * weights).sum())]


def build_geometry(catalog):
    """每个位置的加权质心、候选点与定位分辨率。"""
    geometry = {}
    for record in catalog.rows("locations"):
        candidates = record["candidates"]
        if not isinstance(candidates, list) or not candidates:
            raise ScanError(f"位置 {record['id']} 缺少候选坐标")
        geometry[record["id"]] = {
            "resolution": record["resolution"],
            "candidates": candidates,
            "centroid": _centroid(candidates),
        }
    return geometry


def build_frame(catalog, species=None, week_from=None, week_to=None):
    """构造分析底表：完成订单、去重退货事件、分层基准率与计数口径。

    过滤条件只按品种白名单和履约周范围；未完成配送的订单在这一步就
    被排除，因此既不进入暴露也不可能成为事件。
    """
    if species is not None:
        if not species or any(not isinstance(name, str) or not name for name in species):
            raise ScanError("品种过滤条件无效")
        species = sorted(set(species))
    for value, label in ((week_from, "起始周"), (week_to, "起始周")):
        if value is not None and not WEEK_PATTERN.fullmatch(value):
            raise ScanError(f"{label}需采用 ISO 周格式，例如 2026-W31")
    if week_from and week_to and week_from > week_to:
        raise ScanError("履约周范围颠倒")

    deliveries = {row["id"]: row for row in catalog.rows("deliveries")}
    completed = {identifier: row for identifier, row in deliveries.items() if row["delivered"]}

    contact_rows = catalog.rows("contacts")
    order_contacts = {}
    order_claims = {}
    returned_orders = set()
    for row in contact_rows:
        delivery_id = row["delivery_id"]
        if delivery_id not in completed:
            continue
        order_contacts[delivery_id] = order_contacts.get(delivery_id, 0) + 1
        order_claims.setdefault(delivery_id, set()).add(row["claim_id"])
        if row["action"] == RETURN_ACTION:
            returned_orders.add(delivery_id)

    orders = []
    for identifier, row in completed.items():
        week = week_key(row["day"])
        if species is not None and row["species"] not in species:
            continue
        if week_from is not None and week < week_from:
            continue
        if week_to is not None and week > week_to:
            continue
        orders.append({
            "order_id": identifier,
            "location_id": row["location_id"],
            "species": row["species"],
            "week": week,
            "day": row["day"],
            "service_area": row["service_area"],
            "quantity_kg": float(row["quantity_kg"]),
            "event": identifier in returned_orders,
            "contacts": order_contacts.get(identifier, 0),
            "claims": len(order_claims.get(identifier, ())),
        })
    if not orders:
        raise ScanError("过滤后没有已完成配送订单，无法估计风险")

    strata = {}
    for order in orders:
        key = (order["species"], order["week"])
        bucket = strata.setdefault(key, {"exposure": 0, "events": 0})
        bucket["exposure"] += 1
        bucket["events"] += int(order["event"])
    stratum_table = []
    rates = {}
    for (name, week), bucket in sorted(strata.items()):
        rate = bucket["events"] / bucket["exposure"]
        rates[(name, week)] = rate
        stratum_table.append({
            "key": f"{name}|{week}", "species": name, "week": week,
            "exposure": bucket["exposure"], "events": bucket["events"],
            "return_rate": rate,
        })

    multi_contact = sorted(order["order_id"] for order in orders
                           if order["contacts"] > order["claims"] or order["claims"] > 1)
    counting = {
        "exposure_unit": "已完成配送订单",
        "event_unit": "退货订单（按订单去重的二值事件）",
        "exposure_orders": len(orders),
        "return_orders": sum(int(order["event"]) for order in orders),
        "claims": sum(order["claims"] for order in orders),
        "contact_records": sum(order["contacts"] for order in orders),
        "multi_contact_order_ids": multi_contact,
        "note": "一项申请可产生多次联系；联系数和申请数都不是退货事件数，事件按订单只计一次。",
    }
    return {
        "orders": orders,
        "strata": stratum_table,
        "rates": rates,
        "counting": counting,
        "species": sorted({order["species"] for order in orders}),
        "weeks": sorted({order["week"] for order in orders}),
    }


def build_boundary_versions(catalog):
    """area_id -> 按生效日排序的边界版本（左闭右开）。"""
    versions = {}
    for row in catalog.rows("service_areas"):
        versions.setdefault(row["area_id"], []).append(
            (row["valid_from"], row["valid_to"], row["boundary"]))
    for area_id, rows in versions.items():
        rows.sort(key=lambda item: item[0])
    return versions


def _boundary_for(versions, area_id, day, scenario):
    rows = versions.get(area_id, [])
    if not rows:
        return None
    if scenario == FIXED_EARLIEST:
        return rows[0][2]
    if scenario == FIXED_LATEST:
        return rows[-1][2]
    for valid_from, valid_to, boundary in rows:
        if valid_from <= day < valid_to:
            return boundary
    return rows[-1][2]


def _inside(boundary, point):
    return (boundary["xmin"] <= point[0] <= boundary["xmax"]
            and boundary["ymin"] <= point[1] <= boundary["ymax"])


def eligible_order_mask(frame, geometry, versions, scenario):
    """按边界情景判断订单的质心是否处于当日可达服务范围内。"""
    mask = []
    for order in frame["orders"]:
        point = geometry[order["location_id"]]["centroid"]
        boundary = _boundary_for(versions, order["service_area"], order["day"], scenario)
        mask.append(boundary is None or _inside(boundary, point))
    return np.array(mask, dtype=bool)


def _realized_points(geometry, location_ids, rng):
    points = np.zeros((len(location_ids), 2), dtype=float)
    for index, location_id in enumerate(location_ids):
        candidates = geometry[location_id]["candidates"]
        if len(candidates) == 1:
            points[index] = [candidates[0]["x_km"], candidates[0]["y_km"]]
            continue
        weights = np.array([c["weight"] for c in candidates], dtype=float)
        choice = rng.choice(len(candidates), p=weights / weights.sum())
        points[index] = [candidates[choice]["x_km"], candidates[choice]["y_km"]]
    return points


def _enumerate_windows(loc_index, strat_index, events, strata_weeks, rates,
                       points, include_time, min_events):
    """枚举完整搜索网格并计算每个窗口的 LLR。

    返回 (windows, totals)；windows 含位置索引集合、半径、时间前缀与
    观测计数。蒙特卡洛重复使用同一网格，只替换事件向量。
    """
    n = len(points)
    stratum_ids = sorted(set(int(value) for value in strat_index))
    compact = {sid: column for column, sid in enumerate(stratum_ids)}
    strat_column = np.array([compact[int(value)] for value in strat_index], dtype=int)
    week_ids = sorted({strata_weeks[sid] for sid in stratum_ids})
    week_position = {week: position for position, week in enumerate(week_ids)}
    m = len(stratum_ids)

    exposure = np.zeros((n, m), dtype=float)
    observed = np.zeros((n, m), dtype=float)
    np.add.at(exposure, (loc_index, strat_column), 1.0)
    np.add.at(observed, (loc_index, strat_column), events.astype(float))

    p = np.array([rates[sid] for sid in stratum_ids], dtype=float)
    mu_stratum = exposure * p[np.newaxis, :]
    week_of_stratum = np.array([week_position[strata_weeks[sid]] for sid in stratum_ids], dtype=int)
    event_week = np.zeros((n, len(week_ids)), dtype=float)
    mu_week = np.zeros((n, len(week_ids)), dtype=float)
    row_index = np.repeat(np.arange(n), m)
    week_index = np.tile(week_of_stratum, n)
    np.add.at(event_week, (row_index, week_index), observed.reshape(-1))
    np.add.at(mu_week, (row_index, week_index), mu_stratum.reshape(-1))

    total_events = float(events.sum())
    total_mu = float(mu_stratum.sum())
    cap = MAX_EXPOSURE_FRACTION * total_mu

    distances = np.sqrt(((points[:, np.newaxis, :] - points[np.newaxis, :, :]) ** 2).sum(axis=2))
    center_masks = []
    radius_by_center = []
    for center in range(n):
        levels = []
        # 四舍五入到 0.1 毫米，消除浮点噪声产生的近重复半径
        for radius in np.unique(np.round(distances[center], 6)):
            members = np.flatnonzero(distances[center] <= radius + 1e-9)
            levels.append((float(radius), members))
        center_masks.append(levels)
        radius_by_center.append([float(radius) for radius, _ in levels])

    windows = []

    def llr_values(a, b):
        result = np.full_like(a, -np.inf, dtype=float)
        ok = ((a > b) & (b > 0) & (b < total_mu) & (a < total_events))
        aa, bb = a[ok], b[ok]
        result[ok] = aa * np.log(aa / bb) + (total_events - aa) * np.log(
            (total_events - aa) / (total_mu - bb))
        return result

    event_loc = event_week.sum(axis=1)
    mu_loc = mu_week.sum(axis=1)
    for center, levels in enumerate(center_masks):
        masks = np.zeros((len(levels), n), dtype=bool)
        radii = np.zeros(len(levels), dtype=float)
        for row, (radius, members) in enumerate(levels):
            masks[row, members] = True
            radii[row] = radius
        a_spatial = masks @ event_loc
        b_spatial = masks @ mu_loc
        scores = llr_values(a_spatial, b_spatial)
        for row in range(len(levels)):
            a, b, score = float(a_spatial[row]), float(b_spatial[row]), float(scores[row])
            if math.isfinite(score) and a >= min_events and b <= cap:
                windows.append({
                    "center": center, "radius": float(radii[row]),
                    "members": frozenset(np.flatnonzero(masks[row]).tolist()),
                    "week_tip": None, "events": a, "expected": b, "llr": score,
                })
        if include_time:
            a_week = masks @ event_week
            b_week = masks @ mu_week
            a_prefix = np.cumsum(a_week, axis=1)
            b_prefix = np.cumsum(b_week, axis=1)
            for row in range(len(levels)):
                scores_t = llr_values(a_prefix[row], b_prefix[row])
                for tip in range(len(week_ids)):
                    a, b = float(a_prefix[row, tip]), float(b_prefix[row, tip])
                    score = float(scores_t[tip])
                    if math.isfinite(score) and a >= min_events and b <= cap:
                        windows.append({
                            "center": center, "radius": float(radii[row]),
                            "members": frozenset(np.flatnonzero(masks[row]).tolist()),
                            "week_tip": tip, "events": a, "expected": b, "llr": score,
                        })

    totals = {"events": total_events, "mu": total_mu,
              "week_ids": week_ids, "stratum_ids": stratum_ids}
    return windows, totals, (event_week, mu_week, center_masks, week_of_stratum, strat_column)


def _permute_events(loc_index, strat_index, events, rng):
    """在每个分层内部置换订单事件标签，保持各层事件总数与暴露分布。"""
    shuffled = np.empty_like(events)
    for sid in np.unique(strat_index):
        group = np.flatnonzero(strat_index == sid)
        shuffled[group] = events[rng.permutation(group)]
    return shuffled


def run_scan(frame, geometry, versions, scenario, points=None,
             replications=DEFAULT_REPLICATIONS, min_events=DEFAULT_MIN_EVENTS,
             include_time=True, seed=DEFAULT_SEED):
    """一次完整扫描：观测网格加同网格蒙特卡洛推断。"""
    rng = np.random.default_rng(seed)
    eligible = eligible_order_mask(frame, geometry, versions, scenario)
    orders = [order for order, keep in zip(frame["orders"], eligible) if keep]
    if not orders:
        raise ScanError("该边界情景下没有可达订单")

    location_ids = sorted({order["location_id"] for order in orders})
    location_pos = {location_id: index for index, location_id in enumerate(location_ids)}
    if points is None:
        points = np.array([geometry[location_id]["centroid"] for location_id in location_ids])
    points = np.asarray(points, dtype=float)

    strat_lookup = {(row["species"], row["week"]): index
                    for index, row in enumerate(frame["strata"])}
    strata_weeks = {index: row["week"] for index, row in enumerate(frame["strata"])}
    rates = {index: row["return_rate"] for index, row in enumerate(frame["strata"])}
    loc_index = np.array([location_pos[order["location_id"]] for order in orders], dtype=int)
    strat_index = np.array([strat_lookup[(order["species"], order["week"])] for order in orders], dtype=int)
    events = np.array([int(order["event"]) for order in orders], dtype=int)

    windows, totals, cache = _enumerate_windows(
        loc_index, strat_index, events, strata_weeks, rates,
        points, include_time, min_events)
    if not windows:
        return {"windows": [], "null_maximums": [], "threshold_95": None,
                "location_ids": location_ids, "orders": orders, "points": points,
                "excluded_orders": int((~eligible).sum()),
                "total_events": float(events.sum()), "total_mu": 0.0}

    null_maximums = []
    if replications:
        event_week, mu_week, center_masks, week_of_stratum, strat_column = cache
        n = len(location_ids)
        stratum_count = len(totals["stratum_ids"])
        total_events, total_mu = totals["events"], totals["mu"]
        cap = MAX_EXPOSURE_FRACTION * total_mu
        week_index = np.tile(week_of_stratum, n)
        row_index = np.arange(n).repeat(stratum_count)
        for _ in range(replications):
            sim_events = _permute_events(loc_index, strat_index, events, rng)
            sim_stratum = np.zeros((n, stratum_count), dtype=float)
            np.add.at(sim_stratum, (loc_index, strat_column), sim_events.astype(float))
            sim_observed = np.zeros_like(event_week)
            np.add.at(sim_observed, (row_index, week_index), sim_stratum.reshape(-1))
            best = 0.0
            sim_loc = sim_observed.sum(axis=1)
            mu_loc = mu_week.sum(axis=1)
            for levels in center_masks:
                masks = np.zeros((len(levels), n), dtype=bool)
                for row, (_, members) in enumerate(levels):
                    masks[row, members] = True
                a_spatial = masks @ sim_loc
                b_spatial = masks @ mu_loc
                best = max(best, _max_llr(a_spatial, b_spatial, total_events, total_mu, cap, min_events))
                if include_time:
                    a_prefix = np.cumsum(masks @ sim_observed, axis=1)
                    b_prefix = np.cumsum(masks @ mu_week, axis=1)
                    best = max(best, _max_llr(a_prefix, b_prefix, total_events, total_mu,
                                              cap, min_events))
            null_maximums.append(best)

    null_sorted = sorted(null_maximums)
    threshold = float(np.quantile(null_sorted, 0.95)) if null_sorted else None
    return {
        "windows": windows, "null_maximums": null_maximums, "threshold_95": threshold,
        "location_ids": location_ids, "orders": orders, "points": points,
        "excluded_orders": int((~eligible).sum()),
        "total_events": totals["events"], "total_mu": totals["mu"],
    }


def _max_llr(a, b, total_events, total_mu, cap, min_events):
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    ok = ((a > b) & (b > 0) & (b < total_mu) & (a < total_events)
          & (a >= min_events) & (b <= cap))
    if not ok.any():
        return 0.0
    aa, bb = a[ok], b[ok]
    scores = aa * np.log(aa / bb) + (total_events - aa) * np.log(
        (total_events - aa) / (total_mu - bb))
    return float(scores.max())


def _p_value(llr, null_maximums):
    if not null_maximums:
        return None
    exceed = 1 + sum(1 for value in null_maximums if value >= llr - 1e-12)
    return exceed / (len(null_maximums) + 1)


def _select_clusters(windows):
    """按 LLR 贪心选择位置互不重叠的候选线索。"""
    ranked = sorted(windows, key=lambda item: item["llr"], reverse=True)
    selected = []
    used = set()
    for window in ranked:
        if window["llr"] <= 0:
            break
        members = set(window["members"])
        if members & used:
            continue
        selected.append(window)
        used |= members
        if len(selected) >= MAX_REPORTED_CLUSTERS:
            break
    return selected


def _jaccard(left, right):
    left, right = set(left), set(right)
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def _geocoding_stability(lead, frame, geometry, versions, scenario, realization_count,
                         include_time, min_events, seed, threshold):
    """街道候选点抽样实现下的线索稳定性。"""
    rng = np.random.default_rng(seed + 10_000)
    lead_member_ids = set(lead["location_ids"])
    anchor = lead["anchor_location_id"]
    overlaps, anchor_hits, detections, drifts, radii = [], [], [], [], []
    frame_location_ids = sorted({order["location_id"] for order in frame["orders"]})
    for _ in range(realization_count):
        points = _realized_points(geometry, frame_location_ids, rng)
        result = run_scan(frame, geometry, versions, scenario, points=points,
                          replications=0, min_events=min_events,
                          include_time=include_time, seed=int(rng.integers(0, 2**31 - 1)))
        if not result["windows"]:
            overlaps.append(0.0)
            anchor_hits.append(False)
            detections.append(False)
            continue
        ids = result["location_ids"]
        index_of = {location_id: index for index, location_id in enumerate(ids)}
        if anchor not in index_of:
            overlaps.append(0.0)
            anchor_hits.append(False)
            detections.append(False)
            continue
        target = {index_of[value] for value in lead_member_ids if value in index_of}

        def overlap(window):
            return _jaccard(window["members"], target)

        best_window = max(result["windows"], key=overlap)
        realized_members = {ids[index] for index in best_window["members"]}
        overlaps.append(_jaccard(realized_members, lead_member_ids))
        anchor_hits.append(anchor in realized_members)
        detections.append(threshold is not None and best_window["llr"] >= threshold)
        radii.append(best_window["radius"])
        realized_centroid = result["points"][sorted(best_window["members"])].mean(axis=0)
        # 漂移按同组位置的质心坐标比较，避免不同实现间的半径伸缩混入
        baseline_points = np.array([geometry[value]["centroid"] for value in sorted(realized_members)])
        drifts.append(float(np.linalg.norm(baseline_points.mean(axis=0) - realized_centroid)))
    return {
        "realizations": realization_count,
        "detection_rate": sum(detections) / realization_count,
        "anchor_inclusion_rate": sum(anchor_hits) / realization_count,
        "jaccard_mean": float(np.mean(overlaps)),
        "jaccard_min": float(np.min(overlaps)),
        "centroid_drift_km_max": float(np.max(drifts)) if drifts else None,
        "radius_span_km": [float(np.min(radii)), float(np.max(radii))] if radii else None,
    }


def _boundary_stability(lead, frame, geometry, versions, baseline_scenario,
                        include_time, min_events, seed, threshold):
    """替代服务边界情景下同一线索是否仍然成立。"""
    output = []
    for scenario in BOUNDARY_SCENARIOS:
        result = run_scan(frame, geometry, versions, scenario,
                          replications=0, min_events=min_events,
                          include_time=include_time, seed=seed)
        entry = {"scenario": scenario, "label": SCENARIO_LABELS[scenario],
                 "eligible_orders": len(result["orders"]),
                 "excluded_orders": result["excluded_orders"]}
        ids = result["location_ids"]
        index_of = {location_id: index for index, location_id in enumerate(ids)}
        if lead["anchor"] not in index_of:
            entry.update({"matched": False, "jaccard": 0.0, "exceeds_null_envelope": False})
        else:
            target = {index_of[value] for value in lead["location_ids"] if value in index_of}
            best_window = max(result["windows"], key=lambda item: _jaccard(item["members"], target),
                              default=None)
            if best_window is None:
                entry.update({"matched": False, "jaccard": 0.0, "exceeds_null_envelope": False})
            else:
                entry.update({
                    "matched": True,
                    "jaccard": _jaccard(best_window["members"], target),
                    "exceeds_null_envelope": threshold is not None and best_window["llr"] >= threshold,
                    "llr": best_window["llr"],
                })
        if scenario == baseline_scenario:
            entry["baseline"] = True
        output.append(entry)
    return output


def _attach_order_details(cluster, orders, location_ids, frame):
    member_set = set(cluster["members"])
    tip = cluster["window"]["week_tip"]
    weeks = frame["weeks"]
    week_limit = weeks[tip] if tip is not None else weeks[-1]
    in_window = []
    for order in orders:
        index = location_ids.index(order["location_id"])
        if index in member_set and order["week"] <= week_limit:
            in_window.append(order)
    returned_ids = sorted(order["order_id"] for order in in_window if order["event"])
    exposure_ids = sorted(order["order_id"] for order in in_window)
    species_breakdown, week_breakdown = {}, {}
    for order in in_window:
        species_breakdown.setdefault(order["species"], {"exposure": 0, "events": 0})
        species_breakdown[order["species"]]["exposure"] += 1
        species_breakdown[order["species"]]["events"] += int(order["event"])
        week_breakdown.setdefault(order["week"], {"exposure": 0, "events": 0})
        week_breakdown[order["week"]]["exposure"] += 1
        week_breakdown[order["week"]]["events"] += int(order["event"])
    return {
        "return_order_ids": returned_ids,
        "exposure_order_ids": exposure_ids,
        "contact_records": sum(order["contacts"] for order in in_window),
        "claims": sum(order["claims"] for order in in_window),
        "multi_contact_order_ids": sorted(
            order["order_id"] for order in in_window
            if order["contacts"] > order["claims"] or order["claims"] > 1),
        "species_breakdown": species_breakdown,
        "week_breakdown": dict(sorted(week_breakdown.items())),
    }


def cluster_scan(catalog, geometry, frame, versions, *,
                 scenario=TIME_VARYING, replications=DEFAULT_REPLICATIONS,
                 realization_count=DEFAULT_REALIZATIONS,
                 min_events=DEFAULT_MIN_EVENTS, include_time=True,
                 seed=DEFAULT_SEED):
    """组装给主管的完整复核线索报告。"""
    if scenario not in BOUNDARY_SCENARIOS:
        raise ScanError("未知边界情景")
    replications = int(replications)
    realization_count = int(realization_count)
    min_events = int(min_events)
    if replications < 0 or replications > 9999:
        raise ScanError("蒙特卡洛重复次数需在零至九千九百九十九之间")
    if realization_count < 0 or realization_count > 500:
        raise ScanError("定位实现次数需在零至五百之间")
    if min_events < 1:
        raise ScanError("线索最少事件数需为正整数")

    baseline = run_scan(frame, geometry, versions, scenario,
                        replications=replications, min_events=min_events,
                        include_time=include_time, seed=seed)
    scanned_locations = set(baseline["location_ids"])
    no_exposure = sorted(
        location_id for location_id in geometry
        if location_id not in scanned_locations)

    clusters = []
    selected = _select_clusters(baseline["windows"])
    significance_level = 0.05
    rank = 0
    for window in selected:
        p_value = _p_value(window["llr"], baseline["null_maximums"])
        # 没有蒙特卡洛重复就没有搜索校正依据，不产生复核线索
        if p_value is None or p_value > significance_level:
            continue
        rank += 1
        ids = baseline["location_ids"]
        member_ids = [ids[index] for index in sorted(window["members"])]
        anchor = ids[window["center"]]
        points = baseline["points"][list(window["members"])]
        centroid = points.mean(axis=0)
        total_events = baseline["total_events"]
        total_mu = baseline["total_mu"]
        a, b = window["events"], window["expected"]
        outside_rate = (total_events - a) / (total_mu - b) if total_mu > b else None
        relative_risk = (a / b) / outside_rate if outside_rate and outside_rate > 0 else None
        tip = window["week_tip"]
        cluster = {
            "rank": rank,
            "anchor_location_id": anchor,
            "location_ids": member_ids,
            "resolution_mix": _resolution_mix(member_ids, geometry),
            "radius_km": window["radius"],
            "centroid_km": [float(centroid[0]), float(centroid[1])],
            "week_from": frame["weeks"][0],
            "week_to": frame["weeks"][tip] if tip is not None else frame["weeks"][-1],
            "time_window": "全时段空间窗" if tip is None else "截至该履约周的时间前缀",
            "events": int(a),
            "expected": b,
            "observed_rate": a / b if b else None,
            "relative_risk": relative_risk,
            "llr": window["llr"],
            "p_value_search_adjusted": p_value,
            "window": window,
            "members": window["members"],
            "anchor": anchor,
        }
        details = _attach_order_details(cluster, baseline["orders"], ids, frame)
        cluster["exposure"] = len(details["exposure_order_ids"])
        cluster.update(details)
        cluster["geocoding_stability"] = (
            _geocoding_stability(cluster, frame, geometry, versions, scenario,
                                 realization_count, include_time, min_events, seed,
                                 baseline["threshold_95"])
            if realization_count else None)
        cluster["boundary_stability"] = _boundary_stability(
            cluster, frame, geometry, versions, scenario,
            include_time, min_events, seed, baseline["threshold_95"])
        for temporary in ("window", "members", "anchor"):
            cluster.pop(temporary, None)
        clusters.append(cluster)

    radii_evaluated = sorted({window["radius"] for window in baseline["windows"]})
    return {
        "parameters": {
            "scenario": scenario,
            "scenario_label": SCENARIO_LABELS[scenario],
            "stratification": ["species", "fulfillment_week"],
            "spatial_only": not include_time,
            "mc_replications": replications,
            "geocoding_realizations": realization_count,
            "min_events": min_events,
            "max_exposure_fraction": MAX_EXPOSURE_FRACTION,
            "seed": seed,
            "return_action": RETURN_ACTION,
        },
        "counting_units": frame["counting"],
        "strata": frame["strata"],
        "search_space": {
            "scanned_locations": len(scanned_locations),
            "radii_km_evaluated": radii_evaluated,
            "week_prefixes": frame["weeks"] if include_time else [],
            "windows_evaluated": len(baseline["windows"]),
            "significance_level": significance_level,
            "null_llr_quantile_95": baseline["threshold_95"],
            "candidate_windows_before_significance": len(selected),
            "monte_carlo_note": "p 值由同一搜索网格上的分层置换蒙特卡洛产生，已对应扫描全过程的族错误率，而不是挑中窗口的局部检验；仅保留 p<=0.05 的互不重叠线索。",
        },
        "coverage": {
            "locations_with_exposure": sorted(scanned_locations),
            "locations_without_exposure": no_exposure,
            "boundary_excluded_orders_baseline": baseline["excluded_orders"],
            "interpretation": "无完成配送订单的位置没有可见暴露，不参与扫描，也不得解释为无风险；服务边界只是可达范围，不是销量或人口分母。",
        },
        "clusters": clusters,
        "method": {
            "statistic": "分层 Poisson 空间/时空扫描统计量（单侧高值 Kulldorff LLR）",
            "expected_value": "窗口期望退货数由品种×履约周分层基准率间接标准化加总",
            "inference": "分层内置换订单事件标签的蒙特卡洛检验，原假设为各层内退货与位置无关",
            "uncertainty": "街道候选点按权重抽样评估定位敏感性，服务边界按三种情景评估范围敏感性",
            "resolution_limit": "证据只支持街道/路段级区域线索，不支持也不产出任何住户或个人级标签",
        },
    }


def _resolution_mix(member_ids, geometry):
    counts = {}
    for location_id in member_ids:
        resolution = geometry[location_id]["resolution"]
        counts[resolution] = counts.get(resolution, 0) + 1
    return counts


def parse_bool(value):
    if value in ("1", "true", "True", "yes"):
        return True
    if value in ("0", "false", "False", "no"):
        return False
    raise ScanError("布尔参数只接受 1 或 0")
