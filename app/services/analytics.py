from functools import lru_cache
from collections import defaultdict
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models import ReportMetric

@lru_cache(maxsize=16)
def _metric_summary(rows_key: tuple, metric: str):
    rows = [r for r in rows_key if r[1] == metric]
    rows.sort(key=lambda r: r[0])
    if len(rows) < 2: return {"series": rows, "change_pct": None, "change": None}
    prior, current = rows[-2][2], rows[-1][2]
    return {"series": rows, "change_pct": round(((current-prior)/prior)*100, 1) if prior else None,
            "change": round(current-prior, 1)}

def report_data(db: Session):
    records = db.query(ReportMetric).all()
    rows_key = tuple(sorted((r.period, r.metric, r.value) for r in records))
    metrics = {}
    for metric in sorted({r.metric for r in records}):
        summary = _metric_summary(rows_key, metric)
        definition = next((r.definition for r in records if r.metric == metric), "")
        metrics[metric] = {**summary, "definition": definition}
    return metrics

def build_reporting_draft(db: Session, audience: str, lang: str = "en"):
    metrics = report_data(db)
    rev = metrics.get("fundraising_revenue", {})
    donors = metrics.get("active_donors", {})
    opens = metrics.get("email_open_rate", {})
    is_fr = lang == "fr"

    if is_fr:
        labels = {"board": "Conseil d’administration", "leadership": "Direction", "staff": "Personnel"}
        facts = []
        if rev.get("series"):
            current = rev["series"][-1]
            facts.append(f"Les revenus de collecte de fonds au {current[0]} s’élevaient à {current[2]:,.0f} $ CA.")
        if rev.get("change_pct") is not None:
            facts.append(f"Les revenus ont varié de {rev['change_pct']:+.1f} % entre les deux trimestres les plus récents du jeu de données.")
        if donors.get("series"):
            current = donors["series"][-1]
            facts.append(f"Nombre de donateurs actifs au {current[0]} : {int(current[2]):,}.")
        if opens.get("series"):
            current = opens["series"][-1]
            facts.append(f"Taux d’ouverture des courriels au {current[0]} : {current[2]:.1f} %.")
        observations = " ".join(facts)
        audience_label = labels.get(audience, "Personnel")
        prompt = (
            f"Préparez une ébauche de rapport pour le public suivant : {audience_label}, pour WaterAid Canada. "
            f"Utilisez UNIQUEMENT ce résumé de données : {observations}. Les données sont synthétiques. "
            "Contexte connu du scénario : les revenus de 2025-T4 comprennent un don de contrepartie ponctuel synthétique. "
            "Rédigez tout le contenu en français canadien (fr-CA). Distinguez les observations des causes possibles; "
            "n’affirmez aucune causalité. Fournissez un récit exécutif concis, des explications possibles et des questions "
            "pour le personnel. N’inventez aucun fait."
        )
        fallback = {
            "narrative": f"Ébauche pour le public « {audience_label} » (données synthétiques) : {observations} La hausse des revenus de 2025-T4 est associée à un don de contrepartie ponctuel dans ce scénario de démonstration; elle ne doit pas être considérée comme un rythme de revenus récurrent.",
            "observations": observations,
            "interpretations": "La hausse de 2025-T4 concorde avec le scénario étiqueté de don de contrepartie ponctuel. Les variations de l’activité des donateurs et de l’engagement par courriel nécessitent une segmentation supplémentaire avant qu’une cause opérationnelle puisse être déterminée.",
            "questions": "Le don de contrepartie était-il affecté ou non affecté? Les coûts de campagne ont-ils changé? Les définitions des donateurs actifs et de l’engagement par courriel sont-elles cohérentes pour toutes les périodes? Quel contexte de programme ou de marché devrait accompagner ces chiffres?",
            "caveat": "Données synthétiques de scénario. Les observations sont calculées à partir des dossiers; les explications ne sont pas vérifiées indépendamment."
        }
    else:
        labels = {"board": "Board", "leadership": "Leadership", "staff": "Staff"}
        facts = []
        if rev.get("series"):
            current = rev["series"][-1]
            facts.append(f"Fundraising revenue in {current[0]} was CAD {current[2]:,.0f}.")
        if rev.get("change_pct") is not None:
            facts.append(f"Revenue changed {rev['change_pct']:+.1f}% between the two most recent quarters in the dataset.")
        if donors.get("series"):
            current = donors["series"][-1]
            facts.append(f"Active donors in {current[0]}: {int(current[2]):,}.")
        if opens.get("series"):
            current = opens["series"][-1]
            facts.append(f"Email open rate in {current[0]}: {current[2]:.1f}%.")
        observations = " ".join(facts)
        audience_label = labels.get(audience, "Staff")
        prompt = f"Prepare a {audience_label} audience reporting draft for WaterAid Canada using ONLY this dataset summary: {observations}. The dataset is synthetic. Known scenario note: 2025-Q4 revenue contains a one-time synthetic matching gift. Distinguish observations from causes; do not imply causation. Provide a concise executive narrative, possible explanations, and questions for staff. Do not invent facts. Write in Canadian English (en-CA)."
        fallback = {
          "narrative": f"{audience_label} draft (synthetic data): {observations} The 2025-Q4 revenue spike is associated with a one-time matching gift in this demonstration scenario; it should not be interpreted as a recurring run rate.",
          "observations": observations,
          "interpretations": "The 2025-Q4 increase is consistent with the labelled one-time matching-gift scenario. Changes in donor activity and email engagement need additional segmentation before operational causes can be assigned.",
          "questions": "Was the one-time match restricted or unrestricted? Did campaign costs change? Are active-donor and email engagement definitions consistent across all periods? What programme or market context should accompany these figures?",
          "caveat": "Synthetic scenario data. Observations are calculated from the records; explanations are not independently verified."
        }
    generated = __import__('app.services.llm', fromlist=['gemini']).gemini.generate_json(prompt, fallback)
    return {**fallback, **generated}, metrics
