from datetime import datetime, timezone
import hashlib
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session
from app.config import settings
from app.models import Source, Evidence
from app.services.llm import gemini

async def check_public_sources(db: Session, lang: str = "en") -> dict:
    checked = []
    sources = db.query(Source).filter_by(enabled=True).order_by(Source.name).all()
    headers = {"User-Agent": "WaiIntelligence-Demo/1.0 (public-source availability monitor)"}
    async with httpx.AsyncClient(timeout=settings.source_timeout_seconds, follow_redirects=True, headers=headers) as client:
        for source in sources:
            status = "error"; http_status = None; title = ""; content_changed = False; signal = ""
            try:
                parsed = urlparse(source.url)
                if parsed.scheme != "https" or not parsed.netloc:
                    raise ValueError("Only HTTPS public sources are allowed")
                response = await client.get(source.url)
                http_status = response.status_code
                if response.status_code < 400:
                    soup = BeautifulSoup(response.text[:250000], "html.parser")
                    title = soup.title.get_text(" ", strip=True)[:250] if soup.title else ""
                    excerpt = soup.get_text(" ", strip=True)[:9000]
                    digest = hashlib.sha256(excerpt.encode("utf-8", errors="ignore")).hexdigest()
                    content_changed = bool(source.last_content_hash and source.last_content_hash != digest)
                    source.last_excerpt = excerpt[:5000]
                    if content_changed:
                        source.changed_at = datetime.now(timezone.utc)
                        signal = ("Le texte de la page a changé depuis la dernière vérification. Examinez les changements possibles concernant la stratégie, l’admissibilité ou les appels de financement." if lang == "fr" else "Page text changed since the previous check. Review the source for changes to strategy, eligibility or funding calls.")
                        if gemini.configured:
                            lang_instruction = "Rédigez l’analyse en français canadien (fr-CA). " if lang == "fr" else "Write the analysis in Canadian English (en-CA). "
                            interpretation = gemini.generate_text(
                                lang_instruction + "Compare this current public funder page excerpt with its page title and identify any explicit signals of a new funding direction, future funding priority, changed eligibility or call announcement. Do not infer an open call from broad policy language. Quote a short supporting phrase and label uncertainty. If no relevant signal is found, say so.\nSOURCE: " + source.name + "\nURL: " + source.url + "\nTITLE: " + title + "\nEXCERPT: " + excerpt[:7000]
                            )
                            if interpretation: signal = interpretation[:1600]
                        db.add(Evidence(module="prospecting", subject_type="source", subject_id=str(source.id),
                            claim="Public source page text changed since the previous successful check. " + signal,
                            classification="source change signal", source_name=source.name, source_url=source.url,
                            checked_at=datetime.now(timezone.utc), confidence="change detected; interpretation requires review",
                            caveat="A page change is not proof of a new grant call or policy change. Compare the source text and verify relevant details."))
                    source.last_content_hash = digest
                    status = "reachable"
                else:
                    status = "http_error"
            except Exception as exc:
                status = "error"
                title = exc.__class__.__name__
            source.last_checked_at = datetime.now(timezone.utc)
            source.last_status = status
            source.last_http_status = http_status
            source.last_title = title
            checked.append({"name": source.name, "status": status, "http_status": http_status, "page_title": title,
                            "content_changed": content_changed, "signal": signal})
    db.commit()
    note = ("Une vérification HTTP réussie confirme uniquement l’accessibilité de la page. Les changements de texte sont des signaux à examiner, et non une preuve de nouvel appel, de date limite, d’admissibilité ou d’adéquation stratégique." if lang == "fr" else "A successful HTTP check confirms page reachability only. Page-text changes are review signals, not proof of a new call, deadline, eligibility or strategic fit.")
    return {"checked": len(checked), "results": checked, "note": note}
