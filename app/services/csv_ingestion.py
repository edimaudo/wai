import csv
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.config import settings
from app.supabase_client import SupabaseNotConfigured, get_service_client


DATASETS: dict[str, dict[str, Any]] = {
    "gift_transactions": {
        "table": "gift_transactions",
        "required": ["CONSTITUENT_ID", "GIFT_DATE", "AMOUNT"],
        "fields": ["CONSTITUENT_ID", "GIFT_DATE", "AMOUNT"],
    },
    "crm_interactions": {
        "table": "crm_interactions",
        "required": ["CONSTITUENT_ID", "CRM_INTERACTION_DATE", "CRM_INTERACTION_TYPE"],
        "fields": ["CONSTITUENT_ID", "CRM_INTERACTION_DATE", "CRM_INTERACTION_TYPE"],
    },
    "video_email_events": {
        "table": "video_email_events",
        "required": ["CONSTITUENT_ID", "CONTACT_ID", "SENT_DATE"],
        "fields": [
            "CONSTITUENT_ID", "CONTACT_ID", "SENT_DATE", "OPENED_DATE", "STARTED_VIDEO_DATE",
            "BOUNCED", "UNSUBSCRIBED", "CLICKS", "VIDEO_VIEWS", "STARTED_VIDEO",
            "WATCHED_VIDEO_25_PERCENT", "WATCHED_VIDEO_50_PERCENT",
            "WATCHED_VIDEO_75_PERCENT", "FINISHED_VIDEO", "VIDEO_SHARES_TO_FACEBOOK",
        ],
    },
}


def _compact_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def identify_dataset(path: Path) -> str | None:
    name = _compact_name(path.stem)
    if "gifttransaction" in name:
        return "gift_transactions"
    # Accept the supplied filename spelling "CRM_interacions_table" as well as the corrected spelling.
    if name.startswith("crminterac"):
        return "crm_interactions"
    if "videoemail" in name:
        return "video_email_events"
    return None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.upper() in {"NA", "N/A", "NULL", "NONE"}:
        return None
    return text


def _parse_date(value: Any) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    try:
        return date.fromisoformat(cleaned[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid date value: {cleaned}") from exc


def _parse_number(value: Any, field: str) -> int | str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    try:
        number = Decimal(cleaned.replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid number for {field}: {cleaned}") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite number.")
    if field in {"CLICKS", "VIDEO_VIEWS", "VIDEO_SHARES_TO_FACEBOOK"}:
        if number != number.to_integral_value() or number < 0:
            raise ValueError(f"{field} must be a non-negative whole number.")
        return int(number)
    if field == "AMOUNT":
        if number.as_tuple().exponent < -2:
            raise ValueError("AMOUNT cannot contain more than two decimal places.")
        return format(number.quantize(Decimal("0.01")), "f")
    return format(number, "f")


def _parse_bool(value: Any, field: str) -> bool | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    normalized = cleaned.lower()
    if normalized in {"1", "true", "t", "yes", "y"}:
        return True
    if normalized in {"0", "false", "f", "no", "n"}:
        return False
    raise ValueError(f"Invalid boolean for {field}: {cleaned}")


def _normalize_row(dataset: str, raw: dict[str, Any], source_file: str) -> dict[str, Any]:
    spec = DATASETS[dataset]
    row: dict[str, Any] = {}
    for field in spec["fields"]:
        row[field.lower()] = _clean(raw.get(field))

    if dataset == "gift_transactions":
        row["gift_date"] = _parse_date(row["gift_date"])
        row["amount"] = _parse_number(row["amount"], "AMOUNT")
        if not row["constituent_id"] or not row["gift_date"] or row["amount"] is None:
            raise ValueError("CONSTITUENT_ID, GIFT_DATE and AMOUNT are required.")
        if Decimal(row["amount"]) < 0:
            raise ValueError("AMOUNT cannot be negative.")
    elif dataset == "crm_interactions":
        row["crm_interaction_date"] = _parse_date(row["crm_interaction_date"])
        if not row["constituent_id"] or not row["crm_interaction_date"] or not row["crm_interaction_type"]:
            raise ValueError("CONSTITUENT_ID, CRM_INTERACTION_DATE and CRM_INTERACTION_TYPE are required.")
    elif dataset == "video_email_events":
        row["sent_date"] = _parse_date(row["sent_date"])
        row["opened_date"] = _parse_date(row["opened_date"])
        row["started_video_date"] = _parse_date(row["started_video_date"])
        for field in ("bounced", "unsubscribed", "started_video", "watched_video_25_percent",
                      "watched_video_50_percent", "watched_video_75_percent", "finished_video"):
            row[field] = _parse_bool(row[field], field.upper())
        for field in ("clicks", "video_views", "video_shares_to_facebook"):
            row[field] = _parse_number(row[field], field.upper())
        if not row["constituent_id"] or not row["contact_id"] or not row["sent_date"]:
            raise ValueError("CONSTITUENT_ID, CONTACT_ID and SENT_DATE are required.")

    identity = {key: value for key, value in row.items() if key not in {"source_file", "record_hash", "ingested_at"}}
    row["record_hash"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    row["source_file"] = source_file
    return row


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_data_folder(data_dir: Path | None = None) -> dict[str, Any]:
    root = (data_dir or settings.data_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.csv")):
        if not path.is_file() or path.name.startswith("."):
            continue
        dataset = identify_dataset(path)
        files.append({
            "name": path.name,
            "relative_path": path.relative_to(root).as_posix(),
            "dataset": dataset,
            "recognized": dataset is not None,
            "size_bytes": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return {"root": str(root), "files": files, "recognized_count": sum(1 for item in files if item["recognized"])}


def _already_imported(service_client: Any, dataset: str, file_hash: str) -> bool:
    try:
        result = (
            service_client.table("data_imports")
            .select("id")
            .eq("dataset_key", dataset)
            .eq("file_hash", file_hash)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception:
        # A missing import-history table should produce an actionable error when the import log is written.
        return False


def import_csv_file(path: Path, root: Path | None = None) -> dict[str, Any]:
    root = (root or settings.data_dir).resolve()
    dataset = identify_dataset(path)
    if not dataset:
        return {"file": path.name, "status": "skipped", "message": "Unrecognized CSV filename."}

    spec = DATASETS[dataset]
    relative_name = path.resolve().relative_to(root).as_posix()
    file_hash = _sha256_file(path)
    client = get_service_client()
    if _already_imported(client, dataset, file_hash):
        return {"file": relative_name, "dataset": dataset, "status": "unchanged", "message": "This exact file version was already imported."}

    imported = 0
    rejected = 0
    seen = 0
    errors: list[str] = []
    batch: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8-sig", newline="") as file_handle:
        reader = csv.DictReader(file_handle)
        headers = set(reader.fieldnames or [])
        missing = [field for field in spec["required"] if field not in headers]
        if missing:
            raise ValueError(f"{path.name} is missing required columns: {', '.join(missing)}")

        for line_number, raw in enumerate(reader, start=2):
            seen += 1
            if seen > settings.max_csv_import_rows:
                raise ValueError(f"{path.name} exceeds the configured row limit of {settings.max_csv_import_rows}.")
            try:
                normalized = _normalize_row(dataset, raw, relative_name)
                batch.append(normalized)
            except ValueError as exc:
                rejected += 1
                if len(errors) < 25:
                    errors.append(f"Row {line_number}: {exc}")
                continue

            if len(batch) >= 200:
                client.table(spec["table"]).upsert(batch, on_conflict="record_hash").execute()
                imported += len(batch)
                batch.clear()

    if batch:
        client.table(spec["table"]).upsert(batch, on_conflict="record_hash").execute()
        imported += len(batch)

    status_value = "completed" if rejected == 0 else ("completed_with_errors" if imported else "failed")
    client.table("data_imports").insert({
        "dataset_key": dataset,
        "source_file": relative_name,
        "file_hash": file_hash,
        "records_seen": seen,
        "records_loaded": imported,
        "records_rejected": rejected,
        "status": status_value,
        "error_summary": errors,
    }).execute()

    return {
        "file": relative_name,
        "dataset": dataset,
        "status": status_value,
        "records_seen": seen,
        "records_loaded": imported,
        "records_rejected": rejected,
        "errors": errors,
    }


def import_recognized_files(data_dir: Path | None = None) -> dict[str, Any]:
    root = (data_dir or settings.data_dir).resolve()
    scan = scan_data_folder(root)
    recognized_files = [root / item["relative_path"] for item in scan["files"] if item["recognized"]]
    results: list[dict[str, Any]] = []
    for path in recognized_files:
        try:
            results.append(import_csv_file(path, root))
        except Exception as exc:
            results.append({"file": path.relative_to(root).as_posix(), "status": "failed", "message": str(exc)})
    return {
        "files_scanned": len(scan["files"]),
        "recognized_files": len(recognized_files),
        "results": results,
    }
