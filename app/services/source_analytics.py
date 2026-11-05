"""Read-only analytics over Wai's four approved source CSV files.

The CSVs remain the source of truth and are never copied into PostgreSQL.
The service preserves identifier strings and reports join coverage explicitly.
"""
from __future__ import annotations

import csv
import math
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

SOURCE_FILES = {
    "constituents": "constituent_profile_table.csv",
    "gifts": "gift_transactions_table.csv",
    "interactions": "CRM_interacions_table.csv",
    "video": "video_email_data_table.csv",
}


def _read_csv(data_dir: Path, filename: str) -> tuple[list[dict[str, str]], list[str]]:
    path = data_dir / filename
    if not path.is_file():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        rows = []
        for raw in reader:
            rows.append({str(k): (v or "").strip() for k, v in raw.items() if k is not None})
    return rows, fields


def _date(value: str) -> date | None:
    value = (value or "").strip()
    if not value or value.upper() in {"NA", "N/A", "NULL", "NONE"}:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _optional_number(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in {"NA", "N/A", "NULL", "NONE"}:
        return None
    try:
        result = float(text)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _number(value: str | int | float | None) -> float:
    result = _optional_number(value)
    return result if result is not None else 0.0


def _flag(value: str | int | float | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _month(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _quarter(day: date) -> str:
    return f"{day.year}-Q{((day.month - 1) // 3) + 1}"


def load_source_data(data_dir: Path) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for key, filename in SOURCE_FILES.items():
        rows, fields = _read_csv(data_dir, filename)
        files[key] = {"filename": filename, "rows": rows, "fields": fields, "count": len(rows), "available": bool(fields)}
    return files


def _linear_forecast(monthly: list[dict[str, Any]], periods: int = 3) -> dict[str, Any]:
    """Simple transparent trend projection; never fabricate a forecast on sparse history."""
    non_empty = [point for point in monthly if point["amount"] > 0]
    if len(monthly) < 6 or len(non_empty) < 4:
        return {
            "available": False,
            "reason": "At least six calendar months and four months with recorded activity are required. The available source history is too limited for a responsible forecast.",
            "history_months": len(monthly),
            "active_months": len(non_empty),
            "points": [],
            "method": "Not calculated",
        }
    values = [float(point["amount"]) for point in monthly]
    n = len(values)
    xs = list(range(n))
    xbar = mean(xs)
    ybar = mean(values)
    denominator = sum((x - xbar) ** 2 for x in xs)
    slope = sum((x - xbar) * (y - ybar) for x, y in zip(xs, values)) / denominator if denominator else 0.0
    intercept = ybar - slope * xbar
    residuals = [y - (intercept + slope * x) for x, y in zip(xs, values)]
    rmse = math.sqrt(sum(r * r for r in residuals) / max(1, n - 2))
    last_year, last_month = map(int, monthly[-1]["month"].split("-"))
    points = []
    for step in range(1, periods + 1):
        month_index = last_month - 1 + step
        year = last_year + month_index // 12
        month = month_index % 12 + 1
        x = n - 1 + step
        estimate = max(0.0, intercept + slope * x)
        points.append({
            "month": f"{year:04d}-{month:02d}",
            "estimate": round(estimate, 2),
            "lower": round(max(0.0, estimate - 1.28 * rmse), 2),
            "upper": round(max(0.0, estimate + 1.28 * rmse), 2),
        })
    return {
        "available": True,
        "reason": "Directional linear trend projection based on recorded monthly donations. It is not a budget or fundraising commitment.",
        "history_months": n,
        "active_months": len(non_empty),
        "points": points,
        "method": "Linear trend with an 80% approximate residual band",
    }



def _apply_rfm_scores(records: list[dict[str, Any]], minimum_population: int = 20) -> bool:
    """Add directional R/F/M scores for every complete record.

    For small populations the component scores are still useful as a within-file
    ranking, but categorical segments are deliberately withheld until the population
    reaches ``minimum_population``. This avoids both suppressing the requested RFM
    analysis and presenting unstable segment labels as statistically reliable.
    """
    complete = [row for row in records if row.get("recency_days") is not None
                and row.get("frequency") is not None and row.get("monetary") is not None]
    if not complete:
        for row in records:
            row.update({"r_score": None, "f_score": None, "m_score": None, "rfm_score": None,
                        "segment": "Incomplete RFM source fields"})
        return False

    def percentile_score(value: float, values: list[float], lower_is_better: bool = False) -> int:
        n = len(values)
        if n <= 1:
            return 3
        if lower_is_better:
            better = sum(1 for candidate in values if candidate < value)
        else:
            better = sum(1 for candidate in values if candidate > value)
        equal = sum(1 for candidate in values if candidate == value)
        midrank_from_best = better + (equal + 1) / 2
        percentile = 1 - ((midrank_from_best - 1) / (n - 1))
        return max(1, min(5, int(round(1 + percentile * 4))))

    recencies = [float(row["recency_days"]) for row in complete]
    frequencies = [float(row["frequency"]) for row in complete]
    monetaries = [float(row["monetary"]) for row in complete]
    population_is_large_enough = len(complete) >= minimum_population
    for row in records:
        if row not in complete:
            row.update({"r_score": None, "f_score": None, "m_score": None, "rfm_score": None,
                        "segment": "Incomplete RFM source fields"})
            continue
        row["r_score"] = percentile_score(float(row["recency_days"]), recencies, lower_is_better=True)
        row["f_score"] = percentile_score(float(row["frequency"]), frequencies)
        row["m_score"] = percentile_score(float(row["monetary"]), monetaries)
        row["rfm_score"] = row["r_score"] + row["f_score"] + row["m_score"]
        if not population_is_large_enough:
            row["segment"] = f"Not assigned — sample below {minimum_population} complete records"
        elif row["r_score"] >= 4 and row["f_score"] >= 4 and row["m_score"] >= 4:
            row["segment"] = "Champions"
        elif row["r_score"] >= 4 and row["f_score"] >= 3:
            row["segment"] = "Loyal donors"
        elif row["r_score"] <= 2 and row["f_score"] >= 3:
            row["segment"] = "At risk"
        elif row["r_score"] >= 4 and row["f_score"] <= 2:
            row["segment"] = "Recent donors"
        elif row["m_score"] >= 4:
            row["segment"] = "High monetary value"
        else:
            row["segment"] = "Needs review"
    return population_is_large_enough

def donation_analytics(data_dir: Path, as_of: date | None = None) -> dict[str, Any]:
    files = load_source_data(data_dir)
    gift_rows = files["gifts"]["rows"]
    profile_rows = files["constituents"]["rows"]
    interaction_rows = files["interactions"]["rows"]
    as_of = as_of or date.today()

    gifts: list[dict[str, Any]] = []
    invalid_gifts = 0
    by_donor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    monthly_amounts: dict[str, float] = defaultdict(float)
    monthly_counts: Counter[str] = Counter()
    monthly_donors: dict[str, set[str]] = defaultdict(set)
    quarterly_amounts: dict[str, float] = defaultdict(float)
    quarterly_counts: Counter[str] = Counter()
    annual_amounts: dict[str, float] = defaultdict(float)
    annual_counts: Counter[str] = Counter()
    amount_bands = {"under_100": 0, "100_to_499": 0, "500_to_999": 0, "1000_plus": 0}
    for row in gift_rows:
        constituent_id = str(row.get("CONSTITUENT_ID", "")).strip()
        gift_date = _date(row.get("GIFT_DATE", ""))
        amount = _optional_number(row.get("AMOUNT"))
        if not constituent_id or not gift_date or amount is None or amount < 0:
            invalid_gifts += 1
            continue
        item = {"constituent_id": constituent_id, "date": gift_date, "amount": amount}
        gifts.append(item)
        by_donor[constituent_id].append(item)
        month = _month(gift_date)
        monthly_amounts[month] += amount
        monthly_counts[month] += 1
        monthly_donors[month].add(constituent_id)
        quarter = _quarter(gift_date)
        quarterly_amounts[quarter] += amount
        quarterly_counts[quarter] += 1
        annual = str(gift_date.year)
        annual_amounts[annual] += amount
        annual_counts[annual] += 1
        if amount < 100:
            amount_bands["under_100"] += 1
        elif amount < 500:
            amount_bands["100_to_499"] += 1
        elif amount < 1000:
            amount_bands["500_to_999"] += 1
        else:
            amount_bands["1000_plus"] += 1

    donor_rows = []
    for constituent_id, transactions in by_donor.items():
        dates = [x["date"] for x in transactions]
        amounts = [x["amount"] for x in transactions]
        donor_rows.append({
            "constituent_id": constituent_id,
            "recency_days": max(0, (as_of - max(dates)).days),
            "frequency": len(transactions),
            "monetary": round(sum(amounts), 2),
            "average_gift": round(mean(amounts), 2),
            "first_gift": min(dates).isoformat(),
            "last_gift": max(dates).isoformat(),
        })
    donor_rows.sort(key=lambda x: (-x["monetary"], x["constituent_id"]))
    frequency_distribution = Counter(str(row["frequency"]) for row in donor_rows)
    donor_type_counts = {
        "one_time_donors": sum(1 for row in donor_rows if row["frequency"] == 1),
        "repeat_donors": sum(1 for row in donor_rows if row["frequency"] > 1),
    }
    donor_value_order = sorted(donor_rows, key=lambda row: row["monetary"], reverse=True)
    total_transaction_value = sum(row["monetary"] for row in donor_value_order)
    top_10_count = max(1, math.ceil(len(donor_value_order) * 0.10)) if donor_value_order else 0
    top_25_count = max(1, math.ceil(len(donor_value_order) * 0.25)) if donor_value_order else 0
    donor_value_concentration = {
        "top_10_pct_donors": round(sum(row["monetary"] for row in donor_value_order[:top_10_count]) / total_transaction_value * 100, 1) if total_transaction_value else None,
        "top_25_pct_donors": round(sum(row["monetary"] for row in donor_value_order[:top_25_count]) / total_transaction_value * 100, 1) if total_transaction_value else None,
        "top_10_donor_count": top_10_count,
        "top_25_donor_count": top_25_count,
        "population": len(donor_value_order),
        "note": "Concentration is calculated from the gift transaction extract only; it is not blended with lifetime profile summaries.",
    }

    # RFM measures are calculated for individual gift transactions. Percentile
    # segments are withheld unless at least 20 complete constituent histories exist.
    transaction_rfm_available = _apply_rfm_scores(donor_rows)

    # The profile extract also contains lifetime giving, lifetime gift count and
    # last-gift date. Keep this as a separate source-derived RFM view; never mix
    # it with the transaction-level gift file or claim the IDs are reconciled.
    profile_rfm = []
    for row in profile_rows:
        constituent_id = str(row.get("CONSTITUENT_ID", "")).strip()
        if not constituent_id:
            continue
        last_gift = _date(row.get("LAST_GIFT_DATE", ""))
        lifetime_giving_raw = str(row.get("LIFETIME_GIVING", "")).strip()
        lifetime_count_raw = str(row.get("LIFETIME_GIFT_COUNT", "")).strip()
        profile_rfm.append({
            "constituent_id": constituent_id,
            "recency_days": max(0, (as_of - last_gift).days) if last_gift else None,
            "frequency": int(_number(lifetime_count_raw)) if lifetime_count_raw else None,
            "monetary": round(_number(lifetime_giving_raw), 2) if lifetime_giving_raw else None,
            "first_gift": row.get("FIRST_GIFT_DATE") or None,
            "last_gift": last_gift.isoformat() if last_gift else None,
            "basis": "Lifetime values supplied by constituent profile CSV",
        })
    profile_rfm_available = _apply_rfm_scores(profile_rfm)
    lifetime_values = [_number(row.get("LIFETIME_GIVING")) for row in profile_rows
                       if str(row.get("LIFETIME_GIVING", "")).strip()]
    lifetime_counts = [_number(row.get("LIFETIME_GIFT_COUNT")) for row in profile_rows
                       if str(row.get("LIFETIME_GIFT_COUNT", "")).strip()]
    profile_lifetime_summary = {
        "profiles_with_lifetime_giving": len(lifetime_values),
        "reported_lifetime_giving": round(sum(lifetime_values), 2) if lifetime_values else None,
        "profiles_with_lifetime_gift_count": len(lifetime_counts),
        "reported_lifetime_gift_count": int(sum(lifetime_counts)) if lifetime_counts else None,
        "currency": None,
        "note": "Aggregated source-provided lifetime fields from the constituent profile file. Kept separate from transaction totals to avoid double-counting.",
    }

    # Fill calendar gaps with explicit zero-gift months so forecast eligibility
    # reflects the actual history window rather than only active donation months.
    if gifts:
        first_day = min(x["date"] for x in gifts)
        last_day = max(x["date"] for x in gifts)
        year, month = first_day.year, first_day.month
        months = []
        while (year, month) <= (last_day.year, last_day.month):
            months.append(f"{year:04d}-{month:02d}")
            month += 1
            if month == 13:
                year += 1
                month = 1
    else:
        months = []
    monthly = [{"month": month, "amount": round(monthly_amounts[month], 2), "gifts": monthly_counts[month],
                "unique_donors": len(monthly_donors[month]),
                "average_gift": round(monthly_amounts[month] / monthly_counts[month], 2) if monthly_counts[month] else None}
               for month in months]
    quarters = [{"quarter": q, "amount": round(quarterly_amounts[q], 2), "gifts": quarterly_counts[q]}
                for q in sorted(quarterly_amounts)]
    annual = [{"year": year, "amount": round(annual_amounts[year], 2), "gifts": annual_counts[year]}
              for year in sorted(annual_amounts)]
    profile_ids = {str(row.get("CONSTITUENT_ID", "")).strip() for row in profile_rows if row.get("CONSTITUENT_ID")}
    gift_ids = set(by_donor)
    interaction_ids = {str(row.get("CONSTITUENT_ID", "")).strip() for row in interaction_rows if row.get("CONSTITUENT_ID")}
    matched_profiles = profile_ids & gift_ids

    return {
        "source": files["gifts"]["filename"],
        "profile_source": files["constituents"]["filename"],
        "interaction_source": files["interactions"]["filename"],
        "record_counts": {"profiles": len(profile_rows), "gifts": len(gifts), "interactions": len(interaction_rows)},
        "quality": {
            "invalid_gift_rows": invalid_gifts,
            "profile_ids": len(profile_ids),
            "gift_constituents": len(gift_ids),
            "matched_profile_and_gift_ids": len(matched_profiles),
            "profiles_without_gift_match": len(profile_ids - gift_ids),
            "gift_ids_without_profile": len(gift_ids - profile_ids),
            "interaction_ids": len(interaction_ids),
            "profile_gift_join_rate_pct": round(len(matched_profiles) / len(profile_ids) * 100, 1) if profile_ids else None,
        },
        "summary": {
            "total_donations": round(sum(x["amount"] for x in gifts), 2),
            "gift_count": len(gifts),
            "unique_donors": len(by_donor),
            "average_gift": round(mean([x["amount"] for x in gifts]), 2) if gifts else None,
            "median_gift": round(median([x["amount"] for x in gifts]), 2) if gifts else None,
            "largest_gift": max((x["amount"] for x in gifts), default=None),
            "first_gift_date": min((x["date"] for x in gifts), default=None).isoformat() if gifts else None,
            "last_gift_date": max((x["date"] for x in gifts), default=None).isoformat() if gifts else None,
            "currency": None,
        },
        "donor_rfm": donor_rows,
        "transaction_rfm_available": transaction_rfm_available,
        "transaction_rfm_complete_count": sum(1 for row in donor_rows if row.get("rfm_score") is not None),
        "profile_rfm": profile_rfm,
        "profile_rfm_available": profile_rfm_available,
        "profile_rfm_complete_count": sum(1 for row in profile_rfm if row.get("rfm_score") is not None),
        "profile_lifetime_summary": profile_lifetime_summary,
        "amount_bands": amount_bands,
        "donor_value_concentration": donor_value_concentration,
        "monthly": monthly,
        "quarterly": quarters,
        "annual": annual,
        "forecast": _linear_forecast(monthly),
        "gift_count_forecast": _linear_forecast([{"month": point["month"], "amount": point["gifts"]} for point in monthly]),
        "frequency_distribution": [{"gift_frequency": int(key), "donors": value} for key, value in sorted(frequency_distribution.items(), key=lambda x: int(x[0]))],
        "donor_type_counts": donor_type_counts,
        "insight_note": "Amounts are shown in source units. The CSV schema does not specify currency; Wai does not label these values as CAD.",
    }


def video_analytics(data_dir: Path) -> dict[str, Any]:
    files = load_source_data(data_dir)
    rows = files["video"]["rows"]
    interactions = files["interactions"]["rows"]
    profile_rows = files["constituents"]["rows"]
    # Each row represents a dated email/video engagement event. Rows without
    # a valid send date or recipient identifier are excluded from rate metrics.
    valid = [
        row for row in rows
        if _date(row.get("SENT_DATE", "")) is not None
        and (str(row.get("CONSTITUENT_ID", "")).strip() or str(row.get("CONTACT_ID", "")).strip())
    ]
    invalid_rows = len(rows) - len(valid)
    sent = len(valid)
    delivered = sum(1 for row in valid if not _flag(row.get("BOUNCED")))
    opened = sum(1 for row in valid if not _flag(row.get("BOUNCED")) and _date(row.get("OPENED_DATE")) is not None)
    clicked_records = sum(1 for row in valid if not _flag(row.get("BOUNCED")) and _number(row.get("CLICKS")) > 0)
    started = sum(1 for row in valid if not _flag(row.get("BOUNCED")) and (_flag(row.get("STARTED_VIDEO")) or _number(row.get("VIDEO_VIEWS")) > 0 or _date(row.get("STARTED_VIDEO_DATE")) is not None))
    views = sum(_number(row.get("VIDEO_VIEWS")) for row in valid if not _flag(row.get("BOUNCED")))
    clicks = sum(_number(row.get("CLICKS")) for row in valid if not _flag(row.get("BOUNCED")))
    finished = sum(1 for row in valid if not _flag(row.get("BOUNCED")) and _flag(row.get("FINISHED_VIDEO")))
    levels = {
        "25_percent": sum(1 for row in valid if not _flag(row.get("BOUNCED")) and _flag(row.get("WATCHED_VIDEO_25_PERCENT"))),
        "50_percent": sum(1 for row in valid if not _flag(row.get("BOUNCED")) and _flag(row.get("WATCHED_VIDEO_50_PERCENT"))),
        "75_percent": sum(1 for row in valid if not _flag(row.get("BOUNCED")) and _flag(row.get("WATCHED_VIDEO_75_PERCENT"))),
        "completed": finished,
    }
    unsubscribed = sum(1 for row in valid if not _flag(row.get("BOUNCED")) and _flag(row.get("UNSUBSCRIBED")))
    bounced = sum(1 for row in valid if _flag(row.get("BOUNCED")))
    shares = sum(_number(row.get("VIDEO_SHARES_TO_FACEBOOK")) for row in valid if not _flag(row.get("BOUNCED")))
    by_recipient: dict[str, dict[str, Any]] = {}
    by_month: dict[str, dict[str, int]] = defaultdict(lambda: {"sent": 0, "delivered": 0, "opened": 0, "clicked": 0, "video_started": 0, "completed": 0, "views": 0, "unsubscribed": 0, "bounced": 0, "shares": 0})
    for row in valid:
        constituent_id = str(row.get("CONSTITUENT_ID", "")).strip()
        contact_id = str(row.get("CONTACT_ID", "")).strip()
        # Do not collapse all contact-only records into a single blank constituent.
        recipient_key = f"constituent:{constituent_id}" if constituent_id else f"contact:{contact_id}"
        display_id = constituent_id or contact_id
        id_type = "constituent" if constituent_id else "contact"
        item = by_recipient.setdefault(recipient_key, {
            "recipient_id": display_id, "id_type": id_type, "constituent_id": constituent_id or None,
            "sent": 0, "delivered": 0, "opened": 0, "clicks": 0, "video_views": 0,
            "started": 0, "completed": 0, "shares": 0, "unsubscribed": 0, "bounced": 0,
        })
        is_bounced = _flag(row.get("BOUNCED"))
        is_opened = not is_bounced and _date(row.get("OPENED_DATE")) is not None
        has_click = not is_bounced and _number(row.get("CLICKS")) > 0
        has_started = not is_bounced and (_flag(row.get("STARTED_VIDEO")) or _number(row.get("VIDEO_VIEWS")) > 0 or _date(row.get("STARTED_VIDEO_DATE")) is not None)
        has_completed = not is_bounced and _flag(row.get("FINISHED_VIDEO"))
        item["sent"] += 1
        item["delivered"] += int(not is_bounced)
        item["opened"] += int(is_opened)
        item["clicks"] += int(_number(row.get("CLICKS")))
        item["video_views"] += int(_number(row.get("VIDEO_VIEWS"))) if not is_bounced else 0
        item["started"] += int(has_started)
        item["completed"] += int(has_completed)
        item["shares"] += int(_number(row.get("VIDEO_SHARES_TO_FACEBOOK"))) if not is_bounced else 0
        item["unsubscribed"] += int(_flag(row.get("UNSUBSCRIBED")) and not is_bounced)
        item["bounced"] += int(is_bounced)

        sent_date = _date(row.get("SENT_DATE"))
        month = _month(sent_date)
        by_month[month]["sent"] += 1
        by_month[month]["delivered"] += int(not is_bounced)
        by_month[month]["opened"] += int(is_opened)
        by_month[month]["clicked"] += int(has_click)
        by_month[month]["video_started"] += int(has_started)
        by_month[month]["completed"] += int(has_completed)
        by_month[month]["views"] += int(_number(row.get("VIDEO_VIEWS"))) if not is_bounced else 0
        by_month[month]["unsubscribed"] += int(_flag(row.get("UNSUBSCRIBED")) and not is_bounced)
        by_month[month]["bounced"] += int(is_bounced)
        by_month[month]["shares"] += int(_number(row.get("VIDEO_SHARES_TO_FACEBOOK"))) if not is_bounced else 0

    segments = Counter()
    for item in by_recipient.values():
        if item["delivered"] == 0:
            segment = "Bounced / undeliverable"
        elif item["completed"]:
            segment = "Video completer"
        elif item["started"]:
            segment = "Video starter"
        elif item["clicks"]:
            segment = "Email clicker"
        elif item["opened"]:
            segment = "Email opener"
        else:
            segment = "No recorded engagement"
        item["engagement_segment"] = segment
        segments[segment] += 1

    months = sorted(by_month)
    if months:
        first_year, first_month = map(int, months[0].split("-"))
        last_year, last_month = map(int, months[-1].split("-"))
        all_months = []
        year, month = first_year, first_month
        while (year, month) <= (last_year, last_month):
            all_months.append(f"{year:04d}-{month:02d}")
            month += 1
            if month == 13:
                year += 1
                month = 1
        monthly_rows = [{"month": key, **by_month.get(key, {"sent": 0, "delivered": 0, "opened": 0, "clicked": 0, "video_started": 0, "completed": 0, "views": 0, "unsubscribed": 0, "bounced": 0, "shares": 0})} for key in all_months]
        for point in monthly_rows:
            delivered_month = point["delivered"]
            started_month = point["video_started"]
            point["open_rate_pct"] = round(point["opened"] / delivered_month * 100, 1) if delivered_month else None
            point["click_rate_pct"] = round(point["clicked"] / delivered_month * 100, 1) if delivered_month else None
            point["video_start_rate_pct"] = round(started_month / delivered_month * 100, 1) if delivered_month else None
            point["video_completion_rate_pct"] = round(point["completed"] / started_month * 100, 1) if started_month else None
    else:
        monthly_rows = []

    engagement_forecasts = {}
    for metric_key in ("sent", "delivered", "opened", "clicked", "video_started", "completed", "views", "unsubscribed", "bounced", "shares"):
        forecast_input = [{"month": row["month"], "amount": row[metric_key]} for row in monthly_rows]
        engagement_forecasts[metric_key] = _linear_forecast(forecast_input)

    video_constituent_ids = {str(r.get("CONSTITUENT_ID", "")).strip() for r in valid if r.get("CONSTITUENT_ID")}
    profile_constituent_ids = {str(r.get("CONSTITUENT_ID", "")).strip() for r in profile_rows if r.get("CONSTITUENT_ID")}
    crm_constituent_ids = {str(r.get("CONSTITUENT_ID", "")).strip() for r in interactions if r.get("CONSTITUENT_ID")}
    return {
        "source": files["video"]["filename"],
        "record_count": sent,
        "invalid_rows": invalid_rows,
        "unique_constituents": len(video_constituent_ids),
        "unique_recipients": len(by_recipient),
        "metrics": {
            "sent": sent,
            "delivered": delivered,
            "bounced": bounced,
            "opened": opened,
            "open_rate_pct": round(opened / delivered * 100, 1) if delivered else None,
            "clicked_records": clicked_records,
            "click_rate_pct": round(clicked_records / delivered * 100, 1) if delivered else None,
            "clicks": int(clicks),
            "video_starts": started,
            "video_start_rate_pct": round(started / delivered * 100, 1) if delivered else None,
            "video_views": int(views),
            "completion_rate_pct": round(finished / started * 100, 1) if started else None,
            "unsubscribed": unsubscribed,
            "unsubscribe_rate_pct": round(unsubscribed / delivered * 100, 1) if delivered else None,
            "facebook_shares": int(shares),
            "bounce_rate_pct": round(bounced / sent * 100, 1) if sent else None,
        },
        "funnel": [
            {"stage": "Sent", "count": sent},
            {"stage": "Delivered", "count": delivered},
            {"stage": "Opened", "count": opened},
            {"stage": "Clicked", "count": clicked_records},
            {"stage": "Video started", "count": started},
            {"stage": "Video completed", "count": finished},
        ],
        "video_depth": levels,
        "engagement_segments": [{"segment": key, "count": value} for key, value in sorted(segments.items())],
        "recipients": sorted(by_recipient.values(), key=lambda x: (-(x["completed"]), -(x["started"]), x["recipient_id"])),
        "recipient_engagement_distribution": {
            "total": len(by_recipient),
            "engaged": sum(1 for row in by_recipient.values() if row["opened"] or row["clicks"] or row["started"] or row["completed"]),
            "video_engaged": sum(1 for row in by_recipient.values() if row["started"] or row["completed"]),
            "email_only": sum(1 for row in by_recipient.values() if (row["opened"] or row["clicks"]) and not (row["started"] or row["completed"])),
            "no_recorded_engagement": sum(1 for row in by_recipient.values() if row["delivered"] > 0 and not (row["opened"] or row["clicks"] or row["started"] or row["completed"])),
            "undeliverable": sum(1 for row in by_recipient.values() if row["delivered"] == 0),
        },
        "monthly": monthly_rows,
        "forecast": engagement_forecasts,
        "quality": {
            "crm_interaction_rows": len(interactions),
            "video_constituent_ids": len(video_constituent_ids),
            "crm_video_matched_ids": len(video_constituent_ids & crm_constituent_ids),
            "profile_video_matched_ids": len(video_constituent_ids & profile_constituent_ids),
            "video_ids_without_profile": len(video_constituent_ids - profile_constituent_ids),
            "invalid_video_rows": invalid_rows,
        },
        "interpretation": "Email and video metrics are calculated from valid dated engagement event rows. Contact IDs are retained separately when constituent IDs are absent. Missing open dates are not treated as verified non-opens in individual attribution beyond the event-row denominator.",
    }


def crm_analytics(data_dir: Path) -> dict[str, Any]:
    files = load_source_data(data_dir)
    rows = files["interactions"]["rows"]
    profiles = files["constituents"]["rows"]
    valid = []
    invalid_rows = 0
    by_constituent: dict[str, int] = Counter()
    by_type: Counter[str] = Counter()
    by_month: Counter[str] = Counter()
    last_by_constituent: dict[str, date] = {}
    last_type_by_constituent: dict[str, str] = {}
    for row in rows:
        constituent_id = str(row.get("CONSTITUENT_ID", "")).strip()
        interaction_date = _date(row.get("CRM_INTERACTION_DATE", ""))
        interaction_type = str(row.get("CRM_INTERACTION_TYPE", "")).strip() or "Not specified"
        if not constituent_id or not interaction_date:
            invalid_rows += 1
            continue
        valid.append(row)
        by_constituent[constituent_id] += 1
        by_type[interaction_type] += 1
        by_month[_month(interaction_date)] += 1
        if constituent_id not in last_by_constituent or interaction_date > last_by_constituent[constituent_id]:
            last_by_constituent[constituent_id] = interaction_date
            last_type_by_constituent[constituent_id] = interaction_type
    profile_ids = {str(row.get("CONSTITUENT_ID", "")).strip() for row in profiles if row.get("CONSTITUENT_ID")}
    interaction_ids = set(by_constituent)
    months = sorted(by_month)
    monthly = []
    if months:
        start_year, start_month = map(int, months[0].split("-"))
        end_year, end_month = map(int, months[-1].split("-"))
        year, month = start_year, start_month
        while (year, month) <= (end_year, end_month):
            key = f"{year:04d}-{month:02d}"
            monthly.append({"month": key, "interactions": by_month[key]})
            month += 1
            if month == 13:
                year += 1
                month = 1
    forecast = _linear_forecast([{"month": row["month"], "amount": row["interactions"]} for row in monthly])
    by_donor = [
        {"constituent_id": key, "interaction_count": count,
         "last_interaction": last_by_constituent[key].isoformat(),
         "last_interaction_type": last_type_by_constituent.get(key, "Not specified")}
        for key, count in by_constituent.items()
    ]
    frequency_bands = Counter(
        "1 interaction" if count == 1 else "2–4 interactions" if count <= 4 else "5+ interactions"
        for count in by_constituent.values()
    )
    return {
        "source": files["interactions"]["filename"],
        "record_count": len(valid),
        "invalid_rows": invalid_rows,
        "unique_constituents": len(interaction_ids),
        "matched_profile_ids": len(interaction_ids & profile_ids),
        "unmatched_interaction_ids": len(interaction_ids - profile_ids),
        "by_type": [{"interaction_type": key, "count": value} for key, value in sorted(by_type.items(), key=lambda x: (-x[1], x[0].lower()))],
        "frequency_bands": [{"band": key, "constituents": value} for key, value in frequency_bands.items()],
        "monthly": monthly,
        "forecast": forecast,
        "constituents": sorted(by_donor, key=lambda x: (-x["interaction_count"], x["constituent_id"])),
    }


def constituent_profiles(data_dir: Path) -> dict[str, Any]:
    files = load_source_data(data_dir)
    profiles = files["constituents"]["rows"]
    gifts = files["gifts"]["rows"]
    interactions = files["interactions"]["rows"]
    video_rows = files["video"]["rows"]
    rfm = donation_analytics(data_dir)
    rfm_by_id = {row["constituent_id"]: row for row in rfm["profile_rfm"]}
    gift_counts = Counter(str(row.get("CONSTITUENT_ID", "")).strip() for row in gifts)
    interaction_counts = Counter(str(row.get("CONSTITUENT_ID", "")).strip() for row in interactions)
    latest_interaction: dict[str, date] = {}
    latest_interaction_type: dict[str, str] = {}
    for row in interactions:
        cid = str(row.get("CONSTITUENT_ID", "")).strip()
        dt = _date(row.get("CRM_INTERACTION_DATE", ""))
        if cid and dt and (cid not in latest_interaction or dt > latest_interaction[cid]):
            latest_interaction[cid] = dt
            latest_interaction_type[cid] = str(row.get("CRM_INTERACTION_TYPE", "")).strip() or "Not specified"
    video_counts: Counter[str] = Counter()
    email_open_counts: Counter[str] = Counter()
    email_click_counts: Counter[str] = Counter()
    video_view_counts: Counter[str] = Counter()
    video_completion_counts: Counter[str] = Counter()
    latest_video_sent: dict[str, date] = {}
    for row in video_rows:
        cid = str(row.get("CONSTITUENT_ID", "")).strip()
        if not cid:
            continue
        video_counts[cid] += 1
        email_open_counts[cid] += int(_date(row.get("OPENED_DATE", "")) is not None)
        email_click_counts[cid] += int(_number(row.get("CLICKS")))
        video_view_counts[cid] += int(_number(row.get("VIDEO_VIEWS")))
        video_completion_counts[cid] += int(_flag(row.get("FINISHED_VIDEO")))
        sent_date = _date(row.get("SENT_DATE", ""))
        if sent_date and (cid not in latest_video_sent or sent_date > latest_video_sent[cid]):
            latest_video_sent[cid] = sent_date
    output = []
    invalid_profile_rows = 0
    for row in profiles:
        cid = str(row.get("CONSTITUENT_ID", "")).strip()
        if not cid:
            invalid_profile_rows += 1
            continue
        output.append({
            **row,
            "GIFT_TRANSACTION_ROWS": gift_counts[cid],
            "CRM_INTERACTION_ROWS": interaction_counts[cid],
            "LAST_SOURCE_INTERACTION": latest_interaction[cid].isoformat() if cid in latest_interaction else None,
            "LAST_SOURCE_INTERACTION_TYPE": latest_interaction_type.get(cid),
            "VIDEO_EMAIL_ROWS": video_counts[cid],
            "EMAIL_OPENS": email_open_counts[cid],
            "EMAIL_CLICKS": email_click_counts[cid],
            "VIDEO_VIEWS": video_view_counts[cid],
            "VIDEO_COMPLETIONS": video_completion_counts[cid],
            "LAST_VIDEO_SENT": latest_video_sent[cid].isoformat() if cid in latest_video_sent else None,
            "RFM": rfm_by_id.get(cid),
        })
    managed_counts = Counter(
        "Managed prospect" if str(row.get("MANAGED_PROSPECT", "")).strip() == "1"
        else "Not managed prospect" if str(row.get("MANAGED_PROSPECT", "")).strip() == "0"
        else "Managed status not supplied"
        for row in profiles
    )
    capacity_counts = Counter(
        str(row.get("CAPACITY_ESTIMATE", "")).strip() or "Capacity not supplied"
        for row in profiles
    )
    return {
        "profiles": sorted(output, key=lambda x: x["CONSTITUENT_ID"]),
        "source_counts": {key: files[key]["count"] for key in files},
        "invalid_profile_rows": invalid_profile_rows,
        "profile_segments": {
            "managed_status": [{"segment": key, "count": value} for key, value in sorted(managed_counts.items())],
            "source_capacity_bands": [{"segment": key, "count": value} for key, value in sorted(capacity_counts.items())],
            "note": "Segments use only the source-provided MANAGED_PROSPECT and CAPACITY_ESTIMATE fields. Capacity is not independently estimated by Wai.",
        },
        "join_coverage": {
            "profile_ids": len({x["CONSTITUENT_ID"] for x in output}),
            "gift_matched_profiles": sum(1 for x in output if x["GIFT_TRANSACTION_ROWS"]),
            "interaction_matched_profiles": sum(1 for x in output if x["CRM_INTERACTION_ROWS"]),
            "video_matched_profiles": sum(1 for x in output if x["VIDEO_EMAIL_ROWS"]),
        },
    }
