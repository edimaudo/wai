# Private source data

Place the complete synthetic/anonymized and approved source CSV files here. This directory is read-only to the application container in deployment and is not served to browsers.

Recognized initial datasets:
- `gift_transactions_table.csv`
- `CRM_interacions_table.csv` or `CRM_interactions_table.csv`
- `video_email_data_table.csv`

Do not commit source data files to Git. The `.gitignore` excludes CSV, spreadsheet, PDF and Word files under this directory.
