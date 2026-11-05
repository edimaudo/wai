from pathlib import Path

from app.services.source_analytics import (
    donation_analytics,
    video_analytics,
    crm_analytics,
    constituent_profiles,
)


def source_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


def test_donations_are_calculated_from_gift_csv():
    result = donation_analytics(source_dir())
    assert result["record_counts"]["gifts"] == 3
    assert result["summary"]["total_donations"] == 5300.0
    assert result["summary"]["gift_count"] == 3
    assert result["summary"]["unique_donors"] == 3
    assert result["summary"]["currency"] is None


def test_small_rfm_population_gets_directional_scores_but_no_stable_segment():
    result = donation_analytics(source_dir())
    assert len(result["donor_rfm"]) == 3
    assert all(row["r_score"] is not None and row["f_score"] is not None and row["m_score"] is not None for row in result["donor_rfm"])
    assert all(row["segment"].startswith("Not assigned — sample below") for row in result["donor_rfm"])
    assert result["transaction_rfm_available"] is False


def test_forecast_is_withheld_for_one_month_of_history():
    result = donation_analytics(source_dir())
    assert result["forecast"]["available"] is False
    assert result["forecast"]["history_months"] == 1
    assert result["forecast"]["points"] == []


def test_constituent_join_coverage_uses_exact_ids():
    result = constituent_profiles(source_dir())
    assert result["join_coverage"]["profile_ids"] == 3
    assert result["join_coverage"]["gift_matched_profiles"] == 0
    assert result["join_coverage"]["interaction_matched_profiles"] == 0


def test_video_email_metrics_reflect_recorded_rows_only():
    result = video_analytics(source_dir())
    assert result["record_count"] == 3
    assert result["metrics"]["sent"] == 3
    assert result["metrics"]["opened"] == 0
    assert result["metrics"]["clicks"] == 0
    assert result["metrics"]["video_views"] == 0
    assert result["metrics"]["video_starts"] == 0
    assert result["engagement_segments"] == [{"segment": "No recorded engagement", "count": 3}]
    assert result["quality"]["crm_video_matched_ids"] == 0
    assert result["unique_recipients"] == 3


def test_crm_interactions_are_summarized_from_source_rows():
    result = crm_analytics(source_dir())
    assert result["record_count"] == 3
    assert result["unique_constituents"] == 3
    assert result["matched_profile_ids"] == 0
    assert {row["interaction_type"]: row["count"] for row in result["by_type"]} == {"Phone": 2, "Visit": 1}
    assert result["forecast"]["available"] is False


def test_profile_lifetime_values_are_reported_separately_from_transactions():
    result = donation_analytics(source_dir())
    assert result["profile_lifetime_summary"]["reported_lifetime_giving"] == 238582.58
    assert result["profile_lifetime_summary"]["reported_lifetime_gift_count"] == 99
    assert len(result["profile_rfm"]) == 3
    assert result["profile_rfm_available"] is False
    assert sum(1 for row in result["profile_rfm"] if row["r_score"] is not None) == 2
    assert all(row["segment"].startswith("Not assigned — sample below") for row in result["profile_rfm"] if row["r_score"] is not None)


def test_video_forecasts_are_not_created_from_two_sparse_months():
    result = video_analytics(source_dir())
    assert result["forecast"]["sent"]["available"] is False
    assert result["forecast"]["sent"]["points"] == []


def _write_csv(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_rfm_and_forecasts_activate_when_real_history_is_sufficient(tmp_path):
    # Test-only fixtures verify the calculation path. These records are not
    # inserted into the application or used as reporting data.
    _write_csv(
        tmp_path / "constituent_profile_table.csv",
        "CONSTITUENT_ID,MANAGED_PROSPECT,LIFETIME_GIVING,LIFETIME_GIFT_COUNT,FIRST_GIFT_DATE,LAST_GIFT_DATE,LIFETIME_CRM_INTERACTIONS,FIRST_CRM_INTERACTION_DATE,LAST_CRM_INTERACTION_DATE,CAPACITY_ESTIMATE\n",
    )
    _write_csv(tmp_path / "CRM_interacions_table.csv", "CONSTITUENT_ID,CRM_INTERACTION_DATE,CRM_INTERACTION_TYPE\n")
    gifts = ["CONSTITUENT_ID,GIFT_DATE,AMOUNT"]
    for month in range(1, 13):
        for donor in range(1, 3):
            gifts.append(f"{1000 + donor},2025-{month:02d}-15,{month * 10 + donor}")
    _write_csv(tmp_path / "gift_transactions_table.csv", "\n".join(gifts) + "\n")
    _write_csv(
        tmp_path / "video_email_data_table.csv",
        "CONSTITUENT_ID,CONTACT_ID,SENT_DATE,OPENED_DATE,STARTED_VIDEO_DATE,BOUNCED,UNSUBSCRIBED,CLICKS,VIDEO_VIEWS,STARTED_VIDEO,WATCHED_VIDEO_25_PERCENT,WATCHED_VIDEO_50_PERCENT,WATCHED_VIDEO_75_PERCENT,FINISHED_VIDEO,VIDEO_SHARES_TO_FACEBOOK\n"
        + "".join(
            f"{1000 + donor},{5000 + donor},2025-{month:02d}-15,2025-{month:02d}-16,2025-{month:02d}-17,0,0,1,2,1,1,1,1,1,0\n"
            for month in range(1, 13) for donor in range(1, 3)
        ),
    )

    donations = donation_analytics(tmp_path, as_of=__import__("datetime").date(2026, 1, 1))
    video = video_analytics(tmp_path)
    assert len(donations["donor_rfm"]) == 2
    assert donations["forecast"]["available"] is True
    assert len(donations["forecast"]["points"]) == 3
    assert video["forecast"]["sent"]["available"] is True
    assert crm_analytics(tmp_path)["forecast"]["available"] is False
    assert video["forecast"]["video_started"]["available"] is True
    assert video["metrics"]["open_rate_pct"] == 100.0
    assert video["metrics"]["completion_rate_pct"] == 100.0


def test_contact_only_video_rows_keep_distinct_recipients(tmp_path):
    _write_csv(tmp_path / "constituent_profile_table.csv", "CONSTITUENT_ID\n")
    _write_csv(tmp_path / "gift_transactions_table.csv", "CONSTITUENT_ID,GIFT_DATE,AMOUNT\n")
    _write_csv(tmp_path / "CRM_interacions_table.csv", "CONSTITUENT_ID,CRM_INTERACTION_DATE,CRM_INTERACTION_TYPE\n")
    _write_csv(
        tmp_path / "video_email_data_table.csv",
        "CONSTITUENT_ID,CONTACT_ID,SENT_DATE,OPENED_DATE,BOUNCED,CLICKS,VIDEO_VIEWS,STARTED_VIDEO,FINISHED_VIDEO\n"
        " ,CONTACT-A,2025-01-01,NA,0,0,0,0,0\n"
        " ,CONTACT-B,2025-01-02,NA,0,0,0,0,0\n",
    )
    result = video_analytics(tmp_path)
    assert result["unique_constituents"] == 0
    assert result["unique_recipients"] == 2
    assert {row["recipient_id"] for row in result["recipients"]} == {"CONTACT-A", "CONTACT-B"}


def test_rfm_ties_receive_neutral_scores_when_population_is_large(tmp_path):
    _write_csv(tmp_path / "constituent_profile_table.csv", "CONSTITUENT_ID\n")
    _write_csv(tmp_path / "CRM_interacions_table.csv", "CONSTITUENT_ID,CRM_INTERACTION_DATE,CRM_INTERACTION_TYPE\n")
    _write_csv(tmp_path / "video_email_data_table.csv", "CONSTITUENT_ID,CONTACT_ID,SENT_DATE\n")
    rows = ["CONSTITUENT_ID,GIFT_DATE,AMOUNT"]
    for index in range(20):
        rows.append(f"DONOR-{index:02d},2025-01-15,100")
    _write_csv(tmp_path / "gift_transactions_table.csv", "\n".join(rows) + "\n")
    result = donation_analytics(tmp_path, as_of=__import__("datetime").date(2025, 2, 1))
    assert result["transaction_rfm_available"] is True
    assert all(row["r_score"] == 3 and row["f_score"] == 3 and row["m_score"] == 3 for row in result["donor_rfm"])
    assert all(row["segment"] == "Needs review" for row in result["donor_rfm"])


def test_donation_views_include_yearly_amounts_and_donor_concentration():
    result = donation_analytics(source_dir())
    assert result["annual"] == [{"year": "2024", "amount": 5300.0, "gifts": 3}]
    assert result["donor_value_concentration"]["population"] == 3
    assert result["donor_value_concentration"]["top_10_donor_count"] == 1
    assert result["donor_value_concentration"]["top_10_pct_donors"] is not None


def test_video_monthly_rows_include_rates_and_shares():
    result = video_analytics(source_dir())
    assert len(result["monthly"]) == 43
    assert result["monthly"][0]["month"] == "2017-04"
    assert result["monthly"][0]["open_rate_pct"] == 0.0
    assert result["metrics"]["unsubscribed"] == 0
    assert result["metrics"]["facebook_shares"] == 0
    assert "shares" in result["forecast"]


def test_crm_recipient_history_includes_last_interaction_type():
    result = crm_analytics(source_dir())
    assert {row["last_interaction_type"] for row in result["constituents"]} == {"Phone", "Visit"}
    assert sum(item["constituents"] for item in result["frequency_bands"]) == 3


def test_bounced_video_email_rows_do_not_inflate_engagement_rates(tmp_path):
    _write_csv(tmp_path / "constituent_profile_table.csv", "CONSTITUENT_ID\n")
    _write_csv(tmp_path / "gift_transactions_table.csv", "CONSTITUENT_ID,GIFT_DATE,AMOUNT\n")
    _write_csv(tmp_path / "CRM_interacions_table.csv", "CONSTITUENT_ID,CRM_INTERACTION_DATE,CRM_INTERACTION_TYPE\n")
    _write_csv(
        tmp_path / "video_email_data_table.csv",
        "CONSTITUENT_ID,CONTACT_ID,SENT_DATE,OPENED_DATE,BOUNCED,UNSUBSCRIBED,CLICKS,VIDEO_VIEWS,STARTED_VIDEO,WATCHED_VIDEO_25_PERCENT,WATCHED_VIDEO_50_PERCENT,WATCHED_VIDEO_75_PERCENT,FINISHED_VIDEO,VIDEO_SHARES_TO_FACEBOOK\n"
        "D1,C1,2025-01-01,2025-01-02,1,0,4,3,1,1,1,1,1,2\n"
        "D2,C2,2025-01-01,2025-01-02,0,0,2,1,1,0,0,0,0,1\n",
    )
    result = video_analytics(tmp_path)
    assert result["metrics"]["sent"] == 2
    assert result["metrics"]["delivered"] == 1
    assert result["metrics"]["bounced"] == 1
    assert result["metrics"]["opened"] == 1
    assert result["metrics"]["clicks"] == 2
    assert result["metrics"]["video_views"] == 1
    assert result["metrics"]["video_starts"] == 1
    assert result["metrics"]["facebook_shares"] == 1
    assert result["video_depth"]["25_percent"] == 0
    assert result["video_depth"]["completed"] == 0
    assert result["engagement_segments"] == [
        {"segment": "Bounced / undeliverable", "count": 1},
        {"segment": "Video starter", "count": 1},
    ]
    assert result["recipient_engagement_distribution"]["undeliverable"] == 1


def test_profile_rows_without_constituent_id_are_excluded_and_counted(tmp_path):
    _write_csv(
        tmp_path / "constituent_profile_table.csv",
        "CONSTITUENT_ID,MANAGED_PROSPECT\nD1,1\n,0\nD2,1\n",
    )
    _write_csv(tmp_path / "gift_transactions_table.csv", "CONSTITUENT_ID,GIFT_DATE,AMOUNT\n")
    _write_csv(tmp_path / "CRM_interacions_table.csv", "CONSTITUENT_ID,CRM_INTERACTION_DATE,CRM_INTERACTION_TYPE\n")
    _write_csv(tmp_path / "video_email_data_table.csv", "CONSTITUENT_ID,CONTACT_ID,SENT_DATE\n")
    result = constituent_profiles(tmp_path)
    assert [row["CONSTITUENT_ID"] for row in result["profiles"]] == ["D1", "D2"]
    assert result["invalid_profile_rows"] == 1
    assert result["join_coverage"]["profile_ids"] == 2


def test_non_numeric_donation_amount_is_excluded_as_invalid(tmp_path):
    _write_csv(tmp_path / "constituent_profile_table.csv", "CONSTITUENT_ID\nD1\n")
    _write_csv(tmp_path / "gift_transactions_table.csv",
               "CONSTITUENT_ID,GIFT_DATE,AMOUNT\nD1,2025-01-01,100\nD1,2025-01-02,unknown\n")
    _write_csv(tmp_path / "CRM_interacions_table.csv", "CONSTITUENT_ID,CRM_INTERACTION_DATE,CRM_INTERACTION_TYPE\n")
    _write_csv(tmp_path / "video_email_data_table.csv", "CONSTITUENT_ID,CONTACT_ID,SENT_DATE\n")
    result = donation_analytics(tmp_path)
    assert result["summary"]["gift_count"] == 1
    assert result["summary"]["total_donations"] == 100.0
    assert result["quality"]["invalid_gift_rows"] == 1
