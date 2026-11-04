# Source data dictionary

The four source CSVs are stored under `data/source/` by default. `WAI_DATA_DIR` can point to another readable directory. The loader opens the files in place, reports file names, headers and row counts, and never rewrites them or inserts their rows into PostgreSQL. Constituent and contact identifiers are strings, not numeric values.

## Constituent profile: `constituent_profile_table.csv`

| Field | Meaning / handling |
|---|---|
| `CONSTITUENT_ID` | Source identifier. Exact-match key used for in-memory lookup against the other source CSVs. |
| `MANAGED_PROSPECT` | Source-managed prospect flag. The sample uses `1`; other source values are retained as supplied. |
| `LIFETIME_GIVING` | Lifetime giving value. The source snippet does not specify currency; the UI therefore displays the raw value and does not label it CAD. |
| `LIFETIME_GIFT_COUNT` | Lifetime gift count as supplied by the profile source. |
| `FIRST_GIFT_DATE` | First recorded gift date. Blank values remain blank. |
| `LAST_GIFT_DATE` | Last recorded gift date. Blank values remain blank. |
| `LIFETIME_CRM_INTERACTIONS` | Lifetime CRM interaction count as supplied by the profile source. |
| `FIRST_CRM_INTERACTION_DATE` | First CRM interaction date. |
| `LAST_CRM_INTERACTION_DATE` | Last CRM interaction date. |
| `CAPACITY_ESTIMATE` | Source-provided capacity band. Blank values remain blank. It is not an independently calculated wealth estimate. |

## Gift transactions: `gift_transactions_table.csv`

| Field | Meaning / handling |
|---|---|
| `CONSTITUENT_ID` | Source constituent identifier. |
| `GIFT_DATE` | Transaction gift date. |
| `AMOUNT` | Transaction amount. Currency is not specified in the sample. |

## CRM interactions: `CRM_interacions_table.csv`

The spelling of the file name is preserved as supplied.

| Field | Meaning / handling |
|---|---|
| `CONSTITUENT_ID` | Source constituent identifier. |
| `CRM_INTERACTION_DATE` | Recorded interaction date. |
| `CRM_INTERACTION_TYPE` | Interaction type as supplied by the source. |

## Email and video engagement: `video_email_data_table.csv`

| Field | Meaning / handling |
|---|---|
| `CONSTITUENT_ID` | Source constituent identifier. |
| `CONTACT_ID` | Source contact identifier; retained as text. |
| `SENT_DATE` | Email sent date. |
| `OPENED_DATE` | Email opened date or source null marker such as `NA`. |
| `STARTED_VIDEO_DATE` | Date video engagement began or source null marker. |
| `BOUNCED` | Bounce flag. |
| `UNSUBSCRIBED` | Unsubscribe flag. |
| `CLICKS` | Click count. |
| `VIDEO_VIEWS` | Video view count. |
| `STARTED_VIDEO` | Video-start flag. |
| `WATCHED_VIDEO_25_PERCENT` | 25% completion flag. |
| `WATCHED_VIDEO_50_PERCENT` | 50% completion flag. |
| `WATCHED_VIDEO_75_PERCENT` | 75% completion flag. |
| `FINISHED_VIDEO` | Video completion flag. |
| `VIDEO_SHARES_TO_FACEBOOK` | Facebook share count. |

## In-app use and boundaries

The Constituent Source Data view reads the profile file and looks up exact `CONSTITUENT_ID` matches in the other three files at request time. It displays the counts of matching rows in those detail files. Those counts are not represented as lifetime totals, and no join result is written to PostgreSQL. The full source rows remain available through authenticated CSV exports.

The sample files in the project package are incomplete demonstration snippets. Replace them with the complete approved datasets before a pilot. Confirm currency, date conventions, source-system definitions and the currentness/meaning of `CAPACITY_ESTIMATE` with the data owner before using the fields for decisions. Production source data should be kept in a private, access-controlled folder and must not be committed to a public repository.
