from pathlib import Path

import pytest

from app.services.csv_ingestion import (
    _normalize_row,
    identify_dataset,
    scan_data_folder,
)


def test_supplied_crm_filename_spelling_is_recognized():
    assert identify_dataset(Path("CRM_interacions_table.csv")) == "crm_interactions"
    assert identify_dataset(Path("CRM_interactions_table.csv")) == "crm_interactions"


def test_gift_filename_is_recognized():
    assert identify_dataset(Path("gift_transactions_table.csv")) == "gift_transactions"


def test_video_filename_is_recognized():
    assert identify_dataset(Path("video_email_data_table.csv")) == "video_email_events"


def test_gift_row_normalizes_date_and_amount():
    row = _normalize_row(
        "gift_transactions",
        {"CONSTITUENT_ID": "241384274", "GIFT_DATE": "2024-09-18", "AMOUNT": "250.00"},
        "gift_transactions_table.csv",
    )
    assert row["constituent_id"] == "241384274"
    assert row["gift_date"] == "2024-09-18"
    assert row["amount"] == "250.00"
    assert len(row["record_hash"]) == 64


def test_missing_na_values_are_not_converted_to_zero():
    row = _normalize_row(
        "video_email_events",
        {
            "CONSTITUENT_ID": "804281879",
            "CONTACT_ID": "11199576",
            "SENT_DATE": "2017-04-20",
            "OPENED_DATE": "NA",
            "STARTED_VIDEO_DATE": "NA",
            "BOUNCED": "0",
            "UNSUBSCRIBED": "0",
            "CLICKS": "0",
            "VIDEO_VIEWS": "0",
            "STARTED_VIDEO": "0",
            "WATCHED_VIDEO_25_PERCENT": "0",
            "WATCHED_VIDEO_50_PERCENT": "0",
            "WATCHED_VIDEO_75_PERCENT": "0",
            "FINISHED_VIDEO": "0",
            "VIDEO_SHARES_TO_FACEBOOK": "0",
        },
        "video_email_data_table.csv",
    )
    assert row["opened_date"] is None
    assert row["started_video_date"] is None
    assert row["bounced"] is False
    assert row["clicks"] == 0


def test_negative_gift_amount_is_rejected():
    with pytest.raises(ValueError, match="cannot be negative"):
        _normalize_row(
            "gift_transactions",
            {"CONSTITUENT_ID": "123", "GIFT_DATE": "2024-09-18", "AMOUNT": "-5"},
            "gift_transactions_table.csv",
        )


def test_scan_data_folder_detects_only_supported_datasets(tmp_path):
    (tmp_path / "gift_transactions_table.csv").write_text("CONSTITUENT_ID,GIFT_DATE,AMOUNT\n", encoding="utf-8")
    (tmp_path / "unmapped.csv").write_text("anything\n", encoding="utf-8")
    result = scan_data_folder(tmp_path)
    assert result["recognized_count"] == 1
    assert len(result["files"]) == 2


def test_gift_amount_precision_is_preserved():
    row = _normalize_row(
        "gift_transactions",
        {"CONSTITUENT_ID": "123", "GIFT_DATE": "2024-09-18", "AMOUNT": "1,250.45"},
        "gift_transactions_table.csv",
    )
    assert row["amount"] == "1250.45"


def test_gift_amount_with_more_than_two_decimals_is_rejected():
    with pytest.raises(ValueError, match="two decimal places"):
        _normalize_row(
            "gift_transactions",
            {"CONSTITUENT_ID": "123", "GIFT_DATE": "2024-09-18", "AMOUNT": "1.234"},
            "gift_transactions_table.csv",
        )
