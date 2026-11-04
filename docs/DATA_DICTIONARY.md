# Wai Intelligence — source CSV data dictionary

The three CSV files below are the source files supplied in the project brief. They are stored in `data/source/` by default. `WAI_DATA_DIR` can point to another folder readable by the FastAPI process or to a read-only mounted volume.

## Read-only handling

The application opens these files directly from the configured folder. Its source-folder scan reports the file name, column headings and row count. A scan does **not** import rows into the application database, create donor profiles, or join records across files. Constituent and contact identifiers remain strings. The source files are never rewritten, renamed or normalized in place.

The three source files are available for authenticated CSV export. Synthetic stewardship profiles and synthetic reporting metrics are separate demonstration records stored in the workspace database; they are not constructed from these source CSVs.

## `gift_transactions_table.csv`

| Field | Type | Description |
|---|---|---|
| `CONSTITUENT_ID` | String | Source-system constituent identifier. Preserve leading zeroes. |
| `GIFT_DATE` | ISO date string | Date associated with the gift transaction. |
| `AMOUNT` | Numeric string | Gift amount. Currency is not specified in the supplied sample and must be confirmed before financial aggregation. |

## `CRM_interacions_table.csv`

The filename is preserved as written in the project brief.

| Field | Type | Description |
|---|---|---|
| `CONSTITUENT_ID` | String | Source-system constituent identifier. |
| `CRM_INTERACTION_DATE` | ISO date string | Date of the recorded interaction. |
| `CRM_INTERACTION_TYPE` | String | Interaction category as supplied in the source file. |

This is a local CSV file only. It does not establish a live CRM connection or synchronization process.

## `video_email_data_table.csv`

| Field | Type | Description |
|---|---|---|
| `CONSTITUENT_ID` | String | Source-system constituent identifier. |
| `CONTACT_ID` | String | Source-system contact identifier. Preserve as text. |
| `SENT_DATE` | ISO date string | Date the email was sent. |
| `OPENED_DATE` | ISO date string or `NA` | Date the email was opened, if present. |
| `STARTED_VIDEO_DATE` | ISO date string or `NA` | Date video engagement began, if present. |
| `BOUNCED` | Integer flag | Whether the message bounced. |
| `UNSUBSCRIBED` | Integer flag | Whether the recipient unsubscribed. |
| `CLICKS` | Integer | Click count. |
| `VIDEO_VIEWS` | Integer | Video view count. |
| `STARTED_VIDEO` | Integer flag | Whether video playback began. |
| `WATCHED_VIDEO_25_PERCENT` | Integer flag | Whether 25% of the video was watched. |
| `WATCHED_VIDEO_50_PERCENT` | Integer flag | Whether 50% of the video was watched. |
| `WATCHED_VIDEO_75_PERCENT` | Integer flag | Whether 75% of the video was watched. |
| `FINISHED_VIDEO` | Integer flag | Whether the video was completed. |
| `VIDEO_SHARES_TO_FACEBOOK` | Integer | Number of shares to Facebook. |

## Separate demonstration records

The reporting metrics and named donor stewardship profiles shown in the demonstration are fictional workspace records seeded independently. They are not part of these three source CSVs. The supplied schemas do not contain donor names, email addresses, communication preferences, consent restrictions or correspondence. The application therefore does not infer or manufacture those attributes from constituent identifiers.

## Operational notes

- Default folder: `data/source/`.
- Container mount: `./data/source:/app/data/source:ro`.
- Override the folder with `WAI_DATA_DIR`.
- Startup and dashboard scans read the files and report metadata only.
- The authenticated export centre can download the source files.
- The original files remain unchanged. Store approved production files only in access-controlled storage; do not commit sensitive production records to a public repository.
