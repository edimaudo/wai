from pathlib import Path
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.database import SessionLocal
from app.models import User, Opportunity, Donor
from app.security import verify_password
from app.services.seed import load_source_csvs, read_source_records, SOURCE_CSV_FILES
from app.services.scoring import score_opportunity


def token_from(response):
    soup = BeautifulSoup(response.text, "html.parser")
    field = soup.find("input", {"name": "csrf"})
    assert field is not None
    return field.get("value")


def login(client, username, password):
    response = client.get("/signin")
    token = token_from(response)
    return client.post("/signin", data={
        "csrf": token, "username": username, "password": password, "next_path": "/app"
    }, follow_redirects=False)


def test_public_landing_hides_workspace_navigation():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Wai Intelligence" in response.text
        assert 'href="/app"' not in response.text
        assert 'href="/signin"' in response.text
        assert client.get("/openapi.json").status_code == 404


def test_workspace_requires_authentication():
    with TestClient(app) as client:
        response = client.get("/app", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/signin"
        assert client.get("/app/prospecting", follow_redirects=False).status_code == 303


def test_demo_login_and_role_based_admin_access():
    with TestClient(app) as client:
        response = login(client, settings.demo_admin_username, settings.demo_admin_password)
        assert response.status_code == 303
        assert response.headers["location"] == "/app"
        assert client.get("/app/admin").status_code == 200
        signout = client.get("/app")
        token = token_from(signout)
        client.post("/signout", data={"csrf":token}, follow_redirects=False)
        response = login(client, settings.demo_user_username, settings.demo_user_password)
        assert response.status_code == 303
        assert client.get("/app").status_code == 200
        assert client.get("/app/admin").status_code == 403



def test_application_default_database_is_postgresql():
    from app.config import Settings
    defaults = Settings(_env_file=None)
    assert Settings.model_fields['database_url'].default.startswith('postgresql+psycopg://')
    database_module = Path('app/database.py').read_text(encoding='utf-8')
    assert 'check_same_thread' not in database_module
    assert 'data/wai_intelligence.db' not in Path('app/config.py').read_text(encoding='utf-8')

def test_seed_users_are_password_hashed():
    db = SessionLocal()
    try:
        admin = db.query(User).filter_by(username=settings.demo_admin_username).first()
        assert admin is not None
        assert admin.password_hash != settings.demo_admin_password
        assert verify_password(settings.demo_admin_password, admin.password_hash)
    finally:
        db.close()


def test_supplied_source_csvs_are_read_in_place_without_database_import():
    before = {p.name:p.stat().st_mtime_ns for p in settings.wai_data_dir.glob("*.csv")}
    db = SessionLocal()
    try:
        donor_count_before = db.query(Donor).count()
        result = load_source_csvs(db)
        donor_count_after = db.query(Donor).count()
        assert result["read_only"] is True
        assert result["rows"] == 12
        assert {item["name"] for item in result["files"]} == set(SOURCE_CSV_FILES)
        assert donor_count_after == donor_count_before  # no constituent rows become donor profiles
    finally:
        db.close()
    after = {p.name:p.stat().st_mtime_ns for p in settings.wai_data_dir.glob("*.csv")}
    assert before == after
    assert not result["warnings"]

    gift_columns, gifts = read_source_records("gift_transactions_table.csv")
    assert gift_columns == ["CONSTITUENT_ID", "GIFT_DATE", "AMOUNT"]
    assert gifts[0]["CONSTITUENT_ID"] == "241384274"
    interaction_columns, interactions = read_source_records("CRM_interacions_table.csv")
    assert interaction_columns == ["CONSTITUENT_ID", "CRM_INTERACTION_DATE", "CRM_INTERACTION_TYPE"]
    assert interactions[0]["CRM_INTERACTION_TYPE"] == "Phone"
    video_columns, video_rows = read_source_records("video_email_data_table.csv")
    assert "CONTACT_ID" in video_columns
    assert video_rows[0]["CONTACT_ID"] == "11199576"
    assert len(video_rows) == 3

    profile_columns, profiles = read_source_records("constituent_profile_table.csv")
    assert profile_columns == [
        "CONSTITUENT_ID", "MANAGED_PROSPECT", "LIFETIME_GIVING", "LIFETIME_GIFT_COUNT",
        "FIRST_GIFT_DATE", "LAST_GIFT_DATE", "LIFETIME_CRM_INTERACTIONS",
        "FIRST_CRM_INTERACTION_DATE", "LAST_CRM_INTERACTION_DATE", "CAPACITY_ESTIMATE"
    ]
    assert len(profiles) == 3
    assert profiles[0]["CONSTITUENT_ID"] == "437216659"
    assert profiles[0]["CAPACITY_ESTIMATE"] == ""
    assert profiles[1]["CAPACITY_ESTIMATE"] == "250,000 - 499,999"
    assert profiles[2]["LIFETIME_GIVING"] == "33381.58"


def test_dashboard_lists_source_csvs_as_read_only():
    with TestClient(app) as client:
        login(client, settings.demo_admin_username, settings.demo_admin_password)
        response = client.get('/app')
        assert response.status_code == 200
        assert 'gift_transactions_table.csv' in response.text
        assert 'CRM_interacions_table.csv' in response.text
        assert 'video_email_data_table.csv' in response.text
        assert 'constituent_profile_table.csv' in response.text
        assert 'not imported into the application database' in response.text
        assert 'import donor' not in response.text.lower()



def test_constituent_profile_source_is_visible_and_joined_in_memory_only():
    with TestClient(app) as client:
        anonymous = client.get('/app/constituents', follow_redirects=False)
        assert anonymous.status_code == 303
        login(client, settings.demo_admin_username, settings.demo_admin_password)
        response = client.get('/app/constituents')
        assert response.status_code == 200
        assert '437216659' in response.text
        assert '857096450' in response.text
        assert '238949049' in response.text
        assert '250,000 - 499,999' in response.text
        assert 'does not specify a currency' in response.text.lower()
        assert 'Gift rows in source file' in response.text
        assert 'not lifetime totals' in response.text.lower()


def test_fit_score_is_weighted_and_not_probability():
    db = SessionLocal()
    try:
        item = db.query(Opportunity).first()
        score, details = score_opportunity(item, {
            "criteria_wash":"30", "criteria_geography":"20", "criteria_eligibility":"20",
            "criteria_timing":"15", "criteria_capacity":"15"
        })
        assert 0 <= score <= 100
        assert details["calculated_score"] == score
        assert sum(details["weights"].values()) == 100
    finally:
        db.close()


def test_compliance_page_surfaces_missing_document_and_clause_bottlenecks():
    with TestClient(app) as client:
        login(client, settings.demo_admin_username, settings.demo_admin_password)
        response = client.get('/app/compliance')
        assert response.status_code == 200
        assert 'Workflow bottlenecks' in response.text
        assert 'No source documents uploaded' in response.text
        assert 'Clause reference needs verification' in response.text
        assert 'Human input' in response.text


def test_authenticated_csv_exports_and_zip_bundle():
    import csv
    import io
    import zipfile

    with TestClient(app) as client:
        anonymous = client.get('/app/exports/opportunities.csv', follow_redirects=False)
        assert anonymous.status_code == 303
        assert anonymous.headers['location'] == '/signin'

        login(client, settings.demo_admin_username, settings.demo_admin_password)
        page = client.get('/app/exports')
        assert page.status_code == 200
        assert 'Download all CSV files' in page.text
        assert 'CRM_interacions_table.csv' in page.text
        assert 'constituent_profile_table.csv' in page.text

        opportunities = client.get('/app/exports/opportunities.csv')
        assert opportunities.status_code == 200
        assert 'text/csv' in opportunities.headers['content-type']
        assert 'attachment;' in opportunities.headers['content-disposition']
        assert opportunities.headers['cache-control'] == 'no-store'
        assert opportunities.content.startswith(b'\xef\xbb\xbf')
        assert b'title' in opportunities.content

        interaction = client.get('/app/exports/source_interactions.csv')
        assert interaction.status_code == 200
        assert b'CONSTITUENT_ID' in interaction.content
        assert b'CRM_INTERACTION_DATE' in interaction.content  # original supplied column schema is preserved
        assert b'253623044' in interaction.content

        constituent_profiles = client.get('/app/exports/source_constituent_profiles.csv')
        assert constituent_profiles.status_code == 200
        assert 'filename="constituent_profile_table.csv"' in constituent_profiles.headers['content-disposition']
        assert b'CAPACITY_ESTIMATE' in constituent_profiles.content
        assert b'250,000 - 499,999' in constituent_profiles.content

        response = client.get('/app/exports/all.zip')
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())
            assert 'opportunities.csv' in names
            assert 'donors.csv' in names
            assert 'CRM_interacions_table.csv' in names
            assert 'constituent_profile_table.csv' in names
            assert 'source_reporting_metrics.csv' not in names
            document_header = archive.read('grant_documents.csv').decode('utf-8-sig').splitlines()[0]
            assert 'extracted_text' not in document_header
            assert 'stored_path' not in document_header


def test_csv_export_escapes_spreadsheet_formulas():
    from app.main import _csv_value
    assert _csv_value('=HYPERLINK("https://example.invalid")').startswith("'=")
    assert _csv_value('+SUM(A1:A2)') == "'+SUM(A1:A2)"
    assert _csv_value('ordinary text') == 'ordinary text'


def test_win_probability_uses_heuristic_or_explained_na():
    from datetime import date
    from app.services.scoring import estimate_win_probability

    db = SessionLocal()
    try:
        items = db.query(Opportunity).all()
        unsolicited = next(item for item in items if 'Unsolicited concept note' in item.title)
        invitation = next(item for item in items if 'Climate Partnerships' in item.title)
        fellowship = next(item for item in items if 'Fellowship' in item.title)

        estimate = estimate_win_probability(unsolicited, unsolicited.fit_score, today=date(2026, 11, 3))
        assert estimate['method'] == 'heuristic'
        assert 3 <= estimate['value'] <= 40
        assert 'not calibrated' in estimate['note'].lower()

        invitation_result = estimate_win_probability(invitation, invitation.fit_score, today=date(2026, 11, 3))
        assert invitation_result['value'] is None
        assert invitation_result['method'] == 'n_a'
        assert 'invitation' in invitation_result['formula_note'].lower()

        fellowship_result = estimate_win_probability(fellowship, fellowship.fit_score, today=date(2026, 11, 3), lang='fr')
        assert fellowship_result['value'] is None
        assert fellowship_result['method'] == 'n_a'
        assert 'S.O.' in fellowship_result['formula_note']

        french_estimate = estimate_win_probability(unsolicited, unsolicited.fit_score, today=date(2026, 11, 3), lang='fr')
        assert 'Heuristique indicative' in french_estimate['note']
        assert 'score d’adéquation' in french_estimate['formula_note']

        original_eligibility = unsolicited.eligibility
        try:
            unsolicited.eligibility = ''
            missing_data = estimate_win_probability(unsolicited, unsolicited.fit_score, today=date(2026, 11, 3), lang='fr')
            assert missing_data['method'] == 'n_a'
            assert missing_data['value'] is None
            assert 'admissibilité' in missing_data['formula_note'].lower()
        finally:
            unsolicited.eligibility = original_eligibility
    finally:
        db.close()


def test_french_is_persisted_through_login_and_core_pages_are_localized():
    with TestClient(app) as client:
        client.get('/preferences/language?lang=fr')
        login(client, settings.demo_admin_username, settings.demo_admin_password)
        for path in ['/app', '/app/prospecting', '/app/reporting', '/app/stewardship',
                     '/app/diligence', '/app/compliance', '/app/network', '/app/evidence',
                     '/app/tasks', '/app/exports', '/app/constituents', '/app/admin']:
            response = client.get(path)
            assert response.status_code == 200, path
            soup = BeautifulSoup(response.text, 'html.parser')
            assert soup.html.get('lang') == 'fr-CA', path
            assert soup.find(id='main-content') is not None, path
        assert 'Exporter les données' in client.get('/app/exports').text
        prospecting_fr = client.get('/app/prospecting').text
        assert 'Probabilité heuristique de réussite' in prospecting_fr
        assert '17 nov. 2026' in prospecting_fr
        assert 'Relations avec les donateurs' in client.get('/app').text
        stewardship_fr = client.get('/app/stewardship').text
        assert 'Ces profils fictifs sont distincts' in stewardship_fr
        constituent_fr = client.get('/app/constituents').text
        assert 'Données sources des profils de constituants' in constituent_fr
        assert 'Identifiant du constituant' in constituent_fr


def test_workspace_pages_have_accessible_names_landmarks_and_chart_tables():
    paths = ['/app', '/app/prospecting', '/app/reporting', '/app/stewardship',
             '/app/stewardship/1', '/app/constituents', '/app/diligence', '/app/compliance', '/app/network',
             '/app/evidence', '/app/tasks', '/app/exports', '/app/admin']
    with TestClient(app) as client:
        login(client, settings.demo_admin_username, settings.demo_admin_password)
        for path in paths:
            soup = BeautifulSoup(client.get(path).text, 'html.parser')
            assert soup.find('a', class_='skip-link', href='#main-content') is not None, path
            assert soup.find('main', id='main-content') is not None, path
            assert soup.find('nav') is not None, path
            for region in soup.select('.table-wrap'):
                assert region.get('tabindex') == '0', path
                assert region.get('role') == 'region', path
                assert region.get('aria-label'), path
            for control in soup.select('input:not([type=hidden]), select, textarea'):
                if control.has_attr('aria-label') or control.has_attr('aria-labelledby'):
                    continue
                control_id = control.get('id')
                associated = soup.find('label', attrs={'for': control_id}) if control_id else None
                if not associated and control.parent and control.parent.name == 'label':
                    associated = control.parent
                assert associated is not None, f'{path}: unlabeled {control.name}#{control_id}'
            for chart in soup.select('[role="img"][aria-describedby]'):
                described_by = chart.get('aria-describedby').split()
                for reference in described_by:
                    assert soup.find(id=reference) is not None, f'{path}: missing chart alternative {reference}'


def test_every_template_translation_key_exists_in_canadian_french():
    import re
    from app.i18n import TRANSLATIONS

    template_dir = Path(__file__).resolve().parents[1] / 'app' / 'templates'
    keys = set()
    for template in template_dir.glob('*.html'):
        keys.update(re.findall(r"t\(['\"]([^'\"]+)['\"]\)", template.read_text(encoding='utf-8')))
    assert keys
    assert not sorted(key for key in keys if key not in TRANSLATIONS['fr'])


def test_core_light_and_dark_text_colour_pairs_meet_wcag_aa_contrast():
    def relative_luminance(hex_colour):
        colour = hex_colour.lstrip('#')
        rgb = [int(colour[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    def contrast_ratio(foreground, background):
        lighter, darker = sorted((relative_luminance(foreground), relative_luminance(background)), reverse=True)
        return (lighter + 0.05) / (darker + 0.05)

    core_pairs = [
        ('#18232b', '#ffffff'), ('#52636e', '#ffffff'), ('#61717c', '#ffffff'),
        ('#006d91', '#ffffff'), ('#005c7a', '#ffffff'), ('#d9e8ef', '#081e2b'),
        ('#8ea9b7', '#081e2b'), ('#e6eef2', '#111c23'), ('#b4c3cb', '#111c23'),
        ('#9aabb4', '#111c23'), ('#63c9e6', '#111c23'), ('#8ce8ff', '#111c23'),
        ('#15734e', '#e8f6ef'), ('#895e12', '#fff5df'), ('#a33d3d', '#fff0ef'),
        ('#80d4a8', '#16372d'), ('#f0c36b', '#3b311c'), ('#f19a93', '#3d2525'),
        ('#005f7d', '#e3f5fa'), ('#8adcf1', '#173f4e'),
        ('#074e68', '#eaf7fb'), ('#315c6e', '#eaf7fb'),
        ('#c4e5ef', '#173440'), ('#b6d4de', '#173440'),
        ('#76510e', '#fff5df'), ('#6d5b32', '#fff5df'),
        ('#ddc994', '#3b311c'), ('#ffffff', '#006d91'), ('#ffffff', '#006f92'),
    ]
    assert all(contrast_ratio(foreground, background) >= 4.5 for foreground, background in core_pairs)
