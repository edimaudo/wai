from datetime import date
from app.models import Opportunity

def _terms(value: str) -> list[str]:
    return [part.strip().lower() for part in value.replace("\n", ",").replace(";", ",").split(",") if part.strip()]

def score_opportunity(opportunity: Opportunity, settings_rows: dict[str, str], today: date | None = None) -> tuple[int, dict]:
    """Transparent weighted heuristic. It is a fit index, never a win probability."""
    today = today or date.today()
    focus_terms = _terms(settings_rows.get("criteria_focus_terms", "water, sanitation, hygiene, WASH, climate resilience, eau, assainissement, hygiène, EAH, résilience climatique"))
    geo_terms = _terms(settings_rows.get("criteria_geography_terms", "international, ODA-eligible, Sub-Saharan Africa, Africa, Asia, Pacific, admissible à l’APD, Afrique subsaharienne, Afrique, Asie, Pacifique"))
    block_terms = _terms(settings_rows.get("criteria_ineligibility_terms", "invitation only, invitation-only, individual fellowship, individual applicant, sur invitation seulement, bourse individuelle, demandeur individuel"))
    searchable = f"{opportunity.title} {opportunity.summary} {opportunity.focus_areas}".lower()
    eligibility_text = opportunity.eligibility.lower()
    wash_score = 90 if any(term in searchable for term in focus_terms) else 35
    geo_text = f"{opportunity.geography} {opportunity.summary}".lower()
    geography_score = 85 if any(term in geo_text for term in geo_terms) else 35
    if any(term in eligibility_text for term in block_terms):
        eligibility_score = 15
    elif any(term in eligibility_text for term in ("confirm", "verify", "not known", "confirmer", "vérifier", "non connu", "non établie", "à confirmer")):
        eligibility_score = 45
    else:
        eligibility_score = 65
    if opportunity.deadline and opportunity.deadline < today:
        timing_score = 0
    elif opportunity.deadline and (opportunity.deadline - today).days <= 21:
        timing_score = 65
    elif opportunity.deadline:
        timing_score = 82
    else:
        timing_score = 60
    capacity_score = 55
    weights = {
        "wash": int(settings_rows.get("criteria_wash", "30")),
        "geography": int(settings_rows.get("criteria_geography", "20")),
        "eligibility": int(settings_rows.get("criteria_eligibility", "20")),
        "timing": int(settings_rows.get("criteria_timing", "15")),
        "capacity": int(settings_rows.get("criteria_capacity", "15")),
    }
    total_weights = sum(weights.values()) or 100
    components = {"wash": wash_score, "geography": geography_score, "eligibility": eligibility_score,
                  "timing": timing_score, "capacity": capacity_score}
    score = round(sum(components[k] * weights[k] for k in weights) / total_weights)
    return score, {"weights": weights, "components": components, "calculated_score": score}


def _french_adjustments(adjustments: list[str]) -> list[str]:
    phrases = {
        "−5 points: eligibility needs confirmation": "−5 points : l’admissibilité doit être confirmée",
        "+2 points: no explicit eligibility gap in the available summary": "+2 points : aucune lacune explicite d’admissibilité dans le résumé disponible",
        "−2 points: no fixed published deadline": "−2 points : aucune date limite fixe publiée",
        "−7 points: 14 days or less remain": "−7 points : il reste 14 jours ou moins",
        "−4 points: 15–30 days remain": "−4 points : il reste de 15 à 30 jours",
        "−1 point: 31–90 days remain": "−1 point : il reste de 31 à 90 jours",
        "No timing adjustment: more than 90 days remain": "Aucun ajustement de calendrier : il reste plus de 90 jours",
    }
    return [phrases.get(item, item) for item in adjustments]


def estimate_win_probability(opportunity: Opportunity, fit_score: int, today: date | None = None, lang: str = "en") -> dict:
    """Return a conservative, explicit heuristic or N/A when critical gates are unknown.

    The estimate is not trained or calibrated on WaterAid application outcomes. It is
    a directional planning aid based on fit, eligibility confidence and timing only.
    """
    today = today or date.today()
    is_fr = lang == "fr"
    eligibility = (opportunity.eligibility or "").lower()
    title_summary = f"{opportunity.title} {opportunity.summary}".lower()

    if opportunity.deadline and opportunity.deadline < today:
        return {
            "value": None, "method": "n_a",
            "note": "La date limite publiée est passée; une estimation prospective des chances de réussite n’est pas pertinente." if is_fr else "The published deadline has passed; a forward-looking win estimate is not meaningful.",
            "formula_note": "S.O. — la date limite de cette possibilité est passée. Vérifiez si un nouvel appel ou une prolongation a été publié." if is_fr else "N/A — the opportunity deadline is in the past. Verify whether a new call or extension exists.",
        }
    if not eligibility.strip() or eligibility.strip() in {"unknown", "unknown / unclear", "not provided", "n/a", "s.o.", "inconnu", "non fourni", "non précisé"}:
        return {
            "value": None, "method": "n_a",
            "note": "Les renseignements sur l’admissibilité ne sont pas disponibles; les éléments nécessaires pour une estimation indicative sont insuffisants." if is_fr else "Eligibility information is missing; there is not enough evidence for even a directional estimate.",
            "formula_note": "S.O. — vérifiez le type de demandeur, l’admissibilité géographique et les critères officiels avant d’estimer." if is_fr else "N/A — verify applicant type, geographic eligibility and the official criteria before estimating.",
        }
    if any(term in eligibility for term in ("invitation only", "invitation-only", "sur invitation seulement", "sur invitation uniquement")):
        return {
            "value": None, "method": "n_a",
            "note": "L’admissibilité sur invitation seulement n’est pas confirmée pour WaterAid Canada." if is_fr else "Invitation-only eligibility is not confirmed for WaterAid Canada.",
            "formula_note": "S.O. — n’estimez aucune probabilité avant que le personnel ait confirmé l’invitation et l’admissibilité du demandeur." if is_fr else "N/A — do not estimate a chance until invitation and applicant eligibility are confirmed by staff.",
        }
    if any(term in title_summary for term in ("fellowship", "individual fellowship", "bourse individuelle", "fellowship individuel")) and any(
        term in eligibility for term in ("individual", "applicant profile must be checked", "not established", "demandeur individuel", "type de demandeur à vérifier", "non établi")
    ):
        return {
            "value": None, "method": "n_a",
            "note": "Le type de demandeur admissible n’est pas clair; il pourrait s’agir d’une bourse individuelle plutôt que d’une subvention destinée à une organisation." if is_fr else "The eligible applicant type is unclear; this may be an individual fellowship rather than an organizational grant.",
            "formula_note": "S.O. — confirmez si WaterAid Canada ou un partenaire admissible peut présenter une demande avant toute estimation." if is_fr else "N/A — confirm whether WaterAid Canada or an eligible partner can apply before estimating.",
        }
    if any(term in eligibility for term in ("not established", "not known", "must be checked", "non établi", "non établie", "pas établi", "à vérifier avant toute estimation")):
        return {
            "value": None, "method": "n_a",
            "note": "L’admissibilité du demandeur n’a pas été établie à partir du résumé de source disponible." if is_fr else "Applicant eligibility has not been established from the available source summary.",
            "formula_note": "S.O. — vérifiez le type de demandeur, l’admissibilité géographique et la pertinence du programme dans les directives officielles complètes." if is_fr else "N/A — verify applicant type, geographic eligibility and programme fit against the full official guidance.",
        }

    # Transparent planning heuristic. A fit score is the largest input; uncertain
    # eligibility, a missing deadline and a very short application window reduce it.
    base = 8.0 + (max(0, min(100, int(fit_score))) * 0.30)
    adjustments: list[str] = []
    if any(term in eligibility for term in ("confirm", "verify", "subject to", "confirmer", "vérifier", "sous réserve de", "à confirmer")):
        base -= 5
        adjustments.append("−5 points: eligibility needs confirmation")
    else:
        base += 2
        adjustments.append("+2 points: no explicit eligibility gap in the available summary")

    if opportunity.deadline is None:
        base -= 2
        adjustments.append("−2 points: no fixed published deadline")
    else:
        days_left = (opportunity.deadline - today).days
        if days_left <= 14:
            base -= 7
            adjustments.append("−7 points: 14 days or less remain")
        elif days_left <= 30:
            base -= 4
            adjustments.append("−4 points: 15–30 days remain")
        elif days_left <= 90:
            base -= 1
            adjustments.append("−1 point: 31–90 days remain")
        else:
            adjustments.append("No timing adjustment: more than 90 days remain")

    estimate = max(3, min(40, round(base)))
    return {
        "value": estimate,
        "method": "heuristic",
        "note": "Heuristique indicative seulement. Elle n’est pas étalonnée à partir des demandes ni des décisions d’octroi historiques de WaterAid Canada." if is_fr else "Directional heuristic only. It is not calibrated against WaterAid Canada's historical applications or award outcomes.",
        "formula_note": (f"Heuristique : 8 + (score d’adéquation × 0,30), puis ajustements liés à l’admissibilité et au calendrier; {', '.join(_french_adjustments(adjustments))}. Limite de 3 à 40 %. Utilisez-la uniquement pour prioriser l’examen, et non comme prévision ou affirmation de financement." if is_fr else f"Heuristic: 8 + (fit score × 0.30), then eligibility and timing adjustments; {', '.join(adjustments)}. Capped at 3–40%. Use only to prioritize review, not as a forecast or funding claim."),
    }
