import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser
from html import unescape
from urllib.parse import urljoin, urlparse, parse_qs, unquote, urlencode
from urllib.parse import unquote

# ============================================================
# KONFIGURATION
# ============================================================


# ============================================================
# V43 OPEN-DATA PROVIDER POLICY
# ============================================================
# Production rule:
#   - no freemium quota that requires payment for continued/intended use
#   - no trial-only access
#   - no paywall / private-token / access-control bypass
#   - public/open access and an acceptable licence/terms basis
#   - every imported value must retain source/provenance metadata
#
# "allowed" below means allowed by OUR engineering policy, not a legal opinion.
V43_PROVIDER_REGISTRY = {
    "bundesliga_public": {
        "role": "primary_current_player_stats",
        "allowed": True,
        "costModel": "public_free",
        "productionInput": True,
        "reason": "existing public source already used by pipeline",
    },
    "openligadb": {
        "role": "fixtures_results_context",
        "allowed": True,
        "costModel": "community_free_no_auth",
        "license": "ODbL",
        "productionInput": False,  # enable only when a concrete field adds value
        "reason": "free community API; useful mainly for fixtures/results, not micro-events",
    },
    "pappalardo_wyscout_open": {
        "role": "historical_event_model_calibration",
        "allowed": True,
        "costModel": "open_dataset",
        "license": "CC BY 4.0",
        "productionInput": False,
        "reason": "Bundesliga 2017/18 event data; calibration only, not current-season truth",
    },
    "statsbomb_open_data": {
        "role": "event_schema_and_model_calibration",
        "allowed": True,
        "costModel": "open_dataset",
        "license": "custom_attribution_terms",
        "productionInput": False,
        "reason": "open event data for listed competitions; use only where competition/season is actually present",
    },
    "sportmonks": {
        "role": "none",
        "allowed": False,
        "costModel": "freemium_paid_bundesliga",
        "productionInput": False,
        "reason": "Bundesliga access is paid; violates V43 zero-paid-tier dependency rule",
    },
}


# ============================================================
# V44 OPEN-DATA CALIBRATION LAYER
# ============================================================
# Important separation:
# - observed/current data may contribute directly to a projection.
# - historical open datasets may calibrate priors only.
# - calibration data NEVER masquerades as a current-season observed event.
#
# The initial priors are deliberately conservative. They are fallback expectations
# for missing event families, not replacements for observed Bundesliga data.
V44_CALIBRATION_PROVIDERS = {
    "pappalardo_wyscout_open": {
        "scope": "historical_event_calibration",
        "season": "2017/18",
        "competition": "Bundesliga",
        "license": "CC BY 4.0",
        "currentTruth": False,
        "weight": 1.0,
    },
    "statsbomb_open_data": {
        "scope": "event_schema_calibration",
        "season": None,
        "competition": "selected_open_competitions",
        "license": "attribution_required",
        "currentTruth": False,
        "weight": 0.35,
    },
}

# Neutral per-90 fallback rates. These are intentionally modest and are only
# activated for a metric that is absent from the current player's observed data.
# They can later be regenerated from downloaded open datasets without changing
# the projection interface.
V44_POSITION_EVENT_PRIORS_PER90 = {
    "TW": {
        "passAccuracy": 0.78,
        "cleanSheets": 0.27,
    },
    "ABW": {
        "passAccuracy": 0.82,
        "cleanSheets": 0.27,
        "duelsWon": 3.2,
        "aerialDuelsWon": 1.8,
    },
    "MF": {
        "passAccuracy": 0.83,
        "duelsWon": 3.0,
        "aerialDuelsWon": 0.9,
    },
    "ANG": {
        "passAccuracy": 0.78,
        "shotsOnTarget": 1.15,
        "duelsWon": 2.0,
        "aerialDuelsWon": 0.7,
    },
}

V44_ESTIMATE_POINT_WEIGHTS = {
    # Only event families already represented by the model are assigned a
    # conservative points conversion. passAccuracy remains informational until
    # we have the exact underlying pass volume / success counts.
    "cleanSheets": {"TW": 50.0, "ABW": 30.0, "MF": 20.0, "ANG": 10.0},
    "shotsOnTarget": {"TW": 0.0, "ABW": 12.0, "MF": 12.0, "ANG": 12.0},
    "duelsWon": {"TW": 0.5, "ABW": 1.3, "MF": 1.0, "ANG": 0.7},
    "aerialDuelsWon": {"TW": 0.8, "ABW": 1.5, "MF": 0.8, "ANG": 0.9},
}


# ============================================================
# V46 CANONICAL ACTUAL-FACT LAYER
# ============================================================
# Goal: every occurrence stat shown in the UI follows ONE rule:
#   observed current-season count + explicit source => show integer
#   otherwise => unknown
# No model estimate / prior / rate may leak into these fields.

V46_ACTUAL_COUNT_FIELDS = ("goals", "goalsAgainst", "yellowCards")

def v46_actual_count_fact(metric, performance, performance_sources, profile_values=None):
    performance = performance or {}
    performance_sources = performance_sources or {}
    profile_values = profile_values or {}

    # 1) Explicit current-season performance source.
    value = performance.get(metric)
    source = performance_sources.get(metric)
    if value is not None and source:
        try:
            number = float(value)
            rounded = round(number)
            if number >= 0 and abs(number - rounded) < 1e-9:
                return {
                    "value": int(rounded),
                    "status": "observed",
                    "source": source,
                    "origin": "current_performance",
                }
        except (TypeError, ValueError):
            pass

    # 2) Explicit official player-profile value.
    profile_value = profile_values.get(metric)
    profile_source = profile_values.get("sourceUrl")
    if profile_value is not None and profile_source:
        try:
            number = float(profile_value)
            rounded = round(number)
            if number >= 0 and abs(number - rounded) < 1e-9:
                return {
                    "value": int(rounded),
                    "status": "observed",
                    "source": profile_source,
                    "origin": "official_profile",
                }
        except (TypeError, ValueError):
            pass

    return {
        "value": None,
        "status": "unknown",
        "source": None,
        "origin": None,
    }

def v46_build_actual_facts(performance, performance_sources, profile_values=None):
    return {
        metric: v46_actual_count_fact(
            metric, performance, performance_sources, profile_values
        )
        for metric in V46_ACTUAL_COUNT_FIELDS
    }


# ============================================================
# V52 CURRENT-SEASON FACT GUARD
# ============================================================
# Purpose:
# - UI occurrence facts must represent CURRENT_SEASON only.
# - historical/prior values may still be used by the projection as priors,
#   but must never leak into goals / yellowCards / goalsAgainst shown as
#   current observed facts.
# - Bundesliga ranking pages can occasionally expose stale/previous-season
#   values under a current-season URL. We therefore combine provenance with
#   a conservative match-count sanity check.

def v52_completed_team_matches(club_name, matches):
    completed = 0
    for match in matches or []:
        if not isinstance(match, dict) or not match.get("matchIsFinished"):
            continue
        team1 = (match.get("team1") or {}).get("teamName", "")
        team2 = (match.get("team2") or {}).get("teamName", "")
        if names_match(club_name, team1) or names_match(club_name, team2):
            if _openligadb_final_score(match) is not None:
                completed += 1
    return completed


def v52_guard_current_season_count(metric, value, source, club_name, matches):
    """
    Return (safe_value, safe_source, evidence).

    Conservative guard for UI/current-performance counts. It does not try to
    estimate a missing value. A rejected value becomes unknown instead.
    """
    if value is None:
        return None, None, "missing"

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, None, "non_numeric"

    rounded = round(number)
    if number < 0 or abs(number - rounded) > 1e-9:
        return None, None, "not_nonnegative_integer"

    value = int(rounded)
    source_text = str(source or "")

    # Explicit prior-season provenance is never current truth.
    if BUNDESLIGA_PRIOR_SEASON in source_text:
        return None, None, "rejected_prior_season_source"

    team_matches = v52_completed_team_matches(club_name, matches)

    # Before a completed match, only an explicit zero can be current truth.
    if team_matches == 0:
        if value == 0 and source:
            return 0, source, "current_zero_before_first_completed_match"
        return None, None, f"rejected_no_completed_matches:value={value}"

    # Conservative hard sanity bounds. These are NOT forecasts; they only
    # reject impossible/stale season totals such as Kane=36 goals after MD1.
    if metric == "goals":
        max_plausible = 6 * team_matches
    elif metric == "yellowCards":
        max_plausible = 2 * team_matches
    elif metric == "appearances":
        max_plausible = team_matches
    else:
        max_plausible = None

    if max_plausible is not None and value > max_plausible:
        return (
            None,
            None,
            f"rejected_stale_or_implausible:{value}>{max_plausible};"
            f"completedMatches={team_matches}",
        )

    return value, source, f"accepted_current_season;completedMatches={team_matches}"


def v52_sanitize_current_performance(performance, performance_sources, club_name, matches):
    performance = dict(performance or {})
    performance_sources = dict(performance_sources or {})
    evidence = {}

    for metric in ("goals", "yellowCards", "appearances"):
        safe_value, safe_source, reason = v52_guard_current_season_count(
            metric,
            performance.get(metric),
            performance_sources.get(metric),
            club_name,
            matches,
        )
        evidence[metric] = reason
        performance[metric] = safe_value
        if safe_source:
            performance_sources[metric] = safe_source
        else:
            performance_sources.pop(metric, None)

    return performance, performance_sources, evidence


def v44_missing_event_prior(position, missing_metrics, minutes=90.0):
    """
    Return transparent, conservative priors for genuinely missing event metrics.
    No value is labelled observed. The points estimate is separately capped.
    """
    pos = position if position in V44_POSITION_EVENT_PRIORS_PER90 else "ANG"
    priors = V44_POSITION_EVENT_PRIORS_PER90.get(pos, {})
    scale = max(0.0, min(float(minutes or 0.0) / 90.0, 1.0))
    estimates = {}
    raw_points = 0.0

    for metric in sorted(set(missing_metrics or [])):
        if metric not in priors:
            continue
        rate90 = float(priors[metric])
        expected = rate90 * scale
        estimates[metric] = {
            "expected": round(expected, 4),
            "per90": rate90,
            "observed": False,
            "sourceType": "historical_open_data_prior",
            "confidence": "low",
        }
        weight = (V44_ESTIMATE_POINT_WEIGHTS.get(metric) or {}).get(pos)
        if weight is not None:
            raw_points += expected * float(weight)

    # Missing-event priors must remain a secondary correction, never dominate
    # the observed-event model.
    points = max(0.0, min(raw_points, 18.0))
    return {
        "position": pos,
        "minutes": round(float(minutes or 0.0), 1),
        "estimates": estimates,
        "rawEstimatedPoints": round(raw_points, 1),
        "cappedEstimatedPoints": round(points, 1),
        "cap": 18.0,
        "observed": False,
        "providers": list(V44_CALIBRATION_PROVIDERS),
    }

def v44_apply_missing_event_calibration(projection, position, missing_metrics):
    """
    Adds a clearly separated calibration object to a projection.
    For safety V44 does NOT silently mutate expectedPoints yet.
    First we validate the priors in logs across TW/ABW/MF/ANG.
    """
    if not isinstance(projection, dict):
        return projection
    minutes = projection.get("minutesIfStart")
    if minutes is None:
        minutes = projection.get("minStart")
    if minutes is None:
        minutes = 90.0
    calibration = v44_missing_event_prior(position, missing_metrics, minutes)
    projection["openDataCalibration"] = calibration
    projection["openDataCalibrationAppliedToExpectedPoints"] = False
    return projection

def v43_provider_allowed(provider_name, production=False):
    meta = V43_PROVIDER_REGISTRY.get(provider_name) or {}
    if not meta.get("allowed"):
        return False
    if production and not meta.get("productionInput"):
        return False
    return True

def v43_provenance(value, provider, field, scope, season=None, confidence="unknown",
                   source_url=None, observed=True):
    """Wrap a sourced value without silently losing where it came from."""
    return {
        "value": value,
        "provider": provider,
        "field": field,
        "scope": scope,
        "season": season,
        "confidence": confidence,
        "sourceUrl": source_url,
        "observed": bool(observed),
    }

def v43_resolve_sourced_value(candidates):
    """
    Conservative conflict resolver.
    Never averages disagreeing observed providers.
    Returns the highest-confidence candidate only when values agree or there is
    a single observed candidate; otherwise returns an explicit conflict.
    """
    valid = [c for c in (candidates or []) if isinstance(c, dict) and c.get("observed")
             and c.get("value") is not None]
    if not valid:
        return {"status": "unknown", "value": None, "sources": []}

    values = {str(c.get("value")) for c in valid}
    if len(values) > 1:
        return {
            "status": "conflict",
            "value": None,
            "sources": valid,
        }

    rank = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
    chosen = max(valid, key=lambda c: rank.get(c.get("confidence"), 0))
    return {
        "status": "verified" if len(valid) > 1 else "single_source",
        "value": chosen.get("value"),
        "chosen": chosen,
        "sources": valid,
    }

def v43_provider_policy_summary():
    allowed = [k for k, v in V43_PROVIDER_REGISTRY.items() if v.get("allowed")]
    blocked = [k for k, v in V43_PROVIDER_REGISTRY.items() if not v.get("allowed")]
    production = [k for k, v in V43_PROVIDER_REGISTRY.items()
                  if v.get("allowed") and v.get("productionInput")]
    return {
        "allowed": allowed,
        "blocked": blocked,
        "productionEnabled": production,
        "rule": "no_freemium_paid_dependency",
    }

BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"
KICKBASE_MATCH_HISTORY_FILE = BASE_DIR / "kickbase-match-history.json"
# Optional: IDs des aktuellen Kickbase-Kaders für intensive Web-Recherche.
# Die Datei ist bewusst optional, weil GitHub Actions keinen Zugriff auf browser-localStorage hat.
ACTIVE_ROSTER_FILE = BASE_DIR / "kickbase-roster.json"

OPENLIGADB_URL = "https://api.openligadb.de"
BUNDESLIGA_SHORTCUT = "bl1"
CURRENT_SEASON = "2026/27"
OPENLIGADB_SEASON = 2026

# ============================================================
# OFFIZIELLE BUNDESLIGA-QUELLEN
# ============================================================

BUNDESLIGA_CLUBS_URL = (
    "https://www.bundesliga.com/de/bundesliga/clubs"
)

BUNDESLIGA_PLAYERS_URL = (
    "https://www.bundesliga.com/de/bundesliga/spieler"
)

# OpenLigaDB bleibt die Quelle für den Spielplan.
BUNDESLIGA_SHORTCUT = "bl1"
CURRENT_SEASON = "2026/27"
OPENLIGADB_SEASON = 2026

# Offizielle Bundesliga-Namen -> stabile JSON-Namen.
BUNDESLIGA_NAME_MAP = {
    "fc augsburg": "FC Augsburg",
    "1 fc union berlin": "1. FC Union Berlin",
    "sv werder bremen": "SV Werder Bremen",
    "borussia dortmund": "Borussia Dortmund",
    # Bundesliga.com / OpenLigaDB verwenden hier teils
    # unterschiedliche Schreibweisen. Alle Varianten werden
    # auf den Namen des jeweiligen Bundesliga-Spielplans
    # vereinheitlicht.
    "sv elversberg": "SV 07 Elversberg",
    "sv 07 elversberg": "SV 07 Elversberg",

    "eintracht frankfurt": "Eintracht Frankfurt",

    "sport club freiburg": "SC Freiburg",
    "sc freiburg": "SC Freiburg",
    "hamburger sv": "Hamburger SV",
    "tsg hoffenheim": "TSG Hoffenheim",
    "1 fc koln": "1. FC Köln",
    "rb leipzig": "RB Leipzig",
    "bayer 04 leverkusen": "Bayer 04 Leverkusen",
    "1 fsv mainz 05": "1. FSV Mainz 05",
    "borussia monchengladbach": "Borussia Mönchengladbach",
    "fc bayern munchen": "FC Bayern München",
    "sc paderborn 07": "SC Paderborn 07",
    "fc schalke 04": "FC Schalke 04",
    "vfb stuttgart": "VfB Stuttgart",
}

POSITION_MAP = {
    "torhüter": "Torhüter",
    "torhueter": "Torhüter",
    "verteidigung": "Verteidigung",
    "mittelfeld": "Mittelfeld",
    "angriff": "Angriff",
}


# ============================================================
# OFFIZIELLE PERSONAL-/STATUSQUELLEN – GENERISCH
# ============================================================
#
# Ziel:
# - für JEDEN Kaderspieler dieselbe Logik
# - nur offizielle Vereinsdomains als Statusquelle
# - Sitemap / News-Seiten zuerst, Suchmaschine nur als Link-Fallback
# - keine spielerspezifischen Hardcodes
#
# Bereits bestätigte Verletzungen aus der vorhandenen JSON werden solange
# beibehalten, bis eine neuere belastbare Rückkehr-/Fit-Meldung gefunden wird.

OFFICIAL_CLUB_DOMAINS = {
    "FC Augsburg": "fcaugsburg.de",
    "1. FC Union Berlin": "fc-union-berlin.de",
    "SV Werder Bremen": "werder.de",
    "Borussia Dortmund": "bvb.de",
    "SV 07 Elversberg": "sv07elversberg.de",
    "Eintracht Frankfurt": "eintracht.de",
    "SC Freiburg": "scfreiburg.com",
    "Hamburger SV": "hsv.de",
    "TSG Hoffenheim": "tsg-hoffenheim.de",
    "1. FC Köln": "fc.de",
    "RB Leipzig": "rbleipzig.com",
    "Bayer 04 Leverkusen": "bayer04.de",
    "1. FSV Mainz 05": "mainz05.de",
    "Borussia Mönchengladbach": "borussia.de",
    "FC Bayern München": "fcbayern.com",
    "SC Paderborn 07": "scp07.de",
    "FC Schalke 04": "schalke04.de",
    "VfB Stuttgart": "vfb.de",
}

# Bekannte offizielle Seiten dienen nur als zusätzliche Seeds.
# Keine dieser Seiten ist spielerspezifisch im Auswertungsalgorithmus.
OFFICIAL_STATUS_SEEDS = {
    "SV Werder Bremen": [
        "https://www.werder.de/news/maenner/2026-2027/personal-update-260826",
        "https://www.werder.de/news/maenner/2026-2027/personal-lueneburg-20082026",
    ],
}

STATUS_URL_KEYWORDS = (
    "personal",
    "verletz",
    "reha",
    "training",
    "aufstellung",
    "kader",
    "vorbericht",
    "presse",
    "spieltag",
    "team",
    "news",
)

SEARCH_URL = "https://html.duckduckgo.com/html/"

# V13 FAST MODE
# Ziel: kompletter Kader-Refresh in wenigen Minuten statt >15 Minuten.
STATUS_SEARCH_TIMEOUT = 5
STATUS_PAGE_TIMEOUT = 6
STATUS_MAX_SEARCH_QUERIES = 1
STATUS_MAX_CANDIDATE_URLS = 4
STATUS_MAX_PAGES_PER_PLAYER = 3
STATUS_GLOBAL_BUDGET_SECONDS = 120

# V14: eine einzige Bundesliga-Spieltagseite statt Websuche pro Spieler.
BUNDESLIGA_MATCHDAY_STATUS_TEMPLATE = (
    "https://www.bundesliga.com/de/bundesliga/news/"
    "voraussichtliche-aufstellungen-spieltag-verletzungen-"
    "sperren-ubersicht-{matchday}-24397"
)

_MATCHDAY_STATUS_CACHE = {}
_TEAMCHECK_CACHE = {}

# V19: Ligaweite Bundesliga-Statistikseiten werden pro Kategorie nur EINMAL
# geladen und anschließend für alle aktiven Kaderspieler wiederverwendet.
_BUNDESLIGA_STATS_TEXT_CACHE = {}
_BUNDESLIGA_HISTORICAL_STATS_TEXT_CACHE = {}

BUNDESLIGA_STATS_SEASON = "2026-2027"
BUNDESLIGA_PRIOR_SEASON = "2025-2026"

BUNDESLIGA_PLAYER_STAT_CATEGORIES = {
    "goals": {
        "slug": "tore",
        "heading": "Tore",
        "kind": "count",
    },
    "assists": {
        "slug": "vorlagen",
        "heading": "Vorlagen",
        "kind": "count",
    },
    "shots": {
        "slug": "torschuesse",
        "heading": "Torschüsse",
        "kind": "count",
    },
    "woodwork": {
        "slug": "pfosten-oder-lattentreffer",
        "heading": "Pfosten- oder Lattentreffer",
        "kind": "count",
    },
    "penalties": {
        "slug": "elfmeter",
        "heading": "Elfmeter",
        "kind": "count",
    },
    "penaltiesScored": {
        "slug": "verwandelte-elfmeter",
        "heading": "Verwandelte Elfmeter",
        "kind": "count",
    },
    "passAccuracy": {
        "slug": "passquote",
        "heading": "Passquote",
        "kind": "rate",
    },
    "duelsWon": {
        "slug": "gewonnene-zweikaempfe",
        "heading": "Gewonnene Zweikämpfe",
        "kind": "count",
    },
    "aerialDuelsWon": {
        "slug": "gewonnene-kopfballduelle",
        "heading": "Gewonnene Kopfballduelle",
        "kind": "count",
    },
    "crosses": {
        "slug": "flanken-aus-dem-spiel",
        "heading": "Flanken aus dem Spiel",
        "kind": "count",
    },
    "yellowCards": {
        "slug": "gelbe-karten",
        "heading": "Gelbe Karten",
        "kind": "count",
    },
    "cards": {
        "slug": "karten",
        "heading": "Karten",
        "kind": "count",
    },
    "fouls": {
        "slug": "fouls-am-gegner",
        "heading": "Fouls am Gegner",
        "kind": "count",
    },
    "saves": {
        "slug": "gehaltene-torschuesse",
        "heading": "Gehaltene Torschüsse",
        "kind": "count",
    },
    "distanceKm": {
        "slug": "laufdistanz",
        "heading": "Laufdistanz (km)",
        "kind": "rate",
    },
    "sprints": {
        "slug": "sprints",
        "heading": "Sprints",
        "kind": "count",
    },
    "intensiveRuns": {
        "slug": "intensive-laeufe",
        "heading": "Intensive Läufe",
        "kind": "count",
    },
    "topSpeedKmh": {
        "slug": "top-speed",
        "heading": "Top-Speed (km/h)",
        "kind": "rate",
    },
}

# Historical prior: intentionally smaller core set to keep the workflow fast.
BUNDESLIGA_PRIOR_STAT_CATEGORIES = {
    key: BUNDESLIGA_PLAYER_STAT_CATEGORIES[key]
    for key in (
        "goals",
        "assists",
        "shots",
        "passAccuracy",
        "duelsWon",
        "aerialDuelsWon",
        "fouls",
        "saves",
        "distanceKm",
        "sprints",
        "intensiveRuns",
    )
}


BUNDESLIGA_CLUB_NEWS_SLUGS = {
    "FC Bayern München": "fc-bayern-muenchen",
    "VfB Stuttgart": "vfb-stuttgart",
    "RB Leipzig": "rb-leipzig",
    "Borussia Mönchengladbach": "borussia-moenchengladbach",
    "1. FSV Mainz 05": "1-fsv-mainz-05",
    "SC Paderborn 07": "sc-paderborn-07",
    "1. FC Union Berlin": "1-fc-union-berlin",
    "Eintracht Frankfurt": "eintracht-frankfurt",
    "1. FC Köln": "1-fc-koeln",
    "TSG Hoffenheim": "tsg-hoffenheim",
    "SV 07 Elversberg": "sv-elversberg",
    "Bayer 04 Leverkusen": "bayer-04-leverkusen",
    "Borussia Dortmund": "borussia-dortmund",
    "Hamburger SV": "hamburger-sv",
    "SC Freiburg": "sc-freiburg",
    "SV Werder Bremen": "sv-werder-bremen",
    "FC Augsburg": "fc-augsburg",
    "FC Schalke 04": "fc-schalke-04",
}

HISTORICAL_MATCH_EVIDENCE_CACHE = {}
V49_CURRENT_LINEUP_CACHE = {}
BUNDESLIGA_CLUB_SCHEDULE_SLUGS = {
    "SV 07 Elversberg": "sv-elversberg",
    "SV Elversberg": "sv-elversberg",
    "Hamburger SV": "hamburger-sv",
    "1. FC Köln": "1-fc-koeln",
    "FC Schalke 04": "fc-schalke-04",
    "SC Paderborn 07": "sc-paderborn-07",
}

_STATUS_RESEARCH_STARTED_AT = None

LEGACY_NO_INJURY_TEXTS = {
    "keine verletzungsmeldung auf dem profil gefunden",
    "keine verletzungsmeldung gefunden",
}

LEGACY_NO_SUSPENSION_TEXTS = {
    "keine sperrmeldung auf dem profil gefunden",
    "keine sperrmeldung gefunden",
}

INJURY_DIAGNOSES = (
    ("kreuzbandriss", "Kreuzbandriss"),
    ("riss des vorderen kreuzbandes", "Kreuzbandriss"),
    ("muskelfaserriss", "Muskelfaserriss"),
    ("muskelbündelriss", "Muskelbündelriss"),
    ("muskelverletzung", "Muskelverletzung"),
    ("knieverletzung", "Knieverletzung"),
    ("sprunggelenksverletzung", "Sprunggelenksverletzung"),
    ("schulterverletzung", "Schulterverletzung"),
    ("bänderriss", "Bänderriss"),
    ("baenderriss", "Bänderriss"),
    ("fraktur", "Fraktur"),
)

CURRENT_INJURY_TERMS = (
    "verletzt",
    "verletzung",
    "fällt aus",
    "faellt aus",
    "fehlt weiterhin",
    "nicht zur verfügung",
    "nicht zur verfuegung",
    "in der reha",
    "in reha",
    "rehabilitation",
)

CURRENT_SUSPENSION_TERMS = (
    "gesperrt",
    "sperre",
    "rotsperre",
    "gelbsperre",
    "5. gelbe karte",
    "fünfte gelbe karte",
    "fuenfte gelbe karte",
)

RECOVERY_TERMS = (
    "wieder im mannschaftstraining",
    "zurück im mannschaftstraining",
    "zurueck im mannschaftstraining",
    "wieder einsatzbereit",
    "voll belastbar",
)

# ============================================================
# ALLGEMEINE HILFSFUNKTIONEN
# ============================================================

def normalize_name(value):
    value = value or ""

    value = unicodedata.normalize(
        "NFKD",
        str(value),
    )

    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )

    value = value.lower()
    value = value.replace("&", " and ")

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def names_match(a, b):
    a = normalize_name(a)
    b = normalize_name(b)

    if not a or not b:
        return False

    return (
        a == b
        or a in b
        or b in a
    )


def http_get_json(
    url,
    headers=None,
    timeout=30,
):
    request = Request(
        url,
        headers={
            "User-Agent": "kickbase-ai/2.0",
            "Accept": "application/json",
            **(headers or {}),
        },
    )

    with urlopen(
        request,
        timeout=timeout,
    ) as response:

        body = response.read().decode(
            "utf-8"
        )

        return json.loads(body)


def http_get_text(
    url,
    headers=None,
    timeout=30,
):
    """
    Holt HTML/Text von einer Webseite.
    """

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; Kickbase-AI/2.0)"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/json;q=0.9,*/*;q=0.8"
            ),
            **(headers or {}),
        },
    )

    with urlopen(
        request,
        timeout=timeout,
    ) as response:
        return response.read().decode(
            "utf-8",
            "ignore",
        )


class BundesligaClubParser(HTMLParser):
    """
    Extrahiert Club-Überschriften aus Bundesliga.com.
    """

    def __init__(self):
        super().__init__()
        self.club_names = []
        self._heading_depth = 0
        self._heading_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3"):
            self._heading_depth = 1
            self._heading_parts = []

    def handle_data(self, data):
        value = " ".join(data.split())

        if self._heading_depth and value:
            self._heading_parts.append(value)

    def handle_endtag(self, tag):
        if (
            self._heading_depth
            and tag in ("h2", "h3")
        ):
            name = " ".join(
                self._heading_parts
            ).strip()

            if name:
                self.club_names.append(name)

            self._heading_depth = 0
            self._heading_parts = []


class BundesligaPlayersParser(HTMLParser):
    """
    Robuster Parser für Bundesliga.com.

    Wichtig: Die Clubnamen werden NICHT mehr nur anhand von
    h2/h3 erkannt. Bundesliga.com kann die Überschriften je
    nach Seite unterschiedlich ausliefern. Deshalb erkennen
    wir Clubnamen und Positionsüberschriften anhand ihres
    Textinhalts.
    """

    def __init__(self):
        super().__init__()

        self.players = []
        self.current_club = None
        self.current_position = None

        self._anchor_href = None
        self._anchor_parts = []

        self._tag_stack = []

        # Sowohl alle bekannten Aliasnamen (Dictionary-Keys)
        # als auch die kanonischen Namen (Dictionary-Values)
        # müssen erkannt werden. Genau das war der Fehler bei
        # SV Elversberg und Sport-Club Freiburg:
        #
        # Bundesliga.com: "SV Elversberg"
        # intern:         "SV 07 Elversberg"
        #
        # Bundesliga.com: "Sport-Club Freiburg"
        # intern:         "SC Freiburg"
        self.club_lookup = {}

        for alias, canonical in (
            BUNDESLIGA_NAME_MAP.items()
        ):
            self.club_lookup[
                normalize_name(alias)
            ] = canonical

            self.club_lookup[
                normalize_name(canonical)
            ] = canonical

        self.position_lookup = {
            normalize_name(
                value
            ): value
            for value in POSITION_MAP.values()
        }

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        self._tag_stack.append(tag)

        if tag == "a":
            href = attrs.get("href", "")

            if (
                href
                and "/spieler/" in href
            ):
                self._anchor_href = href
                self._anchor_parts = []

    def handle_data(self, data):
        value = " ".join(data.split())

        if not value:
            return

        normalized = normalize_name(value)

        # --------------------------------------------------------
        # Club erkennen – unabhängig von h2/h3/div/span usw.
        # --------------------------------------------------------
        mapped_club = self.club_lookup.get(
            normalized
        )

        if mapped_club:
            self.current_club = mapped_club
            self.current_position = None

        # --------------------------------------------------------
        # Position erkennen
        # --------------------------------------------------------
        mapped_position = self.position_lookup.get(
            normalized
        )

        if mapped_position:
            self.current_position = mapped_position

        # --------------------------------------------------------
        # Spieler-Link sammeln
        # --------------------------------------------------------
        if self._anchor_href is not None:
            self._anchor_parts.append(value)

    def handle_endtag(self, tag):
        if (
            tag == "a"
            and self._anchor_href is not None
        ):
            raw_text = " ".join(
                self._anchor_parts
            ).strip()

            if (
                raw_text
                and self.current_club
                and self.current_position
            ):
                number = None
                name = raw_text

                # Bundesliga.com kann Nummer und Name ohne
                # Trennzeichen ausliefern, z.B. "1FinnDahmen".
                match = re.match(
                    r"^(\d{1,2})(.+)$",
                    raw_text,
                )

                if match:
                    number = int(
                        match.group(1)
                    )
                    name = match.group(2).strip()

                # Eventuelle zusammengeklebte Namen etwas
                # lesbarer machen.
                name = re.sub(
                    r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])",
                    " ",
                    name,
                )

                href = urljoin(
                    BUNDESLIGA_PLAYERS_URL,
                    self._anchor_href,
                )

                slug = (
                    self._anchor_href
                    .rstrip("/")
                    .split("/")[-1]
                )

                self.players.append(
                    {
                        "club": self.current_club,
                        "position": self.current_position,
                        "id": slug or href,
                        "name": name,
                        "number": number,
                        "sourceUrl": href,
                    }
                )

            self._anchor_href = None
            self._anchor_parts = []

        if self._tag_stack:
            # HTMLParser kann bei fehlerhaftem HTML verschachtelte
            # Tags sehen; deshalb nur den letzten Tag entfernen.
            if self._tag_stack[-1] == tag:
                self._tag_stack.pop()
            elif tag in self._tag_stack:
                self._tag_stack.remove(tag)




def get_bundesliga_squads():
    """
    Holt alle Bundesliga-Kader direkt von Bundesliga.com.
    """

    try:
        html = http_get_text(
            BUNDESLIGA_PLAYERS_URL
        )

        parser = BundesligaPlayersParser()
        parser.feed(html)

        squads = {
            team_name: []
            for team_name in BUNDESLIGA_NAME_MAP.values()
        }

        for player in parser.players:
            club = player["club"]

            if club not in squads:
                squads[club] = []

            squads[club].append(
                {
                    "id": player["id"],
                    "name": player["name"],
                    "age": None,
                    "number": player["number"],
                    "position": player["position"],
                    "photo": None,
                    "injury": None,
                    "sourceUrl": player["sourceUrl"],
                }
            )

        for team_name, players in squads.items():
            unique = {}

            for player in players:
                key = (
                    normalize_name(
                        player.get("name")
                    ),
                    player.get("position"),
                )
                unique[key] = player

            squads[team_name] = list(
                unique.values()
            )

        total_players = sum(
            len(players)
            for players in squads.values()
        )

        print(
            "Bundesliga.com: "
            f"{len(parser.players)} Spieler gefunden."
        )
        print(
            "Bundesliga.com: "
            f"{total_players} Spieler nach Bereinigung."
        )

        for team_name in BUNDESLIGA_NAME_MAP.values():
            count = len(
                squads.get(
                    team_name,
                    [],
                )
            )
            print(
                f"  - {team_name}: "
                f"{count} Spieler"
            )

        return squads

    except Exception as exc:
        print(
            "Bundesliga.com Kader "
            f"fehlgeschlagen: {exc}"
        )
        return {}


# ============================================================
# OPENLIGADB
# ============================================================

def openligadb_get(endpoint):
    url = (
        f"{OPENLIGADB_URL}/"
        f"{endpoint.lstrip('/')}"
    )

    return http_get_json(url)


def get_bundesliga_teams():
    """
    Holt die aktuelle Clubübersicht direkt von Bundesliga.com.

    Keine externe Team-ID-Suche mehr.
    Fallback: OpenLigaDB.
    """

    try:
        html = http_get_text(
            BUNDESLIGA_CLUBS_URL
        )

        parser = BundesligaClubParser()
        parser.feed(html)

        teams = []
        seen = set()

        for raw_name in parser.club_names:
            key = normalize_name(raw_name)
            team_name = BUNDESLIGA_NAME_MAP.get(
                key,
                raw_name.strip(),
            )

            normalized_team = normalize_name(
                team_name
            )

            if normalized_team in seen:
                continue

            seen.add(normalized_team)

            teams.append(
                {
                    "teamName": team_name,
                    "source": "Bundesliga.com",
                }
            )

        if len(teams) == 18:
            print(
                "Bundesliga.com: 18 Clubs gefunden."
            )
            return teams

        print(
            "WARNUNG: Bundesliga.com lieferte "
            f"{len(teams)} Clubs statt 18."
        )

    except Exception as exc:
        print(
            "Bundesliga.com Clubübersicht "
            f"fehlgeschlagen: {exc}"
        )

    try:
        matches = openligadb_get(
            f"/getmatchdata/"
            f"{BUNDESLIGA_SHORTCUT}/"
            f"{OPENLIGADB_SEASON}"
        )

        discovered = {}

        for match in matches or []:
            for side in ("team1", "team2"):
                team = match.get(
                    side,
                    {},
                )
                name = team.get(
                    "teamName",
                    "",
                )

                if name:
                    discovered[
                        normalize_name(name)
                    ] = {
                        "teamName": name,
                        "source": "OpenLigaDB",
                    }

        teams = list(
            discovered.values()
        )

        print(
            "OpenLigaDB-Fallback: "
            f"{len(teams)} Clubs gefunden."
        )

        return teams

    except Exception as exc:
        print(
            "OpenLigaDB-Fallback "
            f"fehlgeschlagen: {exc}"
        )
        return []




def get_bundesliga_matches():
    """
    Holt den Spielplan 2026/27 von OpenLigaDB.
    """

    try:
        matches = openligadb_get(
            f"/getmatchdata/"
            f"{BUNDESLIGA_SHORTCUT}/"
            f"{OPENLIGADB_SEASON}"
        )

        if not matches:
            return []

        return matches

    except Exception as exc:
        print(
            f"OpenLigaDB Fehler: {exc}"
        )
        return []


def parse_match_datetime(match):
    value = (
        match.get("matchDateTimeUTC")
        or match.get("matchDateTime")
        or ""
    )

    if not value:
        return None

    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(
                value.replace(
                    "Z",
                    "+00:00",
                )
            )

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(
                tzinfo=timezone.utc
            )

        return dt

    except ValueError:
        return None



def _openligadb_final_score(match):
    """Return confirmed final score (team1, team2), otherwise None."""
    if not isinstance(match, dict):
        return None

    results = match.get("matchResults") or []
    valid = []
    for result in results:
        if not isinstance(result, dict):
            continue
        p1 = result.get("pointsTeam1")
        p2 = result.get("pointsTeam2")
        if p1 is None or p2 is None:
            continue
        result_type = result.get("resultTypeID")
        result_name = normalize_name(result.get("resultName") or "")
        is_final = (
            result_type == 2
            or "endergebnis" in result_name
            or "endstand" in result_name
            or "final" in result_name
        )
        valid.append((is_final, p1, p2))

    for is_final, p1, p2 in valid:
        if is_final:
            try:
                return int(p1), int(p2)
            except (TypeError, ValueError):
                return None

    if match.get("matchIsFinished") and len(valid) == 1:
        try:
            return int(valid[0][1]), int(valid[0][2])
        except (TypeError, ValueError):
            return None

    return None


def v48_resolve_goalkeeper_goals_against(player, club_name, matches, public_player):
    """
    Exact-count resolver:
    only sum team goals conceded when the official player profile proves
    complete goalkeeper coverage for every completed league match so far.
    """
    if not _is_goalkeeper(player):
        return {
            "value": None,
            "status": "not_applicable",
            "source": None,
            "evidence": "not_goalkeeper",
        }

    completed = []
    for match in matches or []:
        if not isinstance(match, dict) or not match.get("matchIsFinished"):
            continue
        team1 = (match.get("team1") or {}).get("teamName", "")
        team2 = (match.get("team2") or {}).get("teamName", "")
        if not (names_match(club_name, team1) or names_match(club_name, team2)):
            continue

        score = _openligadb_final_score(match)
        if score is None:
            continue

        g1, g2 = score
        conceded = g2 if names_match(club_name, team1) else g1
        completed.append(conceded)

    if not completed:
        return {
            "value": None,
            "status": "unknown",
            "source": None,
            "evidence": "no_completed_team_matches",
        }

    def _num(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    appearances = _num(public_player.get("appearances"))
    starts = _num(public_player.get("starts"))
    minutes = _num(public_player.get("minutes"))
    team_matches = len(completed)

    appeared_all = appearances is not None and int(appearances) == team_matches
    started_all = starts is not None and int(starts) == team_matches
    full_minutes = minutes is not None and minutes >= (89.0 * team_matches)

    lineup_evidence = _v49_current_official_gk_start_evidence(
        player,
        club_name,
        team_matches,
    )
    official_starts_all = (
        lineup_evidence.get("status") == "complete"
        and int(lineup_evidence.get("startsProven") or 0) >= team_matches
    )

    # Exact current count is allowed when either:
    # A) profile minutes/starts prove full coverage, or
    # B) official current Bundesliga lineup pages prove the keeper started
    #    every completed league match AND the profile appearances count does
    #    not contradict that evidence.
    profile_complete = appeared_all and (started_all or full_minutes)
    lineup_complete = official_starts_all and (
        appearances is None or int(appearances) == team_matches
    )

    if not (profile_complete or lineup_complete):
        return {
            "value": None,
            "status": "unknown",
            "source": None,
            "evidence": (
                f"coverage_not_proven:teamMatches={team_matches},"
                f"appearances={appearances},starts={starts},minutes={minutes};"
                f"lineups={lineup_evidence.get('startsProven')}/"
                f"{lineup_evidence.get('matchesRequired')};"
                f"lineupStatus={lineup_evidence.get('status')}"
            ),
            "lineupEvidence": lineup_evidence,
        }

    source = (
        "OpenLigaDB + Bundesliga.com Lineups"
        if lineup_complete and not profile_complete
        else "OpenLigaDB"
    )
    return {
        "value": int(sum(completed)),
        "status": "observed",
        "source": source,
        "evidence": (
            f"complete_keeper_coverage:{team_matches}_matches;"
            f"appearances={appearances};starts={starts};minutes={minutes};"
            f"officialLineups={lineup_evidence.get('startsProven')}/"
            f"{lineup_evidence.get('matchesRequired')}"
        ),
        "lineupEvidence": lineup_evidence,
    }


def get_next_match_for_team(
    team_name,
    matches,
):
    """
    Findet das nächste noch nicht gestartete
    Spiel des Vereins.
    """

    now = datetime.now(
        timezone.utc
    )

    upcoming = []

    for match in matches:

        match_date = (
            parse_match_datetime(match)
        )

        if (
            match_date is None
            or match_date <= now
        ):
            continue

        team1 = match.get(
            "team1",
            {},
        )

        team2 = match.get(
            "team2",
            {},
        )

        name1 = team1.get(
            "teamName",
            "",
        )

        name2 = team2.get(
            "teamName",
            "",
        )

        if not (
            names_match(
                team_name,
                name1,
            )
            or names_match(
                team_name,
                name2,
            )
        ):
            continue

        if names_match(
            team_name,
            name1,
        ):
            opponent = name2
            home_away = "Heim"
        else:
            opponent = name1
            home_away = "Auswärts"

        upcoming.append(
            (
                match_date,
                opponent,
                home_away,
                match,
            )
        )

    if not upcoming:
        return None

    upcoming.sort(
        key=lambda item: item[0]
    )

    match_date, opponent, home_away, match = (
        upcoming[0]
    )

    return {
        "opponent": opponent,
        "homeAway": home_away,
        "dateTime": match_date.isoformat(),
        "match": match,
    }


# ============================================================
# ÖFFENTLICHE PLAYER-INTELLIGENCE V10
# ============================================================

# Bewusst ohne kostenpflichtige Fußball-API-Abhängigkeit.
#
# Die Architektur verwendet:
# - Bundesliga.com: aktuelle Bundesliga-Kader und Spieler-Stammdaten
# - OpenLigaDB: Spielplan, nächster Gegner, Heim/Auswärts
# - öffentliche Bundesliga-Statistikseite als dokumentierte Zusatzquelle
#
# Echte Kickbase-Ø-Punkte werden nicht erfunden. Ebenso werden
# Verletzungen, Sperren, Startelf und Form nicht aus einem anderen
# Wert abgeleitet, wenn keine belastbare öffentliche Information
# vorliegt. Stattdessen bleibt das Feld sauber auf
# "Noch nicht recherchiert".

BUNDESLIGA_STATS_URL = (
    "https://www.bundesliga.com/de/bundesliga/"
    "statistiken/spieler/2026-2027"
)


def get_public_player_intelligence_source():
    """
    Prüft die offizielle Bundesliga-Statistikseite als öffentliche
    Zusatzquelle. Die Seite wird nicht als Kickbase-Datenquelle
    missbraucht und es werden keine Werte erfunden.
    """
    try:
        html = http_get_text(BUNDESLIGA_STATS_URL)
        if html and len(html) > 500:
            print(
                "Bundesliga.com Statistikseite: erreichbar."
            )
            return {
                "available": True,
                "url": BUNDESLIGA_STATS_URL,
            }
    except Exception as exc:
        print(
            "Bundesliga.com Statistikseite nicht erreichbar: "
            f"{exc}"
        )

    return {
        "available": False,
        "url": BUNDESLIGA_STATS_URL,
    }


def build_public_recommendation(
    next_match,
    old_player=None,
):
    """
    Konservative Empfehlung ohne erfundene Spielerwerte.

    Bereits vorhandene belastbare Empfehlung aus der alten JSON
    wird erhalten. Andernfalls wird nur die sichere Spieltags-
    Information verwendet.
    """
    old_player = old_player or {}

    old_recommendation = old_player.get(
        "recommendation"
    )

    if old_recommendation and old_recommendation != "Noch nicht recherchiert":
        return old_recommendation

    if next_match:
        return "Nächstes Spiel vorhanden"

    return "Noch nicht ausreichend Daten"


# ============================================================
# ALTE JSON LADEN
# ============================================================

def load_old_data():
    """
    Liest die alte JSON-Datei.

    Die komplette alte JSON wird geladen, damit bereits
    vorhandene Intelligence-Werte nicht unnötig verloren gehen.
    """

    if not INTELLIGENCE_FILE.exists():
        return {}

    try:

        with INTELLIGENCE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(file)

        return data

    except Exception as exc:

        print(
            "Alte player-intelligence.json "
            "konnte nicht geladen werden: "
            f"{exc}"
        )

        return {}


# ============================================================
# PLAYER-INTELLIGENCE: ÖFFENTLICHE DATEN
# ============================================================

# Keine kostenpflichtige Fußball-API-Abhängigkeit.
# Die Spieler-Stammdaten kommen von Bundesliga.com, der Spielplan
# von OpenLigaDB. Öffentliche Bundesliga-Statistiken werden nur
# als zusätzliche Quelle geprüft; fehlende Werte bleiben leer bzw.
# "Noch nicht recherchiert" und werden nicht erfunden.

# ============================================================
# INTELLIGENCE
# ============================================================

def build_intelligence(
    squad,
    next_match,
):
    """
    Baut weiterhin die Team-Intelligence.

    Die eigentlichen Spieler werden separat unter
    data["players"] gespeichert. Dadurch kann das Frontend
    später direkt auf einen einzelnen Kickbase-Spieler zugreifen.
    """

    if next_match:
        opponent = next_match.get(
            "opponent"
        )

        home_away = next_match.get(
            "homeAway"
        )

        recommendation = (
            "Nächstes Spiel vorhanden"
        )

    else:
        opponent = None
        home_away = None
        recommendation = (
            "Kein nächstes Spiel gefunden"
        )

    return {
        "average": None,
        "starting": "Noch nicht recherchiert",
        "form": "Noch nicht recherchiert",
        "opponent": opponent,
        "homeAway": home_away,
        "injury": (
            "Noch nicht recherchiert"
        ),
        "suspension": (
            "Noch nicht recherchiert"
        ),
        "recommendation": recommendation,
        "players": squad,
    }



def parse_number_after_label(text, label):
    """
    Liest einfache Bundesliga.com-Spielerprofil-Werte aus dem
    sichtbaren Text. Beispiel: 'Einsätze 2'.
    """
    pattern = rf"{re.escape(label)}\s+(\d+(?:[.,]\d+)?)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None

    value = match.group(1).replace(",", ".")
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def parse_number_after_labels(text, labels):
    """Probiert mehrere Sprachvarianten derselben Statistik."""
    for label in labels:
        value = parse_number_after_label(text, label)
        if value is not None:
            return value
    return None



def parse_float_after_labels(text, labels):
    """Liest einen numerischen Wert hinter einem von mehreren Labels."""
    for label in labels:
        pattern = rf"{re.escape(label)}\s+(\d+(?:[.,]\d+)?)"
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            raw = match.group(1).replace(",", ".")
            try:
                value = float(raw)
                return int(value) if value.is_integer() else value
            except ValueError:
                pass
    return None



def _bundesliga_stat_url(slug, season=None, competition="bundesliga"):
    season = season or BUNDESLIGA_STATS_SEASON
    competition = competition if competition in ("bundesliga", "2bundesliga") else "bundesliga"
    return (
        f"https://www.bundesliga.com/de/{competition}/statistiken/"
        f"spieler/{slug}/{season}"
    )


def _get_bundesliga_stat_text(metric_key, season=None, historical=False, competition="bundesliga"):
    """
    Loads one official Bundesliga ranking page and caches it.

    Current and historical seasons use separate caches.
    """
    season = season or BUNDESLIGA_STATS_SEASON
    cache = (
        _BUNDESLIGA_HISTORICAL_STATS_TEXT_CACHE
        if historical
        else _BUNDESLIGA_STATS_TEXT_CACHE
    )
    cache_key = (competition, season, metric_key)

    if cache_key in cache:
        return cache[cache_key]

    config_map = (
        BUNDESLIGA_PRIOR_STAT_CATEGORIES
        if historical
        else BUNDESLIGA_PLAYER_STAT_CATEGORIES
    )
    config = config_map.get(metric_key)
    if not config:
        cache[cache_key] = (None, None)
        return None, None

    url = _bundesliga_stat_url(config["slug"], season=season, competition=competition)

    try:
        html = http_get_text(url, timeout=8)
        page_text = _html_to_visible_text(html)
        cache[cache_key] = (page_text, url)
        prefix = "PRIOR-CACHE" if historical else "STATS-CACHE"
        print(
            f"{prefix} {metric_key}: geladen "
            f"({len(page_text)} Zeichen) | {url}"
        )
        return page_text, url
    except Exception as exc:
        prefix = "PRIOR-CACHE FEHLER" if historical else "STATS-CACHE FEHLER"
        print(f"{prefix} {metric_key}: {exc}")
        cache[cache_key] = (None, url)
        return None, url



# ============================================================
# V56 CURRENT-SEASON GOALS RESOLVER
# ============================================================
# UI/current-performance goals are read ONLY from the current-season
# Bundesliga goals ranking. Historical pages remain projection priors only.

def _v56_current_season_labels():
    """Return official season spellings currently used by Bundesliga.com."""
    labels = {str(CURRENT_SEASON or "").strip()}
    raw = str(BUNDESLIGA_STATS_SEASON or "").strip()
    if raw:
        labels.add(raw)
        parts = re.split(r"[-/]", raw)
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            labels.add(f"{parts[0]}/{parts[1]}")
            if len(parts[1]) == 4:
                labels.add(f"{parts[0]}/{parts[1][-2:]}")
    return tuple(label for label in labels if label)


def _v56_profile_current_season_block(player):
    """
    Load and isolate the explicit current-season statistics block from the
    official Bundesliga player profile.

    Bundesliga.com currently renders season headings e.g. ``2026/2027`` while
    the pipeline historically also used ``2026/27`` and ``2026-2027``.  V56
    accepts all equivalent spellings but still requires an explicit
    ``Statistik Saison`` heading, so historical/news text cannot leak in.
    """
    source_url = (player or {}).get("sourceUrl")
    if not source_url:
        return None, None, "profile_url_missing"

    try:
        html = http_get_text(source_url, timeout=12)
        text = _html_to_visible_text(html)
    except Exception as exc:
        return None, source_url, f"profile_unavailable:{type(exc).__name__}"

    season_pattern = "(?:" + "|".join(
        re.escape(label) for label in _v56_current_season_labels()
    ) + ")"
    # V57: _html_to_visible_text() may preserve link/navigation text between
    # "Statistik Saison" and the season value. Match the heading defensively
    # instead of requiring the season to follow immediately. The bounded gap
    # still requires an explicit current-season label and cannot match historical
    # prose elsewhere on the profile.
    matches = list(re.finditer(
        rf"Statistik\s+Saison(?:\s|[^0-9]){{0,120}}{season_pattern}",
        text or "",
        flags=re.IGNORECASE,
    ))
    if not matches:
        return None, source_url, "explicit_current_season_profile_block_missing"

    # Prefer the last occurrence. Bundesliga.com may render a navigation/tab
    # label first and the actual detailed statistics heading later.
    start = matches[-1].end()
    tail = (text or "")[start:]
    end_match = re.search(
        r"\b(?:News|Videos|Mitspieler|Kompletter\s+Kader|Empfohlener\s+redaktioneller\s+Inhalt)\b",
        tail,
        flags=re.IGNORECASE,
    )
    block = tail[:end_match.start()] if end_match else tail[:5000]
    return block.strip(), source_url, "explicit_current_season_profile_block"


def _v56_metric_from_profile_block(block, labels):
    """Parse an explicit non-negative integer metric from a V56 profile block."""
    for label in labels:
        match = re.search(
            rf"(?<![\w-]){re.escape(label)}(?![\w-])\s*[:\-]?\s*(-?\d+(?:[.,]\d+)?)\b",
            block or "",
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        try:
            number = float(match.group(1).replace(",", "."))
            rounded = round(number)
            if number >= 0 and abs(number - rounded) < 1e-9:
                return int(rounded)
        except (TypeError, ValueError):
            pass
    return None


def _v55_profile_current_season_metric(player, labels):
    """V56-compatible wrapper retained under the V55 function name."""
    block, source_url, block_evidence = _v56_profile_current_season_block(player)
    if not block:
        return {
            "value": None,
            "status": "unknown",
            "source": source_url,
            "evidence": block_evidence,
        }

    value = _v56_metric_from_profile_block(block, labels)
    if value is not None:
        return {
            "value": value,
            "status": "observed",
            "source": source_url,
            "evidence": "explicit_current_season_official_player_profile_v56",
        }

    # A player with an explicitly observed 0 current-season appearances cannot
    # have scored a current-season league goal. This is a deterministic fact,
    # not an estimate and not an inference from ranking absence.
    normalized_labels = {normalize_name(label) for label in labels}
    if "tore" in normalized_labels or "goals" in normalized_labels:
        appearances = _v56_metric_from_profile_block(block, ("Einsätze", "Einsaetze"))
        if appearances == 0:
            return {
                "value": 0,
                "status": "observed",
                "source": source_url,
                "evidence": "explicit_current_season_profile_appearances_zero_implies_goals_zero_v56",
            }

    return {
        "value": None,
        "status": "unknown",
        "source": source_url,
        "evidence": "metric_missing_in_current_season_profile_block_v56",
    }

def v55_current_season_goals(player, club_name, matches):
    """
    V56 resolver order:
      1) explicit 2026/27 official player-profile statistics block;
      2) explicit current-season Bundesliga goals ranking;
      3) unknown.

    Every candidate still passes the V52 match-count sanity guard.
    Absence from a ranking is never interpreted as zero.
    """
    profile_fact = _v55_profile_current_season_metric(player, ("Tore",))
    if profile_fact.get("status") == "observed":
        safe_value, safe_source, guard = v52_guard_current_season_count(
            "goals", profile_fact.get("value"), profile_fact.get("source"),
            club_name, matches,
        )
        if safe_value is not None:
            profile_fact["value"] = safe_value
            profile_fact["source"] = safe_source
            profile_fact["evidence"] += f";{guard}"
            return profile_fact
        profile_fact["status"] = "unknown"
        profile_fact["value"] = None
        profile_fact["source"] = None
        profile_fact["evidence"] += f";guard_rejected:{guard}"

    ranking_fact = v54_current_season_goals((player or {}).get("name"))
    if ranking_fact.get("status") == "observed":
        safe_value, safe_source, guard = v52_guard_current_season_count(
            "goals", ranking_fact.get("value"), ranking_fact.get("source"),
            club_name, matches,
        )
        if safe_value is not None:
            ranking_fact["value"] = safe_value
            ranking_fact["source"] = safe_source
            ranking_fact["evidence"] += f";{guard}"
            return ranking_fact
        ranking_fact["status"] = "unknown"
        ranking_fact["value"] = None
        ranking_fact["source"] = None
        ranking_fact["evidence"] += f";guard_rejected:{guard}"

    return {
        "value": None,
        "status": "unknown",
        "source": profile_fact.get("source") or ranking_fact.get("source"),
        "evidence": (
            f"profile={profile_fact.get('evidence')};"
            f"ranking={ranking_fact.get('evidence')}"
        ),
    }


def v54_current_season_goals(player_name):
    page_text, source_url = _get_bundesliga_stat_text(
        "goals",
        season=BUNDESLIGA_STATS_SEASON,
        historical=False,
        competition="bundesliga",
    )

    if not page_text or not source_url:
        return {
            "value": None,
            "status": "unknown",
            "source": None,
            "evidence": "current_goals_page_unavailable",
        }

    heading = (BUNDESLIGA_PLAYER_STAT_CATEGORIES.get("goals") or {}).get("heading") or "Tore"
    value = _extract_metric_from_ranking_text(page_text, heading, player_name)

    if value is None:
        # Never infer zero merely because the player is absent from a ranking.
        return {
            "value": None,
            "status": "unknown",
            "source": source_url,
            "evidence": "player_not_explicitly_listed_on_current_goals_ranking",
        }

    try:
        number = float(value)
        rounded = round(number)
        if number < 0 or abs(number - rounded) > 1e-9:
            raise ValueError
    except (TypeError, ValueError):
        return {
            "value": None,
            "status": "unknown",
            "source": source_url,
            "evidence": f"non_integer_current_goals_value:{value}",
        }

    return {
        "value": int(rounded),
        "status": "observed",
        "source": source_url,
        "evidence": "explicit_current_season_bundesliga_goals_ranking",
    }


def _normalize_player_lookup_name(value):
    value = normalize_name(value or "")
    return re.sub(r"\s+", " ", value).strip()


def _v54_primary_ranking_segment(page_text, heading):
    """
    Isolate ONLY the requested ranking block.

    Bundesliga statistic pages contain many teaser rankings below the primary
    ranking. Older parsing searched from the requested heading to EOF, so a
    player absent from the primary "Tore" block could accidentally match later
    in "Torschüsse", "Sprints", etc.
    """
    if not page_text or not heading:
        return None

    heading_match = re.search(
        re.escape(str(heading)),
        page_text,
        flags=re.IGNORECASE,
    )
    if not heading_match:
        return None

    tail = page_text[heading_match.start():]

    cut_positions = []
    for pattern in (
        r"\bWeniger anzeigen\b",
        r"\bMehr laden\b",
        r"\bVollständige Liste anzeigen\b",
        r"\bVollstaendige Liste anzeigen\b",
    ):
        marker = re.search(pattern, tail, flags=re.IGNORECASE)
        if marker and marker.start() > len(str(heading)):
            cut_positions.append(marker.start())

    for config in BUNDESLIGA_PLAYER_STAT_CATEGORIES.values():
        other = str((config or {}).get("heading") or "").strip()
        if not other or normalize_name(other) == normalize_name(heading):
            continue
        marker = re.search(
            re.escape(other),
            tail[len(str(heading)):],
            flags=re.IGNORECASE,
        )
        if marker:
            absolute = len(str(heading)) + marker.start()
            if absolute > len(str(heading)):
                cut_positions.append(absolute)

    end = min(cut_positions) if cut_positions else len(tail)
    return tail[:end]


def _extract_metric_from_ranking_text(page_text, heading, player_name):
    """
    V54 strict ranking parser.

    A value is accepted only if the player occurs inside the PRIMARY block of
    the requested metric and the numeric value follows that exact full name.
    No cross-category search and no surname-only fallback.
    """
    if not page_text or not player_name:
        return None

    segment = _v54_primary_ranking_segment(page_text, heading)
    if not segment:
        return None

    parts = [re.escape(p) for p in str(player_name).strip().split() if p]
    if not parts:
        return None

    name_pattern = r"\s+".join(parts)

    for match in re.finditer(
        rf"(?<![\w-]){name_pattern}(?![\w-])",
        segment,
        flags=re.IGNORECASE,
    ):
        tail = segment[match.end():match.end() + 40]

        value_match = re.match(
            r"\s+(-?\d+(?:[.,]\d+)?)\b",
            tail,
        )
        if not value_match:
            continue

        raw = value_match.group(1).replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            continue

        return int(value) if value.is_integer() else value

    return None


def collect_bundesliga_rankings_for_player(player):
    """
    V23 current-season collector.

    IMPORTANT:
    A player missing from the visible Bundesliga ranking is UNKNOWN, not 0.
    Ranking pages can show only the leading entries. Earlier versions treated
    "not listed" as zero; V23 removes that unsafe assumption.
    """
    player_name = str(player.get("name") or "").strip()

    values = {}
    sources = {}
    pages_available = 0
    explicit_hits = 0

    for metric_key, config in BUNDESLIGA_PLAYER_STAT_CATEGORIES.items():
        page_text, url = _get_bundesliga_stat_text(metric_key)

        if not page_text:
            values[metric_key] = None
            continue

        pages_available += 1
        value = _extract_metric_from_ranking_text(
            page_text,
            config["heading"],
            player_name,
        )

        values[metric_key] = value
        if value is not None:
            explicit_hits += 1
            sources[metric_key] = url

    values["redCards"] = None

    print(
        f"STATS-PLAYER {player_name}: "
        f"{explicit_hits} explizite Rankingwerte | "
        f"{pages_available} Rankingseiten verfügbar | "
        "nicht gelistet = unbekannt"
    )

    return values, sources



def _extract_match_links_from_schedule(html, competition, season):
    """Extract unique match URLs from an official Bundesliga club schedule."""
    if not html:
        return []

    # HTML can contain escaped or absolute links.
    html = html.replace("\\/", "/")
    pattern = re.compile(
        rf'(?:https://www\.bundesliga\.com)?'
        rf'(/de/{re.escape(competition)}/spieltag/{re.escape(season)}/\d+/'
        rf'[^"\'<>?#]+)'
    )

    links = []
    seen = set()
    for match in pattern.finditer(html):
        path = match.group(1).rstrip("/")
        # Strip tab suffix if schedule already links to one.
        path = re.sub(r"/(?:lineup|stats|liveticker|table|news)$", "", path)
        if path not in seen:
            seen.add(path)
            links.append("https://www.bundesliga.com" + path)
    return links



def _v49_current_official_gk_start_evidence(player, club_name, completed_match_count):
    """
    Current-season official Bundesliga lineup evidence.

    Strict rule:
    - the player's name must be present on the official /lineup page
    - AND the page must contain a starting-XI marker
    - AND this must be proven for every completed league match so far.

    If anything is missing, return unknown. We never infer a start from an
    appearance count alone.
    """
    name = str(player.get("name") or "").strip()
    if not name or completed_match_count <= 0:
        return {
            "status": "unknown",
            "startsProven": 0,
            "matchesRequired": completed_match_count,
            "evidence": "no_name_or_no_completed_matches",
            "sourceUrls": [],
        }

    competition = "bundesliga"
    slug = (
        BUNDESLIGA_CLUB_SCHEDULE_SLUGS.get(club_name)
        or BUNDESLIGA_CLUB_NEWS_SLUGS.get(club_name)
    )
    if not slug:
        return {
            "status": "unknown",
            "startsProven": 0,
            "matchesRequired": completed_match_count,
            "evidence": "club_slug_missing",
            "sourceUrls": [],
        }

    cache_key = (club_name, CURRENT_SEASON, "v49-current-lineup")
    club_data = V49_CURRENT_LINEUP_CACHE.get(cache_key)
    if club_data is None:
        schedule_url = (
            f"https://www.bundesliga.com/de/{competition}/spieltag/"
            f"{BUNDESLIGA_STATS_SEASON}/{slug}"
        )
        try:
            schedule_html = http_get_text(schedule_url, timeout=12)
            links = _extract_match_links_from_schedule(
                schedule_html,
                competition,
                BUNDESLIGA_STATS_SEASON,
            )
        except Exception as exc:
            club_data = {
                "available": False,
                "reason": f"schedule_fetch_failed:{type(exc).__name__}",
                "scheduleUrl": schedule_url,
                "lineups": {},
            }
            V49_CURRENT_LINEUP_CACHE[cache_key] = club_data
            links = []

        if links:
            lineups = {}
            # Early season: fetch only enough pages to cover completed matches,
            # plus a tiny buffer in case future fixtures appear first.
            for match_url in links[: max(completed_match_count + 2, 3)]:
                lineup_url = match_url.rstrip("/") + "/lineup"
                try:
                    html = http_get_text(lineup_url, timeout=10)
                except Exception:
                    continue

                plain = re.sub(
                    r"<script\b[^>]*>.*?</script>",
                    " ",
                    html,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                plain = re.sub(
                    r"<style\b[^>]*>.*?</style>",
                    " ",
                    plain,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                plain = re.sub(r"<[^>]+>", " ", plain)
                plain = unescape(re.sub(r"\s+", " ", plain)).strip()
                lineups[lineup_url] = plain

            club_data = {
                "available": bool(lineups),
                "scheduleUrl": schedule_url,
                "lineups": lineups,
            }
            V49_CURRENT_LINEUP_CACHE[cache_key] = club_data

    if not club_data.get("available"):
        return {
            "status": "unknown",
            "startsProven": 0,
            "matchesRequired": completed_match_count,
            "evidence": club_data.get("reason") or "no_lineup_pages",
            "sourceUrls": [],
        }

    name_norm = _normalize_player_lookup_name(name)
    club_norm = _normalize_player_lookup_name(club_name)
    proven_urls = []

    for lineup_url, plain in (club_data.get("lineups") or {}).items():
        normalized = _normalize_player_lookup_name(plain)
        if not name_norm or name_norm not in normalized:
            continue

        # Presence alone can include a bench. Require a start-XI marker.
        # Bundesliga pages use variants such as "Startelf", "Startaufstellung"
        # or "Starting XI". The club name/slug must also be present.
        has_start_marker = any(
            marker in normalized
            for marker in (
                "startelf",
                "startaufstellung",
                "starting xi",
                "starting lineup",
            )
        )
        has_club_context = club_norm in normalized or normalize_name(slug) in normalized

        if has_start_marker and has_club_context:
            proven_urls.append(lineup_url)

    starts_proven = len(proven_urls)
    status = (
        "complete"
        if starts_proven >= completed_match_count
        else "partial"
        if starts_proven > 0
        else "unknown"
    )
    return {
        "status": status,
        "startsProven": starts_proven,
        "matchesRequired": completed_match_count,
        "evidence": (
            f"official_current_lineups:{starts_proven}/{completed_match_count}"
        ),
        "sourceUrls": proven_urls,
        "scheduleUrl": club_data.get("scheduleUrl"),
    }


def _historical_match_evidence_for_club(club_name, competition):
    """
    V26 fallback: use official 2025/26 club schedule + match lineup pages.

    Cached per club/competition so all players from the same club reuse the
    same downloaded season evidence.
    """
    cache_key = (club_name, competition, BUNDESLIGA_PRIOR_SEASON)
    if cache_key in HISTORICAL_MATCH_EVIDENCE_CACHE:
        return HISTORICAL_MATCH_EVIDENCE_CACHE[cache_key]

    slug = BUNDESLIGA_CLUB_SCHEDULE_SLUGS.get(club_name)
    if not slug:
        result = {"available": False, "reason": "club_slug_missing", "players": {}}
        HISTORICAL_MATCH_EVIDENCE_CACHE[cache_key] = result
        return result

    schedule_url = (
        f"https://www.bundesliga.com/de/{competition}/spieltag/"
        f"{BUNDESLIGA_PRIOR_SEASON}/{slug}"
    )
    try:
        schedule_html = http_get_text(schedule_url, timeout=15)
    except Exception as exc:
        result = {
            "available": False,
            "reason": f"schedule_fetch_failed:{type(exc).__name__}",
            "scheduleUrl": schedule_url,
            "players": {},
        }
        HISTORICAL_MATCH_EVIDENCE_CACHE[cache_key] = result
        return result

    match_links = _extract_match_links_from_schedule(
        schedule_html, competition, BUNDESLIGA_PRIOR_SEASON
    )

    # Safety cap: a league season has 34 matches. Allow a few duplicates/odd links.
    match_links = match_links[:40]
    player_rows = {}
    loaded = 0

    for match_url in match_links:
        lineup_url = match_url + "/lineup"
        try:
            lineup_html = http_get_text(lineup_url, timeout=12)
        except Exception:
            continue
        loaded += 1

        # Server-rendered lineup text contains player names. We deliberately do
        # not infer minutes or performance stats from absence/presence.
        plain = re.sub(r"<[^>]+>", " ", lineup_html)
        plain = re.sub(r"\s+", " ", plain)

        # Keep raw normalized text once per match; player-specific matching is
        # done later to avoid inventing a roster parser.
        player_rows[match_url] = _normalize_player_lookup_name(plain)

    result = {
        "available": bool(loaded),
        "scheduleUrl": schedule_url,
        "matchesDiscovered": len(match_links),
        "lineupsLoaded": loaded,
        "matchTexts": player_rows,
    }
    HISTORICAL_MATCH_EVIDENCE_CACHE[cache_key] = result
    return result


def collect_historical_match_evidence(player, competition):
    """
    Player-specific historical evidence. This is intentionally a role/usage
    fallback, not a replacement for missing event metrics.
    """
    name = str(player.get("name") or "").strip()
    club = str(player.get("club") or player.get("team") or "").strip()
    club_data = _historical_match_evidence_for_club(club, competition)

    if not club_data.get("available"):
        print(
            f"PRIOR-MATCH-EVIDENCE {name}: nicht verfügbar | "
            f"Grund={club_data.get('reason', 'unknown')}"
        )
        return {
            "available": False,
            "matchesFound": 0,
            "lineupsLoaded": club_data.get("lineupsLoaded", 0),
            "matchesDiscovered": club_data.get("matchesDiscovered", 0),
            "sourceUrl": club_data.get("scheduleUrl"),
        }

    needle = _normalize_player_lookup_name(name)
    found_urls = [
        url for url, normalized_text in club_data.get("matchTexts", {}).items()
        if needle and needle in normalized_text
    ]

    print(
        f"PRIOR-MATCH-EVIDENCE {name}: "
        f"{len(found_urls)} Match-Lineups gefunden | "
        f"{club_data.get('lineupsLoaded', 0)} Lineups geladen | "
        f"Liga={competition}"
    )

    return {
        "available": True,
        "matchesFound": len(found_urls),
        "lineupsLoaded": club_data.get("lineupsLoaded", 0),
        "matchesDiscovered": club_data.get("matchesDiscovered", 0),
        "sourceUrl": club_data.get("scheduleUrl"),
        "sampleMatchUrls": found_urls[:5],
        "note": (
            "Offizielle historische Match-/Aufstellungs-Evidenz. "
            "Wird nicht als fehlende Eventstatistik ausgegeben."
        ),
    }


def _collect_historical_prior_from_competition(player, competition):
    """
    Collects the 2025/26 prior from one competition.
    Returns explicit ranking hits only; not listed remains unknown.
    """
    player_name = str(player.get("name") or "").strip()
    values = {}
    sources = {}
    pages_available = 0
    explicit_hits = 0

    for metric_key, config in BUNDESLIGA_PRIOR_STAT_CATEGORIES.items():
        page_text, url = _get_bundesliga_stat_text(
            metric_key,
            season=BUNDESLIGA_PRIOR_SEASON,
            historical=True,
            competition=competition,
        )

        if not page_text:
            values[metric_key] = None
            continue

        pages_available += 1
        value = _extract_metric_from_ranking_text(
            page_text,
            config["heading"],
            player_name,
        )
        values[metric_key] = value

        if value is None and _normalize_player_lookup_name(player_name) in _normalize_player_lookup_name(page_text):
            print(
                f"PRIOR-MATCH {player_name}: Name auf {competition}/{metric_key} "
                "gefunden, aber kein direkt parsebarer Wert"
            )

        if value is not None:
            explicit_hits += 1
            sources[metric_key] = url

    return values, sources, pages_available, explicit_hits


def collect_bundesliga_historical_prior(player):
    """
    V24 historical league fallback.

    First checks Bundesliga 2025/26. If the player has no explicit ranking
    hit there, it automatically checks 2. Bundesliga 2025/26.

    The competition with more explicit hits wins. Historical values remain
    fully separate from current-season totals.
    """
    player_name = str(player.get("name") or "").strip()

    bl_values, bl_sources, bl_pages, bl_hits = (
        _collect_historical_prior_from_competition(player, "bundesliga")
    )

    candidates = [
        ("Bundesliga", "bundesliga", bl_values, bl_sources, bl_pages, bl_hits)
    ]

    # Only pay for the second-league requests when Bundesliga gives us no
    # historical evidence. The cache means subsequent promoted players are cheap.
    if bl_hits == 0:
        zbl_values, zbl_sources, zbl_pages, zbl_hits = (
            _collect_historical_prior_from_competition(player, "2bundesliga")
        )
        candidates.append(
            ("2. Bundesliga", "2bundesliga",
             zbl_values, zbl_sources, zbl_pages, zbl_hits)
        )

    best = max(candidates, key=lambda item: item[5])

    # V25 tie handling: max() previously silently selected Bundesliga on 0:0.
    # If we actually had to check 2. Bundesliga and both have zero explicit hits,
    # retain 2. Bundesliga as the contextual prior league. Values stay None.
    if len(candidates) > 1 and all(item[5] == 0 for item in candidates):
        best = candidates[-1]

    league_label, competition, values, sources, pages_available, explicit_hits = best

    match_evidence = None
    if explicit_hits == 0:
        match_evidence = collect_historical_match_evidence(player, competition)

    match_evidence_signal = _v27_match_evidence_adjustment(
        {"matchEvidence": match_evidence}
    )

    available = [key for key, value in values.items() if value is not None]
    missing = [key for key, value in values.items() if value is None]
    coverage = round(
        len(available) / max(len(BUNDESLIGA_PRIOR_STAT_CATEGORIES), 1) * 100
    )

    tried = ", ".join(
        f"{label}:{hits}"
        for label, _comp, _vals, _srcs, _pages, hits in candidates
    )

    print(
        f"PRIOR-LEAGUE {player_name}: gewählt={league_label} | "
        f"Treffer={explicit_hits} | geprüft={tried}"
    )
    print(
        f"PRIOR-PLAYER {player_name}: Coverage {coverage}% | "
        f"vorhanden={len(available)} | fehlend={len(missing)} | "
        f"Liga={league_label}"
    )

    return values, sources, {
        "season": BUNDESLIGA_PRIOR_SEASON,
        "competition": competition,
        "league": league_label,
        "availableMetrics": available,
        "missingMetrics": missing,
        "coveragePercent": coverage,
        "explicitRankingHits": explicit_hits,
        "rankingPagesAvailable": pages_available,
        "competitionsTried": [
            {"league": label, "competition": comp, "explicitRankingHits": hits}
            for label, comp, _vals, _srcs, _pages, hits in candidates
        ],
        "matchEvidence": match_evidence,
        "matchEvidenceSignal": match_evidence_signal,
        "note": (
            "Historischer Prior; automatisch zwischen Bundesliga und "
            "2. Bundesliga gewählt; nicht mit aktuellen Saisonwerten vermischt."
        ),
    }


def _v31_html_cell(row_html, data_stat):
    match = re.search(
        rf'data-stat=["\']{re.escape(data_stat)}["\'][^>]*>(.*?)</(?:td|th)>',
        row_html,
        flags=re.I | re.S,
    )
    if not match:
        return None
    raw = re.sub(r"<[^>]+>", " ", match.group(1))
    raw = unescape(raw).replace(",", "").strip()
    number = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not number:
        return None
    try:
        value = float(number.group(0))
        return int(value) if value.is_integer() else value
    except ValueError:
        return None


def _v32_official_goalkeeper_current_profile(player):
    if _v29_position_group(player.get("position")) != "TW": return {}, {}, None
    name=str(player.get("name") or "").strip()
    if not name: return {}, {}, None
    slug=normalize_name(name).replace(" ", "-")
    for url in (f"https://www.bundesliga.com/de/spieler/{slug}", f"https://www.bundesliga.com/en/player/{slug}"):
        try:
            page=_html_to_visible_text(http_get_text(url, timeout=10))
        except Exception as exc:
            print(f"V32-GK-OFFICIAL {name}: Profil nicht verfügbar | {type(exc).__name__}"); continue
        if _normalize_player_lookup_name(name) not in _normalize_player_lookup_name(page): continue
        def grab(labels):
            for label in labels:
                m=re.search(rf"{re.escape(label)}\s+(\d+(?:[.,]\d+)?)", page, flags=re.I)
                if m:
                    v=float(m.group(1).replace(",", ".")); return int(v) if v.is_integer() else v
            return None
        vals={"saves":grab(("abgewehrte Schüsse","Gehaltene Torschüsse","Shots saved")),"appearances":grab(("Einsätze","Appearances")),"yellowCards":grab(("Gelbe Karten","Yellow cards"))}
        vals={k:v for k,v in vals.items() if v is not None}
        if vals:
            print(f"V32-GK-OFFICIAL {name}: CurrentProfile={vals} | {url}")
            return vals,{k:url for k in vals},{"provider":"Bundesliga.com","scope":"current-season-profile","sourceUrl":url,"metrics":sorted(vals)}
    return {}, {}, None


def _v35_season_label(start_year):
    return f"{int(start_year)}-{int(start_year)+1}"

def _v35_current_and_prior_seasons():
    # The workflow is for the live 2026/27 season. Keeping this explicit makes
    # reruns deterministic; future versions can derive it from competition metadata.
    return "2026-2027", "2025-2026"

def _v35_current_sample_weight(appearances):
    """
    Smoothly shifts authority from historical prior to the live season.
    0 apps=0%, 1=10%, 3=25%, 5=45%, 8=70%, 10=82%, 12+=90%.
    We deliberately retain a small prior contribution to reduce early-season noise.
    """
    try:
        n = max(0, int(appearances or 0))
    except (TypeError, ValueError):
        n = 0
    knots = [(0,0.0),(1,0.10),(3,0.25),(5,0.45),(8,0.70),(10,0.82),(12,0.90)]
    if n >= knots[-1][0]:
        return knots[-1][1]
    for (x0,y0),(x1,y1) in zip(knots, knots[1:]):
        if x0 <= n <= x1:
            return y0 + (y1-y0)*(n-x0)/float(x1-x0)
    return 0.0

def _v39_blend_rate(current_total, current_apps, prior_total, prior_apps, neutral_rate=None):
    """
    Blend per-appearance rates with early-season shrinkage.

    Priority:
      current sample + player historical prior, when available;
      otherwise current sample + neutral positional baseline.

    Missing observations never become zero.
    """
    def rate(total, apps):
        try:
            a = int(apps or 0)
            if total is None or a <= 0:
                return None
            return float(total) / float(a)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    cr = rate(current_total, current_apps)
    pr = rate(prior_total, prior_apps)

    if cr is None:
        return pr, 0.0, "historical_prior" if pr is not None else None

    w = _v35_current_sample_weight(current_apps)

    if pr is not None:
        return cr*w + pr*(1.0-w), w, "historical_prior"

    if neutral_rate is not None:
        try:
            nr = float(neutral_rate)
            return cr*w + nr*(1.0-w), w, "neutral_gk_baseline"
        except (TypeError, ValueError):
            pass

    # No defensible comparison prior exists: expose current rate, but keep
    # the sample weight diagnostic instead of pretending confidence=100%.
    return cr, w, "current_only"


def _v34_official_goalkeeper_season_prior(player, season="2025-2026"):
    """
    Official Bundesliga/2. Bundesliga season-ranking collector for goalkeeper saves.
    It deliberately distinguishes:
      - explicit player row -> usable value
      - player absent from loaded ranking -> unknown, never zero
    """
    if _v29_position_group(player.get("position")) != "TW":
        return {}, {}, None

    name = str(player.get("name") or "").strip()
    if not name:
        return {}, {}, None

    league_paths = [
        ("2bundesliga", "https://www.bundesliga.com/de/2bundesliga/statistiken/spieler/gehaltene-torschuesse/" + season),
        ("bundesliga", "https://www.bundesliga.com/de/bundesliga/statistiken/spieler/gehaltene-torschuesse/" + season),
    ]

    wanted = _normalize_player_lookup_name(name)
    checked = []
    for league, url in league_paths:
        try:
            html = http_get_text(url, timeout=15)
        except Exception as exc:
            checked.append({"league": league, "url": url, "error": type(exc).__name__})
            continue

        visible = _html_to_visible_text(html)
        normalized = _normalize_player_lookup_name(visible)
        checked.append({"league": league, "url": url, "loaded": True})

        # Accept only an explicit row/name occurrence followed closely by a numeric value.
        # Do not interpret absence from a top-N ranking as zero.
        candidates = [
            rf"{re.escape(name)}\s+(\d+)",
            rf"{re.escape(wanted)}\s+(\d+)",
        ]
        saves = None
        for pattern in candidates:
            m = re.search(pattern, visible, flags=re.I)
            if m:
                try:
                    saves = int(m.group(1))
                except (TypeError, ValueError):
                    saves = None
                if saves is not None:
                    break

        if saves is not None:
            meta = {
                "provider": "Bundesliga.com",
                "season": season,
                "league": league,
                "sourceUrl": url,
                "explicit": True,
                "metrics": ["saves"],
                "checked": checked,
            }
            print(f"V34-GK-SEASON {name}: Liga={league} | Saves={saves} | explizit=JA")
            return {"saves": saves}, {"saves": url}, meta

    print(f"V34-GK-SEASON {name}: kein expliziter Saison-Rankingwert | unknown")
    return {}, {}, {
        "provider": "Bundesliga.com",
        "season": season,
        "explicit": False,
        "metrics": [],
        "checked": checked,
        "note": "Nicht im geladenen Ranking = unbekannt, nicht 0.",
    }



def _v30_goalkeeper_historical_fallback(player, values, sources, meta):
    """Use official historical lineup evidence as GK usage prior, never as fake events."""
    if _v29_position_group(player.get("position")) != "TW":
        return values, sources, meta

    values = dict(values or {})
    sources = dict(sources or {})
    meta = dict(meta or {})
    evidence = meta.get("matchEvidence") or {}
    starts = int(evidence.get("matchesFound") or 0)
    loaded = int(evidence.get("lineupsLoaded") or 0)

    # V35: collect both live season and previous-season prior.
    current_season,prior_season=_v35_current_and_prior_seasons()
    gk_current_values,gk_current_sources,gk_current_meta=_v34_official_goalkeeper_season_prior(
        player,current_season
    )
    gk_prior_values,gk_prior_sources,gk_prior_meta=_v34_official_goalkeeper_season_prior(
        player,prior_season
    )
    if gk_current_meta:
        meta["goalkeeperCurrentSeasonPerformance"]=gk_current_meta
    if gk_prior_meta:
        meta["goalkeeperSeasonPerformancePrior"]=gk_prior_meta

    # Preserve the historical prior in values for downstream compatibility.
    for key,value in gk_prior_values.items():
        if value is not None and values.get(key) is None:
            values[key]=value
            sources[key]=gk_prior_sources.get(key)

    # Keep live values separate: downstream V35 blends rates rather than totals.
    meta["goalkeeperCurrentSeasonValues"]=gk_current_values
    meta["goalkeeperCurrentSeasonSources"]=gk_current_sources

    if starts > 0:
        if values.get("appearances") is None:
            values["appearances"] = starts
            sources["appearances"] = evidence.get("sourceUrl")
        if values.get("starts") is None:
            values["starts"] = starts
            sources["starts"] = evidence.get("sourceUrl")
        if values.get("minutes") is None:
            values["minutes"] = starts * 90
            sources["minutes"] = evidence.get("sourceUrl")
        meta["goalkeeperUsagePrior"] = {
            "appearances": starts,
            "starts": starts,
            "minutes": starts * 90,
            "lineupsLoaded": loaded,
            "sourceUrl": evidence.get("sourceUrl"),
            "method": "official historical lineup evidence; 90-minute GK start scenario",
        }

    meta["goalkeeperEventCoveragePercent"] = meta.get("coveragePercent", 0)
    print(
        f"V30-GK-PRIOR {player.get('name')}: Starts={starts}/{loaded} | "
        f"Minuten={values.get('minutes')} | Saves={values.get('saves')} | "
        f"GA={values.get('goalsAgainst')} | CS={values.get('cleanSheets')} | "
        f"EventCoverage={meta.get('coveragePercent', 0)}%"
    )
    return values, sources, meta


def build_kickbase_factor_coverage(performance, position=None):
    """
    V41 position-aware Kickbase event coverage matrix.

    Categories:
      exact_public     -> explicit public value available
      aggregate_public -> public aggregate exists, but not Kickbase event subtype
      contextual       -> usable context, not a directly scored event
      unavailable      -> not currently observed by this pipeline

    This is a transparency/readiness layer, not a claim that the proprietary
    Kickbase event model has been fully reproduced.
    """
    pos_group = _v29_position_group(position)

    exact_metric_map = {
        "goals": "goals",
        "assists": "assists",
        "shots": "shots",
        "shotsOnTarget": "shotsOnTarget",
        "woodwork": "woodwork",
        "penalties": "penalties",
        "penaltiesScored": "penaltiesScored",
        "passAccuracy": "passAccuracy",
        "duelsWon": "duelsWon",
        "aerialDuelsWon": "aerialDuelsWon",
        "crosses": "crosses",
        "fouls": "fouls",
        "yellowCards": "yellowCards",
        "redCards": "redCards",
        "cleanSheets": "cleanSheets",
        "goalsAgainst": "goalsAgainst",
    }

    aggregate_metric_map = {
        # Aggregate saves are useful predictive evidence, but cannot be mapped
        # exactly to Kickbase keeper subtypes such as caught/parried/long-shot.
        "aggregateSaves": "saves",
        "distanceKm": "distanceKm",
        "sprints": "sprints",
        "intensiveRuns": "intensiveRuns",
        "topSpeedKmh": "topSpeedKmh",
    }

    exact_available = [
        factor for factor, metric in exact_metric_map.items()
        if performance.get(metric) is not None
    ]
    exact_missing = [
        factor for factor, metric in exact_metric_map.items()
        if performance.get(metric) is None
    ]
    aggregate_available = [
        factor for factor, metric in aggregate_metric_map.items()
        if performance.get(metric) is not None
    ]

    contextual = {
        "expectedMinutes": "Startelf-/Einsatzwahrscheinlichkeit",
        "homeAway": "Spielkontext",
        "injurySuspension": "Verfügbarkeit",
    }

    unavailable_common = [
        "successfulPassesCount",
        "misplacedPassesCount",
        "passesOpponentHalf",
        "passesFinalThird",
        "keyPasses",
        "bigChancesCreated",
        "bigChancesMissed",
        "ballRecoveries",
        "interceptions",
        "tacklesWonDetailed",
        "blockedShotsDetailed",
        "errorsLeadingShot",
        "errorsLeadingGoal",
        "lastManActions",
        "dribbledPast",
        "successfulDribblesDetailed",
    ]

    unavailable_by_position = {
        "TW": [
            "keeperCaughtBall",
            "keeperParry",
            "keeperSavedShotBox",
            "keeperSavedLongShot",
            "keeperHighClaimsDetailed",
            "penaltySavesDetailed",
            "goalsPrevented",
        ],
        "ABW": [
            "clearancesDetailed",
            "blockedCrossesDetailed",
        ],
        "MF": [
            "progressivePassesDetailed",
            "chanceCreationDetailed",
        ],
        "ANG": [
            "shotsByLocationDetailed",
            "chanceConversionDetailed",
        ],
        "ALL": [],
    }

    # Position-specific critical factors for model readiness.
    critical = {
        "TW": [
            "aggregateSaves",
            "cleanSheets",
            "goalsAgainst",
            "passAccuracy",
        ],
        "ABW": [
            "duelsWon",
            "aerialDuelsWon",
            "passAccuracy",
            "crosses",
            "cleanSheets",
        ],
        "MF": [
            "goals",
            "assists",
            "shots",
            "passAccuracy",
            "duelsWon",
            "crosses",
        ],
        "ANG": [
            "goals",
            "assists",
            "shots",
            "shotsOnTarget",
            "duelsWon",
        ],
        "ALL": [
            "goals",
            "assists",
            "shots",
            "passAccuracy",
            "duelsWon",
        ],
    }[pos_group]

    all_observed = set(exact_available + aggregate_available)
    critical_available = [key for key in critical if key in all_observed]
    critical_missing = [key for key in critical if key not in all_observed]
    readiness = round(
        len(critical_available) / max(len(critical), 1) * 100
    )

    # Reliability band is intentionally conservative.
    if readiness >= 80:
        band = "hoch"
    elif readiness >= 60:
        band = "mittel"
    elif readiness >= 40:
        band = "niedrig"
    else:
        band = "sehr niedrig"

    return {
        "positionModel": pos_group,
        "exactPublicAvailable": exact_available,
        "exactPublicMissing": exact_missing,
        "aggregatePublicAvailable": aggregate_available,
        "contextual": contextual,
        "currentlyUnavailable": (
            unavailable_common + unavailable_by_position.get(pos_group, [])
        ),
        "criticalFactors": critical,
        "criticalAvailable": critical_available,
        "criticalMissing": critical_missing,
        "scoringReadinessPercent": readiness,
        "reliabilityBand": band,
        "notes": [
            "Aggregate Werte sind Prognose-Evidenz, keine exakten Kickbase-Eventtypen.",
            "Fehlende Events werden nicht als 0 interpretiert.",
            "Readiness misst Datenabdeckung, nicht Spielerqualität.",
        ],
    }


def _v27_match_evidence_adjustment(prior_meta):
    """
    Convert historical lineup evidence into a bounded availability/role signal.
    It must never manufacture missing event statistics or raw Kickbase points.
    """
    evidence = (prior_meta or {}).get("matchEvidence") or {}
    found = int(evidence.get("matchesFound") or 0)
    loaded = int(evidence.get("lineupsLoaded") or 0)

    if loaded <= 0:
        return {
            "ratio": None,
            "sample": 0,
            "roleSignal": "unknown",
            "projectionMultiplier": 1.0,
            "confidenceBoost": 0,
        }

    ratio = max(0.0, min(1.0, found / loaded))

    # Conservative bounded influence: lineup evidence affects expected usage,
    # not event production. Even 34/34 can only move the projection by +12%.
    if loaded >= 8 and ratio >= 0.85:
        signal, mult, boost = "very_strong", 1.12, 10
    elif loaded >= 8 and ratio >= 0.65:
        signal, mult, boost = "strong", 1.08, 7
    elif loaded >= 6 and ratio >= 0.40:
        signal, mult, boost = "moderate", 1.04, 4
    elif loaded >= 6 and ratio <= 0.15:
        signal, mult, boost = "weak", 0.94, 2
    else:
        signal, mult, boost = "limited", 1.0, 1

    return {
        "ratio": round(ratio, 3),
        "sample": loaded,
        "matchesFound": found,
        "roleSignal": signal,
        "projectionMultiplier": mult,
        "confidenceBoost": boost,
    }



def _v29_position_group(position):
    raw = str(position or "").strip().lower()
    normalized = normalize_name(position)

    if (
        "torwart" in raw
        or "torhüter" in raw
        or "torhueter" in raw
        or normalized in {"torhuter", "torhueter", "torwart", "tw", "gk", "goalkeeper"}
    ):
        return "TW"

    if (
        "abwehr" in raw
        or "verteidigung" in raw
        or normalized in {"abwehr", "verteidigung", "ab", "abw", "df"}
    ):
        return "ABW"

    if "mittelfeld" in raw or normalized in {"mittelfeld", "mf", "mid"}:
        return "MF"

    if "angriff" in raw or normalized in {"angriff", "ang", "fw", "st"}:
        return "ANG"

    return "ALL"


def _v29_prior_strength(historical_prior, historical_prior_coverage, pos_group):
    """
    Converts full-season 2025/26 public ranking totals into a bounded strength
    signal. No per-90 rates are invented because historical minutes are not
    consistently available in the current collector.

    Output is a small additive starter-scenario adjustment, not fake events.
    """
    prior = historical_prior or {}
    coverage = int((historical_prior_coverage or {}).get("coveragePercent") or 0)

    if coverage <= 0:
        return {
            "bonus": 0.0,
            "coverage": 0,
            "source": "none",
            "components": {},
        }

    comps = {}

    def cap_ratio(key, denom, weight, cap):
        value = prior.get(key)
        if value is None:
            return
        try:
            contribution = min(float(value) / float(denom) * weight, cap)
            comps[key] = round(contribution, 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # Position-aware season prior. These are bounded strength indicators,
    # not claimed Kickbase event weights.
    if pos_group == "ANG":
        cap_ratio("goals", 20, 20, 22)
        cap_ratio("assists", 10, 10, 10)
        cap_ratio("shots", 80, 14, 16)
        cap_ratio("duelsWon", 150, 5, 6)
        cap_ratio("aerialDuelsWon", 80, 4, 5)
    elif pos_group == "MF":
        cap_ratio("goals", 10, 12, 14)
        cap_ratio("assists", 10, 14, 16)
        cap_ratio("shots", 60, 8, 10)
        cap_ratio("duelsWon", 200, 8, 10)
        cap_ratio("aerialDuelsWon", 100, 4, 5)
    elif pos_group == "ABW":
        cap_ratio("goals", 5, 8, 10)
        cap_ratio("assists", 6, 7, 8)
        cap_ratio("duelsWon", 220, 12, 14)
        cap_ratio("aerialDuelsWon", 140, 10, 12)
    elif pos_group == "TW":
        cap_ratio("saves", 100, 18, 20)
    else:
        cap_ratio("goals", 10, 10, 12)
        cap_ratio("assists", 8, 8, 10)
        cap_ratio("shots", 60, 7, 9)
        cap_ratio("duelsWon", 180, 6, 8)

    # Small pass-quality contribution; no pass volume inference.
    pa = prior.get("passAccuracy")
    if pa is not None:
        try:
            comps["passAccuracy"] = round(
                max(-2.0, min((float(pa) - 75.0) * 0.10, 3.0)),
                2,
            )
        except (TypeError, ValueError):
            pass

    raw = sum(comps.values())

    # Prior coverage controls how much of the bounded strength is trusted.
    trust = max(0.35, min(coverage / 90.0, 1.0))
    bonus = max(-5.0, min(raw * trust, 38.0))

    return {
        "bonus": round(bonus, 2),
        "coverage": coverage,
        "source": (historical_prior_coverage or {}).get("league") or "historical_prior",
        "components": comps,
    }



_V51_KB_HISTORY_CACHE = None

def v51_load_kickbase_history_store():
    """
    Optional local history store:
      {
        "players": {
          "harry-kane": [
            {"points": 124, "played": true, "source": "manual_verified"}
          ]
        }
      }

    This is deliberately separate from public Bundesliga statistics. A value is
    only treated as real Kickbase history when it is explicitly stored here (or
    already persisted as kickbaseMatchHistory on the player).
    """
    global _V51_KB_HISTORY_CACHE
    if _V51_KB_HISTORY_CACHE is not None:
        return _V51_KB_HISTORY_CACHE

    store = {"players": {}}
    try:
        if KICKBASE_MATCH_HISTORY_FILE.exists():
            loaded = json.loads(
                KICKBASE_MATCH_HISTORY_FILE.read_text(encoding="utf-8")
            )
            if isinstance(loaded, dict):
                store = loaded
    except Exception as exc:
        print(f"V51-KB-HISTORY: Datei konnte nicht geladen werden: {type(exc).__name__}")

    if not isinstance(store.get("players"), dict):
        store["players"] = {}

    _V51_KB_HISTORY_CACHE = store
    return store


def v51_player_kickbase_history(player_id, old_player=None):
    """Merge explicit store history with already-persisted player history."""
    old_player = old_player or {}
    merged = []

    store = v51_load_kickbase_history_store()
    external = (store.get("players") or {}).get(player_id) or []
    persisted = (
        old_player.get("kickbaseMatchHistory")
        or old_player.get("kickbasePointsHistory")
        or []
    )

    for item in list(persisted) + list(external):
        normalized = item
        if isinstance(item, (int, float)):
            normalized = {"points": item, "source": "legacy_history"}
        if not isinstance(normalized, dict):
            continue

        value = normalized.get("kickbasePoints", normalized.get("points"))
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if not (-50.0 <= value <= 500.0):
            continue

        record = dict(normalized)
        record["points"] = int(round(value))
        record.setdefault("played", True)
        merged.append(record)

    # Deduplicate exact records while preserving order.
    unique = []
    seen = set()
    for record in merged:
        key = (
            record.get("date"),
            record.get("opponent"),
            record.get("points"),
            record.get("source"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)

    return unique


def v51_kickbase_form_label(history):
    values = []
    for item in history or []:
        if isinstance(item, dict) and item.get("played") is False:
            continue
        value = item.get("points") if isinstance(item, dict) else item
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue

    if not values:
        return None

    recent = values[-5:]
    mean = sum(recent) / len(recent)
    last = recent[-1]
    if len(recent) == 1:
        return f"Letztes KB-Spiel: {int(round(last))} Pkt."
    return (
        f"Letzte {len(recent)} KB-Spiele: Ø {int(round(mean))} Pkt. "
        f"· zuletzt {int(round(last))}"
    )


def build_kickbase_ai_projection(
    player,
    performance,
    data_coverage,
    evidence_adj=None,
    historical_prior=None,
    historical_prior_coverage=None,
    official_gk_profile=None,
    kickbase_match_history=None,
):
    """
    V50 Kickbase-calibrated scenario expected-points engine.

    Structural changes:
    - Start probability and minutes-if-start are separate.
    - Goalkeepers: start ~= 90 minutes, bench ~= 0 minutes.
    - Field players: starter and substitute scenarios are modelled separately.
    - Historical 2025/26 prior contributes as a bounded strength signal.
    - Historical lineup evidence affects usage/role, not fake event production.
    - Missing public micro-actions remain unmodelled.
    """
    current_coverage = int(data_coverage.get("coveragePercent") or 0)
    evidence_adj = evidence_adj or {}
    evidence_multiplier = float(evidence_adj.get("projectionMultiplier") or 1.0)
    evidence_confidence_boost = int(evidence_adj.get("confidenceBoost") or 0)

    starting = str(player.get("starting") or "").lower()
    injury = str(player.get("injury") or "").lower()
    suspension = str(player.get("suspension") or "").lower()
    home_away = str(player.get("homeAway") or "").lower()
    pos_group = _v29_position_group(player.get("position"))

    if "verletzt" in injury or "gesperrt" in suspension:
        return {
            "expectedPoints": None,
            "rangeMin": None,
            "rangeMax": None,
            "confidence": current_coverage,
            "recommendation": "Nicht aufstellen",
            "reason": "Verletzung oder Sperre",
            "expectedMinutes": 0,
            "minutesIfStart": 0,
            "minutesIfBench": 0,
            "startProbability": 0,
            "positionModel": pos_group,
            "scenario": {"start": 0, "bench": 0},
            "model": "v44.1-open-data-calibration-wiring-fix",
        }

    # ------------------------------------------------------------
    # 1) Start probability from current public starting signal
    # ------------------------------------------------------------
    if "sehr wahrscheinlich" in starting:
        start_probability = 0.94
    elif "wahrscheinlich" in starting:
        start_probability = 0.83
    elif "eher bank" in starting:
        start_probability = 0.38
    elif "nicht" in starting and "recherchiert" not in starting:
        start_probability = 0.18
    else:
        start_probability = 0.56

    # Historical lineup evidence only adjusts the role/usage probability.
    start_probability *= evidence_multiplier

    # V31 goalkeeper role prior: a fit, unsuspended keeper with a strong
    # historical starter sample should not collapse to ~60% merely because
    # there is no current teamcheck article. Explicit negative current evidence
    # still wins.
    usage_prior = (historical_prior_coverage or {}).get("goalkeeperUsagePrior") or {}
    explicit_negative_start = (("nicht" in starting and "recherchiert" not in starting) or "eher bank" in starting or starting.strip() == "bank")
    if pos_group == "TW" and not explicit_negative_start:
        prior_starts = int(usage_prior.get("starts") or 0)
        prior_loaded = int(usage_prior.get("lineupsLoaded") or 0)
        prior_rate = prior_starts / max(prior_loaded, 1)
        if prior_loaded >= 20 and prior_rate >= 0.95:
            start_probability = max(start_probability, 0.96)
        elif prior_loaded >= 8 and prior_rate >= 0.90:
            start_probability = max(start_probability, 0.92)

    start_probability = max(0.02, min(start_probability, 0.99))

    # ------------------------------------------------------------
    # 2) Separate minutes by scenario
    # ------------------------------------------------------------
    if pos_group == "TW":
        minutes_if_start = 90.0
        minutes_if_bench = 0.0
    else:
        minutes_if_start = {
            "ABW": 83.0,
            "MF": 78.0,
            "ANG": 77.0,
            "ALL": 80.0,
        }[pos_group]
        minutes_if_bench = {
            "ABW": 16.0,
            "MF": 22.0,
            "ANG": 24.0,
            "ALL": 20.0,
        }[pos_group]

    # Current known historic minutes can gently adjust field-player start duration,
    # but never create the goalkeeper "58 minute" artefact.
    appearances = performance.get("appearances")
    minutes = performance.get("minutes")
    if pos_group != "TW" and minutes is not None and appearances:
        try:
            current_mpa = float(minutes) / max(float(appearances), 1.0)
            minutes_if_start = 0.82 * minutes_if_start + 0.18 * max(current_mpa, 55.0)
        except (TypeError, ValueError):
            pass

    minutes_if_start = max(0.0, min(minutes_if_start, 90.0))
    minutes_if_bench = max(0.0, min(minutes_if_bench, 35.0))

    expected_minutes = (
        start_probability * minutes_if_start
        + (1.0 - start_probability) * minutes_if_bench
    )

    # ------------------------------------------------------------
    # 3) Current public-performance starter scenario
    # ------------------------------------------------------------
    def per_appearance(value):
        if value is None or not appearances:
            return None
        try:
            return float(value) / max(float(appearances), 1.0)
        except (TypeError, ValueError):
            return None

    starter_components = {}

    # V42: official Kickbase participation actions.
    # Minutenbonus: +1 per completed 10 minutes, plus +1 for a full match.
    # Startelf: +5.
    completed_tens = int(minutes_if_start // 10)
    starter_components["minutesBase"] = float(completed_tens)
    if minutes_if_start >= 89.5:
        starter_components["minutesBase"] += 1.0
    starter_components["startingBonus"] = 5.0

    # Current season rates; only when explicitly available.
    rate_weights = {
        "TW": {
            "goals": 120, "assists": 55, "shots": 0.0, "duelsWon": 0.5,
            "aerialDuelsWon": 0.8, "crosses": 0.0, "saves": 0.0,
            "goalsAgainst": -5.0, "fouls": -2.0, "yellowCards": -10.0,
        },
        "ABW": {
            "goals": 100, "assists": 45, "shots": 3.0, "duelsWon": 1.3,
            "aerialDuelsWon": 1.5, "crosses": 0.7, "saves": 0.0,
            "goalsAgainst": 0.0, "fouls": -2.0, "yellowCards": -10.0,
        },
        "MF": {
            "goals": 90, "assists": 35, "shots": 3.5, "duelsWon": 1.0,
            "aerialDuelsWon": 0.8, "crosses": 1.0, "saves": 0.0,
            "goalsAgainst": 0.0, "fouls": -2.0, "yellowCards": -10.0,
        },
        "ANG": {
            "goals": 80, "assists": 35, "shots": 4.0, "duelsWon": 0.7,
            "aerialDuelsWon": 0.9, "crosses": 0.5, "saves": 0.0,
            "goalsAgainst": 0.0, "fouls": -2.0, "yellowCards": -10.0,
        },
        "ALL": {
            "goals": 65, "assists": 28, "shots": 3.0, "duelsWon": 0.9,
            "aerialDuelsWon": 0.9, "crosses": 0.7, "saves": 2.0,
            "cleanSheets": 7.0, "goalsAgainst": -1.0, "fouls": -0.9,
            "yellowCards": -7.0,
        },
    }[pos_group]

    for metric, weight in rate_weights.items():
        if weight == 0:
            continue
        rate = per_appearance(performance.get(metric))
        if rate is None:
            continue
        contribution = rate * weight * (minutes_if_start / 90.0)
        # Headline actions such as goals/assists may legitimately exceed 30.
        cap = 140.0 if metric in {"goals", "assists"} else 30.0
        starter_components[metric] = max(-30.0, min(contribution, cap))

    # V42: official position-dependent clean-sheet action.
    # Per completed 10 minutes: TW +5, ABW +3, MF +2, ANG +1.
    # A full 90-minute appearance receives one additional unit.
    cs_rate = per_appearance(performance.get("cleanSheets"))
    if cs_rate is not None:
        cs_unit = {"TW": 5.0, "ABW": 3.0, "MF": 2.0, "ANG": 1.0, "ALL": 1.0}[pos_group]
        cs_units = int(minutes_if_start // 10)
        if minutes_if_start >= 89.5:
            cs_units += 1
        starter_components["cleanSheets"] = max(
            0.0, min(cs_rate * cs_unit * cs_units, 50.0)
        )

    # Shots on target get the official +12 action value when available.
    sot = per_appearance(performance.get("shotsOnTarget"))
    if sot is not None:
        starter_components["shotsOnTarget"] = min(
            sot * 12.0 * (minutes_if_start / 90.0), 24.0
        )

    pa = performance.get("passAccuracy")
    if pa is not None:
        try:
            starter_components["passAccuracy"] = max(
                -4.0,
                min((float(pa) - 75.0) * 0.18, 4.0),
            )
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------
    # 4) Historical prior: bounded strength, separate from current totals
    # ------------------------------------------------------------
    prior_strength = _v29_prior_strength(
        historical_prior,
        historical_prior_coverage,
        pos_group,
    )
    starter_components["historicalPrior"] = prior_strength["bonus"]

    # V31: for goalkeepers, use explicit PUBLIC historical GK events as a
    # separate performance prior when current-season event data is missing.
    # This is not a Kickbase import and does not fabricate micro-actions.
    gk_prior_components = {}
    if pos_group == "TW":
        official_gk_profile = (
            official_gk_profile
            or (historical_prior_coverage or {}).get("currentGoalkeeperProfileValues")
            or {}
        )
        prior_apps = historical_prior.get("appearances")
        try:
            prior_apps = float(prior_apps) if prior_apps else 0.0
        except (TypeError, ValueError):
            prior_apps = 0.0

        if prior_apps > 0 or (official_gk_profile or {}).get("appearances"):
            # V38: do not gate live-season blending on performance['saves'].
            # V32 may already have copied the same official profile value there.
            if True:
                season_meta=(historical_prior_coverage or {}).get("goalkeeperSeasonPerformancePrior") or {}
                current_meta=(historical_prior_coverage or {}).get("goalkeeperCurrentSeasonPerformance") or {}
                current_values=(historical_prior_coverage or {}).get("goalkeeperCurrentSeasonValues") or {}

                # V36 source hierarchy for the live season:
                # 1) explicit official player profile
                # 2) explicit official season ranking
                # Missing remains unknown; never coerce absence to zero.
                try:
                    current_apps=int((official_gk_profile or {}).get("appearances") or 0)
                except (TypeError,ValueError):
                    current_apps=0

                profile_saves=(official_gk_profile or {}).get("saves")
                ranking_saves=current_values.get("saves")
                if profile_saves is not None and current_apps > 0:
                    current_saves=profile_saves
                    current_ok=True
                    current_source="official_profile"
                elif current_meta.get("explicit") is True and ranking_saves is not None and current_apps > 0:
                    current_saves=ranking_saves
                    current_ok=True
                    current_source="season_ranking"
                else:
                    current_saves=None
                    current_ok=False
                    current_source=None

                prior_saves=historical_prior.get("saves")
                prior_ok=(season_meta.get("explicit") is True and prior_apps >= 8)

                # V39 neutral goalkeeper baseline: 3.0 saves/appearance.
                # It is a model prior, not an observed player statistic, and is
                # used only when no usable historical player prior exists.
                neutral_gk_saves_rate = 3.0
                blended_rate,live_weight,blend_prior_source=_v39_blend_rate(
                    current_saves if current_ok else None,
                    current_apps,
                    prior_saves if prior_ok else None,
                    prior_apps,
                    neutral_rate=neutral_gk_saves_rate,
                )
                if blended_rate is not None:
                    # V40: aggregate "saves" does not reveal the Kickbase save subtype.
                    # Different goalkeeper actions carry different point values, so a
                    # fixed points-per-save multiplier would create false precision.
                    # Keep the blended save rate as predictive evidence only.
                    gk_prior_components["blendedSavesPerAppearance"]=round(float(blended_rate),3)
                    gk_prior_components["liveSeasonWeight"]=round(live_weight,3)
                    gk_prior_components["liveSeasonSource"]=current_source
                    gk_prior_components["saveScoringMode"]="aggregate_rate_evidence_only"
                    gk_prior_components["blendPriorSource"]=blend_prior_source
                    gk_prior_components["neutralGKSavesRate"]=neutral_gk_saves_rate if blend_prior_source == "neutral_gk_baseline" else None
                    gk_prior_components["liveSeasonAppearances"]=current_apps
                    if current_saves is not None:
                        gk_prior_components["liveSeasonSaves"]=current_saves
            if performance.get("cleanSheets") is None and historical_prior.get("cleanSheets") is not None:
                value = float(historical_prior["cleanSheets"]) / prior_apps * 50.0
                gk_prior_components["cleanSheets"] = max(0.0, min(value, 50.0))
            if performance.get("goalsAgainst") is None and historical_prior.get("goalsAgainst") is not None:
                value = float(historical_prior["goalsAgainst"]) / prior_apps * -5.0
                gk_prior_components["goalsAgainst"] = max(-20.0, min(value, 0.0))

        if gk_prior_components:
            # V38.1: gk_prior_components also contains diagnostic metadata
            # such as liveSeasonSource="official_profile". Sum only numeric
            # point-producing fields.
            numeric_gk_components = [
                value
                for key, value in gk_prior_components.items()
                if key in {"cleanSheets", "goalsAgainst"}
                and isinstance(value, (int, float))
            ]
            if numeric_gk_components:
                starter_components["historicalGoalkeeperPerformance"] = sum(
                    numeric_gk_components
                )

    if "heim" in home_away:
        starter_components["homeAway"] = 4.0
    elif "auswärts" in home_away:
        starter_components["homeAway"] = -2.0

    starter_points = sum(starter_components.values())

    # Substitute scenario: field player receives only a fraction of starter value.
    if pos_group == "TW":
        bench_points = 0.0
    else:
        # Separate participation base + limited event opportunity.
        event_without_bases = sum(
            value
            for key, value in starter_components.items()
            if key not in {"minutesBase", "startingBonus", "historicalPrior", "homeAway"}
        )
        bench_points = (
            9.0 * (minutes_if_bench / 25.0)
            + max(0.0, event_without_bases) * (minutes_if_bench / max(minutes_if_start, 1.0))
            + prior_strength["bonus"] * 0.15
        )

    raw_expected = (
        start_probability * starter_points
        + (1.0 - start_probability) * bench_points
    )

    # ------------------------------------------------------------
    # V50) Kickbase calibration layer
    # ------------------------------------------------------------
    # The public-event engine above is useful for relative player strength, but
    # it systematically underestimates real Kickbase totals because many
    # Kickbase micro-actions are not available from the public Bundesliga pages.
    # Therefore raw_expected is NOT exposed directly as the KB forecast anymore.
    #
    # Preferred signal: actual historical Kickbase match points supplied by the
    # app/old JSON. Supported entries: numbers or dicts with points/kickbasePoints.
    # Fallback: conservative position baseline + public model delta. No player
    # specific hardcodes.
    history_values = []
    for item in (kickbase_match_history or []):
        value = item
        if isinstance(item, dict):
            value = item.get("kickbasePoints", item.get("points"))
            # Ignore explicitly marked DNP/non-appearance records.
            if item.get("played") is False or item.get("minutes") == 0:
                continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if -50.0 <= value <= 500.0:
            history_values.append(value)

    # Recency weighting: newest values should be last in the stored list.
    kb_history_mean = None
    if history_values:
        recent = history_values[-8:]
        weights = list(range(1, len(recent) + 1))
        kb_history_mean = sum(v * w for v, w in zip(recent, weights)) / sum(weights)

    position_baseline = {"TW": 82.0, "ABW": 88.0, "MF": 92.0, "ANG": 96.0, "ALL": 90.0}[pos_group]

    # V51: public data is a modifier, never an unrestricted KB baseline.
    # Current cumulative/partial-season metrics can be noisy early in the year,
    # therefore the delta is deliberately bounded.
    public_center = {"TW": 28.0, "ABW": 38.0, "MF": 42.0, "ANG": 45.0, "ALL": 40.0}[pos_group]
    public_delta_raw = (raw_expected - public_center) * 0.35
    public_delta = max(-30.0, min(public_delta_raw, 35.0))
    fallback_starter = max(35.0, min(position_baseline + public_delta, 145.0))

    if kb_history_mean is not None:
        # A real KB match is much more informative than a generic position
        # baseline. Weight rises with sample size but one match is not allowed
        # to completely determine the forecast.
        n_hist = len(history_values[-8:])
        hist_weight = min(0.88, 0.48 + 0.07 * n_hist)
        calibrated_starter = (
            hist_weight * kb_history_mean
            + (1.0 - hist_weight) * fallback_starter
        )
        calibration_source = "kickbase_match_history"
    else:
        hist_weight = 0.0
        calibrated_starter = fallback_starter
        calibration_source = "position_public_fallback_v51"

    # Apply availability only once, after calibrating the full-match KB level.
    # A likely starter keeps most of his calibrated baseline; a bench scenario is
    # materially lower but never pretends that missing public micro-actions are 0.
    calibrated_bench = 0.0 if pos_group == "TW" else max(8.0, calibrated_starter * (minutes_if_bench / 90.0) * 0.82)
    expected_float = start_probability * calibrated_starter + (1.0 - start_probability) * calibrated_bench
    expected = int(round(max(0.0, min(expected_float, 300.0))))

    # ------------------------------------------------------------
    # 5) Confidence: current coverage + prior + lineup evidence are separate
    # ------------------------------------------------------------
    prior_cov = int((historical_prior_coverage or {}).get("coveragePercent") or 0)
    usage_prior = (historical_prior_coverage or {}).get("goalkeeperUsagePrior") or {}
    usage_confidence = 0
    if pos_group == "TW" and usage_prior.get("starts"):
        usage_sample = int(usage_prior.get("lineupsLoaded") or 0)
        usage_starts = int(usage_prior.get("starts") or 0)
        if usage_sample >= 8:
            usage_confidence = min(12, round(12 * usage_starts / usage_sample))

    confidence = (
        current_coverage * 0.62
        + prior_cov * 0.28
        + evidence_confidence_boost
        + usage_confidence
    )
    confidence = int(round(max(0.0, min(confidence, 100.0))))

    # Scenario uncertainty, not "half a match" averaging.
    scenario_gap = abs(starter_points - bench_points)
    uncertainty = (
        18.0
        + (100 - confidence) * 0.32
        + scenario_gap * min(start_probability, 1.0 - start_probability) * 0.45
    )
    if pos_group == "ANG":
        uncertainty += 9.0
    elif pos_group == "MF":
        uncertainty += 4.0
    uncertainty = int(round(max(18.0, min(uncertainty, 85.0))))

    range_min = max(0, expected - uncertainty)
    range_max = expected + uncertainty

    if expected_minutes < 20:
        recommendation = "Nicht starten"
    elif start_probability < 0.45:
        recommendation = "Joker / Riskant"
    elif expected >= 115:
        recommendation = "Top-Option"
    elif expected >= 80:
        recommendation = "Starten"
    elif expected >= 55:
        recommendation = "Gute Option"
    else:
        recommendation = "Beobachten"

    return {
        "expectedPoints": expected,
        "rangeMin": range_min,
        "rangeMax": range_max,
        "confidence": confidence,
        "recommendation": recommendation,
        "expectedMinutes": int(round(expected_minutes)),
        "minutesIfStart": int(round(minutes_if_start)),
        "minutesIfBench": int(round(minutes_if_bench)),
        "startProbability": int(round(start_probability * 100)),
        "positionModel": pos_group,
        "scenario": {
            "starterPoints": round(calibrated_starter, 1),
            "benchPoints": round(calibrated_bench, 1),
            "publicRawStarterPoints": round(starter_points, 1),
            "publicRawBenchPoints": round(bench_points, 1),
        },
        "kickbaseCalibration": {
            "source": calibration_source,
            "historyMatches": len(history_values),
            "historyMean": round(kb_history_mean, 1) if kb_history_mean is not None else None,
            "historyWeight": round(hist_weight, 2),
            "positionBaseline": position_baseline,
            "publicRawExpected": round(raw_expected, 1),
            "publicDeltaRaw": round(public_delta_raw, 1),
            "publicDeltaApplied": round(public_delta, 1),
            "calibratedStarter": round(calibrated_starter, 1),
        },
        "historicalPriorStrength": prior_strength,
        "goalkeeperPrior": (
            (historical_prior_coverage or {}).get("goalkeeperUsagePrior")
            if pos_group == "TW" else None
        ),
        "goalkeeperPerformancePrior": (
            (historical_prior_coverage or {}).get("goalkeeperPerformancePrior")
            if pos_group == "TW" else None
        ),
        "goalkeeperCurrentSeasonPerformance": (
            (historical_prior_coverage or {}).get("goalkeeperCurrentSeasonPerformance")
            if pos_group == "TW" else None
        ),
        "goalkeeperSeasonPerformancePrior": (
            (historical_prior_coverage or {}).get("goalkeeperSeasonPerformancePrior")
            if pos_group == "TW" else None
        ),
        "goalkeeperPriorComponents": (
            gk_prior_components if pos_group == "TW" else {}
        ),
        "evidence": {
            "roleSignal": evidence_adj.get("roleSignal", "unknown"),
            "lineupRatio": evidence_adj.get("ratio"),
            "matchesFound": evidence_adj.get("matchesFound", 0),
            "sample": evidence_adj.get("sample", 0),
            "usageMultiplier": round(evidence_multiplier, 2),
            "confidenceBoost": evidence_confidence_boost,
        },
        "components": {
            key: round(value, 1)
            for key, value in starter_components.items()
        },
        "positionScoring": {
            "goal": {"TW": 120, "ABW": 100, "MF": 90, "ANG": 80, "ALL": 80}.get(pos_group),
            "assist": {"TW": 55, "ABW": 45, "MF": 35, "ANG": 35, "ALL": 35}.get(pos_group),
            "cleanSheetPer10": {"TW": 5, "ABW": 3, "MF": 2, "ANG": 1, "ALL": 1}.get(pos_group),
            "startingXI": 5,
            "minutePer10": 1,
            "goalConcededTW": -5 if pos_group == "TW" else None,
        },
        "reason": (
            f"{pos_group}-Szenariomodell | Start {int(round(start_probability*100))}% | "
            f"{int(round(minutes_if_start))} Min bei Start | "
            f"Current {current_coverage}% | Prior {prior_cov}%"
        ),
        "model": "v51-kickbase-history-calibrated",
        "disclaimer": (
            "Kickbase-kalibrierte Prognose. Reale Kickbase-Historie wird bevorzugt; "
            "ohne Historie dient ein positionsbasierter Public-Data-Fallback als Basis."
        ),
    }


def build_performance_layer(values, old_performance=None):
    """
    V18: Transparente Statistikschicht für den späteren Kickbase-AI Score.
    Es werden ausschließlich tatsächlich gefundene Werte gespeichert.
    Kein Wert wird aus anderen Statistiken geschätzt.
    """
    old_performance = old_performance or {}

    metric_names = (
        "appearances",
        "starts",
        "minutes",
        "goals",
        "assists",
        "shots",
        "shotsOnTarget",
        "woodwork",
        "penalties",
        "penaltiesScored",
        "passAccuracy",
        "duelsWon",
        "aerialDuelsWon",
        "crosses",
        "fouls",
        "yellowCards",
        "redCards",
        "saves",
        "cleanSheets",
        "goalsAgainst",
        "distanceKm",
        "sprints",
        "intensiveRuns",
        "topSpeedKmh",
    )

    performance = {}
    for key in metric_names:
        new_value = values.get(key)
        performance[key] = (
            new_value
            if new_value is not None
            else old_performance.get(key)
        )

    available = [key for key in metric_names if performance.get(key) is not None]
    missing = [key for key in metric_names if performance.get(key) is None]
    coverage = round((len(available) / len(metric_names)) * 100) if metric_names else 0

    return performance, {
        "availableMetrics": available,
        "missingMetrics": missing,
        "coveragePercent": coverage,
        "scoreReady": False,  # V23 pauses scoring until coverage is validated
        "note": (
            "Nur explizit öffentlich gefundene Werte; nicht gelistet wird NICHT "
            "mehr als 0 interpretiert."
        ),
    }


def _is_goalkeeper(player):
    position = normalize_name(player.get("position"))
    return position in {"torhuter", "torhueter", "tw", "goalkeeper"}


def _localized_profile_urls(source_url):
    """Alternative offizielle Bundesliga.com-Sprachpfade für dasselbe Profil."""
    replacements = (
        ("/de/spieler/", "/pt/jogador/"),
        ("/de/spieler/", "/en/player/"),
        ("/de/spieler/", "/es/jugador/"),
        ("/de/spieler/", "/fr/joueur/"),
    )
    urls = []
    for old, new in replacements:
        if old in source_url:
            candidate = source_url.replace(old, new, 1)
            if candidate not in urls:
                urls.append(candidate)
    return urls




def _official_host_matches(url, domain):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False

    domain = (domain or "").lower()
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def _extract_xml_locs(xml_text):
    if not xml_text:
        return []

    return [
        match.strip()
        for match in re.findall(
            r"<loc>\s*(.*?)\s*</loc>",
            xml_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match.strip()
    ]


def _candidate_sitemap_urls(domain):
    base = f"https://{domain}"
    return [
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap-index.xml",
        f"{base}/news-sitemap.xml",
        f"{base}/sitemap-news.xml",
    ]


def _url_looks_status_relevant(url):
    lower = (url or "").lower()
    year = str(datetime.now(timezone.utc).year)

    keyword_hit = any(keyword in lower for keyword in STATUS_URL_KEYWORDS)
    current_year = year in lower
    previous_year = str(int(year) - 1) in lower

    return keyword_hit and (current_year or previous_year or "/news/" in lower)


def _decode_duckduckgo_url(href):
    if not href:
        return None

    href = href.replace("&amp;", "&")
    if href.startswith("//"):
        href = "https:" + href

    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        try:
            parsed = urlparse(
                href if href.startswith("http")
                else "https://duckduckgo.com" + href
            )
            uddg = parse_qs(parsed.query).get("uddg", [None])[0]
            return unquote(uddg) if uddg else None
        except Exception:
            return None

    return href if href.startswith("http") else None


def _discover_urls_via_sitemaps(domain, max_urls=35):
    """
    Holt Status-/News-URLs ausschließlich von der offiziellen Vereinsdomain.
    Sitemap-Indizes werden maximal eine Ebene rekursiv verfolgt.
    """
    result = []
    seen = set()
    sitemap_queue = _candidate_sitemap_urls(domain)
    processed_sitemaps = set()

    while sitemap_queue and len(result) < max_urls:
        sitemap_url = sitemap_queue.pop(0)

        if sitemap_url in processed_sitemaps:
            continue

        processed_sitemaps.add(sitemap_url)

        try:
            body = http_get_text(sitemap_url, timeout=12)
        except Exception:
            continue

        locs = _extract_xml_locs(body)

        for loc in locs:
            if not _official_host_matches(loc, domain):
                continue

            lower = loc.lower()

            if (
                "sitemap" in lower
                and loc not in processed_sitemaps
                and len(processed_sitemaps) < 12
            ):
                sitemap_queue.append(loc)
                continue

            if (
                loc not in seen
                and _url_looks_status_relevant(loc)
            ):
                seen.add(loc)
                result.append(loc)

                if len(result) >= max_urls:
                    break

    return result


def _discover_urls_via_search(player_name, domain, max_urls=8):
    """
    Fallback nur zur Link-Findung. Inhalt wird anschließend ausschließlich
    von der offiziellen Vereinsdomain geladen.
    """
    year = datetime.now(timezone.utc).year
    queries = (
        f'site:{domain} "{player_name}" Verletzung {year}',
        f'site:{domain} "{player_name}" Reha {year}',
        f'site:{domain} "{player_name}" Personal {year}',
        f'site:{domain} "{player_name}" gesperrt {year}',
    )

    found = []
    seen = set()

    for query in queries:
        try:
            url = SEARCH_URL + "?" + urlencode({"q": query})
            html = http_get_text(
                url,
                headers={"Accept-Language": "de-DE,de;q=0.9"},
                timeout=15,
            )
        except Exception:
            continue

        hrefs = re.findall(
            r'<a[^>]+href=["\']([^"\']+)["\']',
            html,
            flags=re.IGNORECASE,
        )

        for href in hrefs:
            target = _decode_duckduckgo_url(href)

            if (
                not target
                or target in seen
                or not _official_host_matches(target, domain)
            ):
                continue

            seen.add(target)
            found.append(target)

            if len(found) >= max_urls:
                return found

    return found


def _search_query_urls(query, allowed_domains=None, max_urls=12):
    """
    Sucht gezielt nach einem Spieler. Die Suchmaschine dient ausschließlich
    zur URL-Ermittlung; die Inhalte werden danach von den Zielseiten geladen.
    """
    found = []
    seen = set()

    try:
        url = SEARCH_URL + "?" + urlencode({"q": query})
        html = http_get_text(
            url,
            headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.7"},
            timeout=STATUS_SEARCH_TIMEOUT,
        )
    except Exception as exc:
        print(f"SEARCH-DEBUG: Suche fehlgeschlagen: {query} -> {exc}")
        return []

    hrefs = re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )

    for href in hrefs:
        target = _decode_duckduckgo_url(href)
        if not target or target in seen:
            continue

        try:
            host = (urlparse(target).hostname or "").lower()
        except Exception:
            continue

        if allowed_domains:
            ok = any(
                host == domain or host.endswith("." + domain)
                for domain in allowed_domains
            )
            if not ok:
                continue

        seen.add(target)
        found.append(target)

        if len(found) >= max_urls:
            break

    return found



def _matchday_from_next_match(next_match):
    if not isinstance(next_match, dict):
        return None

    match = next_match.get("match") or {}
    group = match.get("group") or {}

    candidates = (
        group.get("groupOrderID"),
        group.get("groupOrderId"),
        match.get("groupOrderID"),
        match.get("groupOrderId"),
    )

    for value in candidates:
        try:
            number = int(value)
            if 1 <= number <= 34:
                return number
        except (TypeError, ValueError):
            continue

    return None


def _get_matchday_status_text(matchday):
    """
    Lädt die offizielle Bundesliga-Spieltagübersicht genau EINMAL pro Lauf.
    Die Seite enthält voraussichtliche Aufstellungen sowie 'Es fehlen'
    mit Verletzungen/Sperren für alle Vereine.
    """
    if not matchday:
        return None, None

    if matchday in _MATCHDAY_STATUS_CACHE:
        return _MATCHDAY_STATUS_CACHE[matchday]

    url = BUNDESLIGA_MATCHDAY_STATUS_TEMPLATE.format(matchday=matchday)

    try:
        html = http_get_text(url, timeout=10)
        page_text = _html_to_visible_text(html)
        result = (page_text, url)
        print(
            f"MATCHDAY-STATUS: Spieltag {matchday} geladen "
            f"({len(page_text)} Zeichen)."
        )
    except Exception as exc:
        print(
            f"MATCHDAY-STATUS FEHLER: Spieltag {matchday} "
            f"konnte nicht geladen werden: {exc}"
        )
        result = (None, url)

    _MATCHDAY_STATUS_CACHE[matchday] = result
    return result


def _extract_team_match_section(page_text, club, opponent):
    """
    Schneidet möglichst nur die Partie des gesuchten Vereins aus der
    Ligaübersicht heraus.
    """
    if not page_text:
        return ""

    patterns = []
    if club and opponent:
        patterns.extend([
            rf"{re.escape(club)}\s*-\s*{re.escape(opponent)}",
            rf"{re.escape(opponent)}\s*-\s*{re.escape(club)}",
        ])

    start = None
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE)
        if match:
            start = match.start()
            break

    if start is None:
        # Fallback: erster Clubtreffer
        match = re.search(re.escape(club or ""), page_text, flags=re.IGNORECASE)
        start = match.start() if match else 0

    # 3500 Zeichen reichen typischerweise für beide Teams einer Partie,
    # ohne große Teile des nächsten Spiels mitzunehmen.
    return page_text[start:start + 3500]


def _classify_absence_reason(reason):
    lower = (reason or "").lower()

    suspension_terms = (
        "sperre", "gesperrt", "rotsperre", "gelbsperre",
        "gelb-rot", "rote karte", "gelbe karte",
    )
    non_injury_terms = (
        "nicht berücksichtigt",
        "nicht beruecksichtigt",
        "belastungssteuerung",
        "rotation",
    )

    if any(term in lower for term in suspension_terms):
        return "suspension"

    if any(term in lower for term in non_injury_terms):
        return "other"

    return "injury"



def _name_matches_in_lineup(lineup_text, player_name):
    if not lineup_text or not player_name:
        return False
    parts = str(player_name).split()
    surname = parts[-1] if parts else player_name
    patterns = [re.escape(player_name)]
    if surname and surname != player_name:
        patterns.append(re.escape(surname))
    return any(
        re.search(rf"\b{pattern}\b", lineup_text, flags=re.IGNORECASE)
        for pattern in patterns
    )


def _extract_team_lineup(section, club):
    """
    Extrahiert die voraussichtliche Elf aus geglättetem Seitentext.
    Funktioniert ohne Zeilenumbrüche.
    """
    if not section or not club:
        return ""

    aliases = {
        "FC Bayern München": ("FCB", "Bayern", "FC Bayern München"),
        "VfB Stuttgart": ("VFB", "VfB Stuttgart", "Stuttgart"),
        "RB Leipzig": ("RBL", "RB Leipzig", "Leipzig"),
        "Borussia Mönchengladbach": ("BMG", "Borussia Mönchengladbach", "Gladbach"),
        "1. FSV Mainz 05": ("M05", "1. FSV Mainz 05", "Mainz"),
        "SC Paderborn 07": ("SCP", "SC Paderborn 07", "Paderborn"),
        "1. FC Union Berlin": ("FCU", "1. FC Union Berlin", "Union Berlin"),
        "Eintracht Frankfurt": ("SGE", "Eintracht Frankfurt", "Frankfurt"),
        "1. FC Köln": ("KOE", "1. FC Köln", "Köln"),
        "TSG Hoffenheim": ("TSG", "TSG Hoffenheim", "Hoffenheim"),
        "SV 07 Elversberg": ("SVE", "SV 07 Elversberg", "Elversberg"),
        "Bayer 04 Leverkusen": ("B04", "Bayer 04 Leverkusen", "Leverkusen"),
        "Borussia Dortmund": ("BVB", "Borussia Dortmund", "Dortmund"),
        "Hamburger SV": ("HSV", "Hamburger SV"),
        "SC Freiburg": ("SCF", "SC Freiburg", "Freiburg"),
        "SV Werder Bremen": ("SVW", "SV Werder Bremen", "Werder Bremen"),
        "FC Augsburg": ("FCA", "FC Augsburg", "Augsburg"),
        "FC Schalke 04": ("S04", "FC Schalke 04", "Schalke"),
    }

    labels = aliases.get(club, (club,))
    start_candidates = []

    for label in labels:
        for pat in (
            rf"Voraussichtliche\s+Aufstellung(?:en)?\s+{re.escape(label)}\s*[:\-]?\s*",
            rf"Predicted\s+line-?up\s*[:\-]?\s*{re.escape(label)}\s*",
            rf"\b{re.escape(label)}\s*:\s*",
        ):
            match = re.search(pat, section, flags=re.IGNORECASE)
            if match:
                start_candidates.append(match.end())

    if not start_candidates:
        return ""

    start = min(start_candidates)
    tail = section[start:]

    end_patterns = [
        r"\bEs\s+fehlen\s*:",
        r"\bVoraussichtliche\s+Aufstellung(?:en)?\b",
        r"\bPredicted\s+line-?up\b",
    ]

    end = len(tail)
    for end_pattern in end_patterns:
        match = re.search(end_pattern, tail, flags=re.IGNORECASE)
        if match and match.start() > 10:
            end = min(end, match.start())

    return re.sub(r"\s+", " ", tail[:end]).strip()



def _slugify_for_search(value):
    value = str(value or "").lower()
    value = (
        value.replace("ä", "a")
        .replace("ö", "o")
        .replace("ü", "u")
        .replace("ß", "ss")
    )
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _discover_bundesliga_teamcheck_url(club):
    """
    V17: Findet den Teamcheck ausschließlich über die offizielle
    Bundesliga-Club-Newsseite. Keine Google-/Suchmaschinen-Abhängigkeit.
    """
    if not club:
        return None

    cached = _TEAMCHECK_CACHE.get(club)
    if cached and "url" in cached:
        return cached.get("url")

    slug = BUNDESLIGA_CLUB_NEWS_SLUGS.get(club)
    if not slug:
        _TEAMCHECK_CACHE[club] = {"url": None, "text": None}
        print(f"TEAMCHECK-DISCOVERY {club}: kein Bundesliga-Club-Slug hinterlegt.")
        return None

    news_url = f"https://www.bundesliga.com/de/bundesliga/clubs/{slug}/news"

    try:
        html = http_get_text(news_url, timeout=10)
    except Exception as exc:
        _TEAMCHECK_CACHE[club] = {"url": None, "text": None}
        print(f"TEAMCHECK-DISCOVERY {club}: Newsseite nicht ladbar -> {exc}")
        return None

    # Direkte absolute und relative Teamcheck-Links akzeptieren.
    hrefs = re.findall(
        r'href=["\']([^"\']*teamcheck-saisonvorschau-2026-27[^"\']*)["\']',
        html,
        flags=re.IGNORECASE,
    )

    candidates = []
    for href in hrefs:
        href = href.replace("&amp;", "&").strip()

        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.bundesliga.com" + href
        elif not href.startswith("http"):
            continue

        # Nur Bundesliga-Domain und aktuelle Saison.
        if (
            "bundesliga.com/" not in href.lower()
            or "teamcheck-saisonvorschau-2026-27" not in href.lower()
        ):
            continue

        href = href.split("?")[0].split("#")[0]
        if href not in candidates:
            candidates.append(href)

    # Falls mehrere Teamchecks auf der Newsseite auftauchen:
    # Clubname/Slug im Link bevorzugen.
    club_tokens = [
        token
        for token in re.sub(r"[^a-z0-9]+", "-", slug.lower()).split("-")
        if len(token) >= 3 and token not in {"club"}
    ]

    best_url = None
    best_score = -1

    for url in candidates:
        lower = url.lower()
        score = sum(token in lower for token in club_tokens)
        if score > best_score:
            best_score = score
            best_url = url

    _TEAMCHECK_CACHE[club] = {
        "url": best_url,
        "text": None,
        "newsUrl": news_url,
    }

    print(
        f"TEAMCHECK-DISCOVERY {club}: "
        f"{len(candidates)} Kandidat(en) -> {best_url or 'kein Treffer'}"
    )

    return best_url


def _get_teamcheck_text(club):
    cached = _TEAMCHECK_CACHE.get(club)
    if cached and cached.get("text"):
        return cached["text"], cached.get("url")

    url = _discover_bundesliga_teamcheck_url(club)
    if not url:
        return None, None

    try:
        html = http_get_text(url, timeout=10)
        page_text = _html_to_visible_text(html)
    except Exception as exc:
        print(f"TEAMCHECK-FEHLER {club}: {exc}")
        return None, url

    _TEAMCHECK_CACHE[club] = {"url": url, "text": page_text}
    print(f"TEAMCHECK {club}: geladen ({len(page_text)} Zeichen).")
    return page_text, url


def _player_in_teamcheck_starting_xi(player_name, page_text):
    if not player_name or not page_text:
        return False

    marker = re.search(
        r"Voraussichtliche\s+Aufstellung\s+am\s+\d+\.\s*Spieltag\s*:",
        page_text,
        flags=re.IGNORECASE,
    )
    if not marker:
        return False

    tail = page_text[marker.end():marker.end() + 1800]

    # Stoppe an einem typischen Artikel-/Autorenmarker, sofern vorhanden.
    stop = re.search(
        r"\b(?:Weitere Videos|Empfohlener redaktioneller Inhalt|Anzeige)\b",
        tail,
        flags=re.IGNORECASE,
    )
    if stop:
        tail = tail[:stop.start()]

    return _name_matches_in_lineup(tail, player_name)


def _derive_lineup_probability(player, section):
    player_name = str(player.get("name") or "").strip()
    club = str(player.get("club") or "").strip()
    lineup = _extract_team_lineup(section, club)

    # Matchday-Aufstellung eindeutig erkannt.
    if _name_matches_in_lineup(lineup, player_name):
        return "Sehr wahrscheinlich"

    # Explizite Ausfallliste hat Vorrang.
    parts = player_name.split()
    surname = parts[-1] if parts else player_name
    if re.search(
        rf"\b{re.escape(surname)}\b\s*\(",
        section or "",
        flags=re.IGNORECASE,
    ):
        return "Nein"

    # Spieler erscheint im Aufstellungsbereich, aber Parser kann die
    # exakte Teamzeile nicht sicher isolieren.
    first_missing = re.search(
        r"\bEs\s+fehlen\s*:",
        section or "",
        flags=re.IGNORECASE,
    )
    pre_missing = (
        (section or "")[:first_missing.start()]
        if first_missing
        else (section or "")
    )

    if _name_matches_in_lineup(pre_missing, player_name):
        return "Wahrscheinlich"

    # V17: offizieller Bundesliga-Teamcheck über Club-Newsseite.
    teamcheck_text, teamcheck_url = _get_teamcheck_text(club)

    if teamcheck_text:
        if _player_in_teamcheck_starting_xi(player_name, teamcheck_text):
            print(
                f"TEAMCHECK-STARTELF {player_name}: JA | {teamcheck_url}"
            )
            return "Wahrscheinlich"

        print(
            f"TEAMCHECK-STARTELF {player_name}: NEIN | {teamcheck_url}"
        )
        return "Eher Bank / offen"

    # Keine belastbare Aufstellungsquelle -> keine negative Behauptung.
    print(
        f"TEAMCHECK-STARTELF {player_name}: "
        "keine belastbare Teamcheck-Quelle gefunden."
    )
    return "Noch nicht recherchiert"


def _derive_form_from_stats(player):
    """
    Form bleibt bewusst konservativ und nutzt nur bereits vorhandene
    strukturierte Match-/Spielerwerte. Keine erfundenen Kickbase-Punkte.
    """
    appearances = player.get("appearances")
    starts = player.get("starts")
    goals = player.get("goals")
    assists = player.get("assists")

    nums = []
    for value in (appearances, starts, goals, assists):
        try:
            nums.append(int(value) if value is not None else None)
        except (TypeError, ValueError):
            nums.append(None)

    appearances, starts, goals, assists = nums

    if appearances is None:
        return "Noch keine belastbare Formbasis"

    if appearances == 0:
        return "Noch kein Saisoneinsatz"

    parts = [f"{appearances} Einsätze"]
    if starts is not None:
        parts.append(f"{starts} Startelf")
    if goals is not None:
        parts.append(f"{goals} Tore")
    if assists is not None:
        parts.append(f"{assists} Vorlagen")
    return " · ".join(parts)


def _normalize_yellow_cards(player):
    for key in ("yellowCards", "yellow_cards", "yellowcards"):
        value = player.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return 0

def get_matchday_status_for_player(player):
    """
    V14: Status aus einer einzigen offiziellen Bundesliga-Seite.

    Wenn ein Spieler unter 'Es fehlen' mit Grund aufgeführt wird:
      - Sperrgrund -> Gesperrt
      - sonst      -> Verletzt + Grund

    Wenn die aktuelle Spieltagseite erfolgreich geladen wurde und der
    Spieler NICHT unter 'Es fehlen' steht:
      - Verletzung -> Fit
      - Sperre     -> Keine Sperre
    """
    player_name = str(player.get("name") or "").strip()
    club = str(player.get("club") or "").strip()
    opponent = str(player.get("_opponent") or "").strip()
    matchday = player.get("_matchday")

    page_text, source_url = _get_matchday_status_text(matchday)

    if not page_text:
        return {
            "checked": False,
            "evidence": "unknown",
            "injured": False,
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "suspended": False,
            "sourceUrl": None,
        }

    section = _extract_team_match_section(
        page_text,
        club,
        opponent,
    )

    lineup_probability = _derive_lineup_probability(player, section)

    parts = player_name.split()
    surname = parts[-1] if parts else player_name

    # Bundesliga führt in der Aufstellung meist nur den Nachnamen.
    name_patterns = []
    if player_name:
        name_patterns.append(re.escape(player_name))
    if surname and surname != player_name:
        name_patterns.append(re.escape(surname))

    reason = None

    for name_pattern in name_patterns:
        # Abwesenheitslisten haben zuverlässig die Form:
        # Spielername (Grund)
        matches = list(re.finditer(
            rf"\b{name_pattern}\b\s*\(([^)]{{2,180}})\)",
            section,
            flags=re.IGNORECASE,
        ))
        if matches:
            reason = re.sub(
                r"\s+",
                " ",
                matches[0].group(1),
            ).strip()
            break

    if reason:
        category = _classify_absence_reason(reason)

        if category == "suspension":
            print(
                f"MATCHDAY-STATUS {player_name}: "
                f"Gesperrt | Grund={reason}"
            )
            return {
                "checked": True,
                "evidence": "suspended",
                "injured": False,
                "injuryDiagnosis": None,
                "injuryExpectedAbsence": None,
                "suspended": True,
                "sourceUrl": source_url,
                "lineupProbability": lineup_probability,
            }

        if category == "injury":
            print(
                f"MATCHDAY-STATUS {player_name}: "
                f"Verletzt | Grund={reason}"
            )
            return {
                "checked": True,
                "evidence": "injured",
                "injured": True,
                "injuryDiagnosis": reason,
                "injuryExpectedAbsence": None,
                "suspended": False,
                "sourceUrl": source_url,
                "lineupProbability": lineup_probability,
            }

        # Nicht berücksichtigt / Rotation etc. ist keine Verletzung.
        print(
            f"MATCHDAY-STATUS {player_name}: "
            f"kein Verletzungs-/Sperrgrund | Hinweis={reason}"
        )
        return {
            "checked": True,
            "evidence": "fit",
            "injured": False,
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "suspended": False,
            "sourceUrl": source_url,
            "lineupProbability": lineup_probability,
        }

    # Seite erfolgreich geprüft und Spieler steht nicht in der Ausfallliste.
    print(
        f"MATCHDAY-STATUS {player_name}: "
        "nicht unter 'Es fehlen' -> Fit / Keine Sperre"
    )
    return {
        "checked": True,
        "evidence": "fit",
        "injured": False,
        "injuryDiagnosis": None,
        "injuryExpectedAbsence": None,
        "suspended": False,
        "sourceUrl": source_url,
        "lineupProbability": lineup_probability,
    }


def discover_official_status_urls(player):
    """
    V13 FAST:
    - keine Sitemap-Crawls
    - maximal EINE gezielte Suche pro Spieler
    - höchstens wenige offizielle Kandidaten
    - bekannte offizielle Club-Seeds bleiben kostenlos nutzbar

    Dadurch vermeiden wir die 19-Minuten-Laufzeit aus V12.
    """
    club = str(player.get("club") or "").strip()
    player_name = str(player.get("name") or "").strip()
    domain = OFFICIAL_CLUB_DOMAINS.get(club)

    if not domain or not player_name:
        return []

    urls = []
    seen = set()

    def add(url):
        if (
            url
            and url not in seen
            and _official_host_matches(url, domain)
        ):
            seen.add(url)
            urls.append(url)

    # Kostenlose bekannte Seeds zuerst
    for url in OFFICIAL_STATUS_SEEDS.get(club, []):
        add(url)

    # Nur EINE kombinierte Suchanfrage
    query = (
        f'site:{domain} "{player_name}" '
        f'(Verletzung OR verletzt OR Reha OR Training OR gesperrt OR Personal)'
    )

    for url in _search_query_urls(
        query,
        allowed_domains=[domain],
        max_urls=STATUS_MAX_CANDIDATE_URLS,
    ):
        add(url)

    print(
        f"STATUS-DISCOVERY {player_name}: "
        f"{len(urls)} offizielle Kandidaten für {club}."
    )

    return urls[:STATUS_MAX_CANDIDATE_URLS]


def _html_to_visible_text(html):
    if not html:
        return ""

    value = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(r"<[^>]+>", " ", value)
    value = (
        value.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\s+", " ", value).strip()


def _player_text_windows(text, player_name, radius=320):
    """
    Liefert nur Textbereiche direkt um den Spielernamen.
    Dadurch wird vermieden, dass die Verletzung/Sperre eines
    Teamkollegen dem falschen Spieler zugeordnet wird.
    """
    if not text or not player_name:
        return []

    name_pattern = re.escape(player_name)
    windows = []

    for match in re.finditer(name_pattern, text, flags=re.IGNORECASE):
        start = max(0, match.start() - radius)
        end = min(len(text), match.end() + radius)
        windows.append(text[start:end])

    # Fallback auf Nachnamen, wenn der vollständige Name in der Meldung
    # anders geschrieben ist.
    if not windows:
        parts = str(player_name).strip().split()
        if parts:
            surname = parts[-1]
            for match in re.finditer(
                re.escape(surname),
                text,
                flags=re.IGNORECASE,
            ):
                start = max(0, match.start() - radius)
                end = min(len(text), match.end() + radius)
                windows.append(text[start:end])

    return windows


def _diagnosis_from_text(text):
    lower = (text or "").lower()

    # Konkreten Kreuzbandbefund inklusive Knie-Seite bevorzugen.
    if (
        "riss des vorderen kreuzbandes" in lower
        or "kreuzbandriss" in lower
    ):
        side = None
        if "linken knie" in lower or "linkes knie" in lower:
            side = "linkes Knie"
        elif "rechten knie" in lower or "rechtes knie" in lower:
            side = "rechtes Knie"

        return (
            f"Kreuzbandriss ({side})"
            if side
            else "Kreuzbandriss"
        )

    for token, label in INJURY_DIAGNOSES:
        if token in lower:
            return label

    return None


def _absence_from_text(text):
    lower = (text or "").lower()

    if "weiterhin in der reha" in lower or "noch in der reha" in lower:
        return "weiterhin Reha; Rückkehr nicht öffentlich terminiert"

    if "in der reha" in lower or "in reha" in lower:
        return "Reha; Rückkehr nicht öffentlich terminiert"

    patterns = (
        r"\bvoraussichtlich\s+für\s+([^.;]{2,80})",
        r"\bvoraussichtlich\s+bis\s+([^.;]{2,80})",
        r"\bausfallzeit\s*[:\-]\s*([^.;]{2,80})",
        r"\bfällt\s+für\s+([^.;]{2,80})\s+aus",
        r"\bfehlt\s+für\s+([^.;]{2,80})",
    )

    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;-")
            if value:
                return value[:80]

    return None


def _format_injury(injured, diagnosis=None, absence=None):
    if not injured:
        return "Fit"

    parts = ["Verletzt"]

    if diagnosis:
        parts.append(diagnosis)
    else:
        parts.append("Diagnose nicht öffentlich verfügbar")

    if absence:
        parts.append(absence)
    else:
        parts.append("Ausfallzeit nicht öffentlich verfügbar")

    return " · ".join(parts)



def _normalize_injury_label(value):
    """Entfernt alte UI-Texte aus früheren Script-Versionen."""
    if value is None:
        return value

    raw = str(value).strip()
    if raw.lower() in LEGACY_NO_INJURY_TEXTS:
        return "Status nicht eindeutig verfügbar"

    return raw


def _normalize_suspension_label(value):
    """Sperre wird nur noch als Gesperrt / Keine Sperre / unbekannt gespeichert."""
    if value is None:
        return value

    raw = str(value).strip()
    lower = raw.lower()

    if lower in LEGACY_NO_SUSPENSION_TEXTS:
        return "Keine Sperre"

    if lower in {"gesperrt", "keine sperre", "noch nicht recherchiert"}:
        return raw

    if "gesperrt" in lower or "sperre" in lower:
        return "Gesperrt"

    return raw


def _window_has_recovery_for_player(window):
    """
    Recovery zählt nur, wenn die Rückkehr-/Fit-Formulierung im direkten
    Spielerfenster steht. So neutralisiert die Rückkehr eines Teamkollegen
    nicht die Verletzung des gesuchten Spielers.
    """
    lower = (window or "").lower()
    return any(term in lower for term in RECOVERY_TERMS)


def get_official_status_for_player(player):
    """
    Generische Statusrecherche für jeden aktiven Kaderspieler.

    Ergebnis-Evidenz:
      injured   -> aktuelle offizielle Verletzungsmeldung
      recovered -> explizite Rückkehr-/Fit-Meldung
      suspended -> aktuelle Sperrmeldung
      unknown   -> keine belastbare Statusaussage gefunden

    Keine spielerspezifischen Sonderfälle.
    """
    global _STATUS_RESEARCH_STARTED_AT

    if _STATUS_RESEARCH_STARTED_AT is None:
        _STATUS_RESEARCH_STARTED_AT = time.monotonic()

    club = str(player.get("club") or "").strip()
    player_name = str(player.get("name") or "").strip()

    elapsed = time.monotonic() - _STATUS_RESEARCH_STARTED_AT
    if elapsed >= STATUS_GLOBAL_BUDGET_SECONDS:
        print(
            f"STATUS {player_name}: Fast-Mode-Zeitbudget erreicht; "
            "vorhandene Daten werden beibehalten."
        )
        return {
            "checked": False,
            "evidence": "budget_exhausted",
            "injured": False,
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "suspended": False,
            "sourceUrl": None,
        }

    urls = discover_official_status_urls(player)

    result = {
        "checked": False,
        "evidence": "unknown",
        "injured": False,
        "injuryDiagnosis": None,
        "injuryExpectedAbsence": None,
        "suspended": False,
        "sourceUrl": None,
    }

    if not urls:
        print(
            f"STATUS {player_name}: keine offiziellen Status-URLs "
            f"für {club} gefunden."
        )
        return result

    for debug_url in urls[:5]:
        print(f"STATUS-URL {player_name}: {debug_url}")

    # Nur eine begrenzte Zahl Seiten laden, damit 17/18 Spieler
    # den Workflow nicht unnötig verlangsamen.
    pages_checked = 0

    for url in urls:
        if pages_checked >= STATUS_MAX_PAGES_PER_PLAYER:
            break

        try:
            html = http_get_text(url, timeout=STATUS_PAGE_TIMEOUT)
            page_text = _html_to_visible_text(html)
            pages_checked += 1
            result["checked"] = True
        except Exception:
            continue

        windows = _player_text_windows(
            page_text,
            player_name,
            radius=420,
        )

        if not windows:
            continue

        for window in windows:
            lower = window.lower()

            diagnosis = _diagnosis_from_text(window)
            absence = _absence_from_text(window)

            injured = (
                diagnosis is not None
                or any(term in lower for term in CURRENT_INJURY_TERMS)
            )

            suspended = any(
                term in lower
                for term in CURRENT_SUSPENSION_TERMS
            )

            recovered = _window_has_recovery_for_player(window)

            # Verletzung ist die stärkste Evidenz, wenn Diagnose/
            # Ausfallbegriff unmittelbar beim Spielernamen steht.
            if injured and not recovered:
                result.update({
                    "evidence": "injured",
                    "injured": True,
                    "injuryDiagnosis": diagnosis,
                    "injuryExpectedAbsence": absence,
                    "sourceUrl": url,
                })

                print(
                    f"STATUS {player_name}: verletzt=True | "
                    f"Diagnose={diagnosis or '-'} | "
                    f"Ausfall={absence or '-'} | "
                    f"Quelle={url}"
                )
                return result

            if suspended:
                result.update({
                    "evidence": "suspended",
                    "suspended": True,
                    "sourceUrl": url,
                })

                print(
                    f"STATUS {player_name}: gesperrt=True | Quelle={url}"
                )
                return result

            if recovered:
                result.update({
                    "evidence": "recovered",
                    "sourceUrl": url,
                })

                print(
                    f"STATUS {player_name}: Rückkehr/Fit bestätigt | "
                    f"Quelle={url}"
                )
                return result

    print(
        f"STATUS {player_name}: keine eindeutige Statusmeldung "
        f"({pages_checked} offizielle Seiten geprüft)."
    )

    return result


def extract_player_profile_intelligence(player):
    """
    Recherchiert einen einzelnen Spieler direkt über sein
    öffentliches Bundesliga.com-Spielerprofil.

    Keine externe Fußball-API.
    Die Funktion liefert nur Werte, die auf dem öffentlichen
    Profil tatsächlich gefunden wurden.
    """
    source_url = player.get("sourceUrl")

    if not source_url:
        return {
            "available": False,
            "sourceUrl": None,
            "appearances": None,
            "goals": None,
            "assists": None,
            "goalsAgainst": None,
            "yellowCards": None,
            "form": "Noch nicht recherchiert",
            "starting": "Noch nicht recherchiert",
            "injury": "Noch nicht recherchiert",
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "injurySourceUrl": None,
            "injuryEvidence": "unknown",
            "suspension": "Noch nicht recherchiert",
            "suspensionEvidence": "unknown",
            "lastMatch": None,
        }

    try:
        html = http_get_text(source_url, timeout=20)

        # HTML grob in sichtbaren Text umwandeln.
        text_only = re.sub(r"<script\b[^>]*>.*?</script>", " ", html,
                           flags=re.IGNORECASE | re.DOTALL)
        text_only = re.sub(r"<style\b[^>]*>.*?</style>", " ", text_only,
                           flags=re.IGNORECASE | re.DOTALL)
        text_only = re.sub(r"<[^>]+>", " ", text_only)
        text_only = re.sub(r"\s+", " ", text_only).strip()

        appearances = parse_number_after_label(text_only, "Einsätze")
        goals = parse_number_after_label(text_only, "Tore")
        assists = parse_number_after_label(text_only, "Vorlagen")
        yellow_cards = parse_number_after_label(text_only, "Gelbe Karten")

        # V18: zusätzliche öffentliche Leistungswerte. Nur übernehmen,
        # wenn das Spielerprofil den Wert tatsächlich ausweist.
        starts = parse_float_after_labels(text_only, ("Startelf", "Startelfeinsätze"))
        minutes = parse_float_after_labels(text_only, ("Spielminuten", "Minuten"))
        shots = parse_float_after_labels(text_only, ("Torschüsse", "Schüsse"))
        shots_on_target = parse_float_after_labels(
            text_only, ("Torschüsse aufs Tor", "Schüsse aufs Tor")
        )
        woodwork = parse_float_after_labels(text_only, ("Aluminiumtreffer", "Pfostentreffer"))
        penalties = parse_float_after_labels(text_only, ("Elfmeter",))
        penalties_scored = parse_float_after_labels(
            text_only, ("Verwandelte Elfmeter", "Elfmetertore")
        )
        pass_accuracy = parse_float_after_labels(
            text_only, ("Passquote", "Passgenauigkeit")
        )
        duels_won = parse_float_after_labels(
            text_only, ("Gewonnene Zweikämpfe", "Zweikämpfe gewonnen")
        )
        aerial_duels_won = parse_float_after_labels(
            text_only, ("Gewonnene Kopfballduelle", "Kopfballduelle gewonnen")
        )
        crosses = parse_float_after_labels(text_only, ("Flanken",))
        fouls = parse_float_after_labels(text_only, ("Fouls",))
        red_cards = parse_float_after_labels(text_only, ("Rote Karten",))
        saves = parse_float_after_labels(
            text_only, ("Gehaltene Torschüsse", "Paraden")
        )
        clean_sheets = parse_float_after_labels(
            text_only, ("Weiße Weste", "Zu null", "Clean Sheets")
        )

        # V48: Gegentore NICHT aus dem generischen Spielerprofil übernehmen.
        # Ein generischer Profilwert hatte für Keeper fälschlich 0 geliefert.
        # Gegentore werden nur aus abgeschlossenen Teamspielen rekonstruiert,
        # wenn die komplette Keeper-Abdeckung für alle bisherigen Spiele
        # zweifelsfrei belegt ist.
        goals_against = None

        # Letztes Spiel nur als Kontext; keine Startelf daraus ableiten.
        last_match = None
        match = re.search(
            r"Letztes Spiel\s+(.{0,180}?)(?:News|Mitspieler|Kompletter Kader|$)",
            text_only,
            flags=re.IGNORECASE,
        )
        if match:
            last_match = re.sub(r"\s+", " ", match.group(1)).strip()

        # Form wird transparent als aktuelle Saisonstatistik
        # dargestellt und nicht als erfundener Rating-Wert.
        form_parts = []
        if appearances is not None:
            form_parts.append(f"{appearances} Einsätze")

        if _is_goalkeeper(player):
            if goals_against is not None:
                form_parts.append(f"{goals_against} Gegentore")
        else:
            if goals is not None:
                form_parts.append(f"{goals} Tore")
            if assists is not None:
                form_parts.append(f"{assists} Vorlagen")

        form = " · ".join(form_parts) if form_parts else "Keine Saisonstatistik gefunden"

        # Bundesliga.com-Spielerprofile liefern nicht zuverlässig
        # eine Startelfquote. Deshalb keine künstliche Prozentzahl.
        starting = "Öffentlich nicht verfügbar"

        # --------------------------------------------------------
        # AKTUELLER VERLETZUNGS-/SPERRSTATUS
        # --------------------------------------------------------
        # Historische News und allgemeine Erwähnungen von Verletzungen
        # dürfen keinen aktuellen Status erzeugen. Deshalb akzeptieren
        # wir nur eindeutig aktuelle Formulierungen.

        current_profile_text = re.split(
            r"\bLetztes Spiel\b",
            text_only,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        lower = current_profile_text.lower()

        injury_patterns = (
            r"\baktuell\s+verletzt\b",
            r"\bderzeit\s+verletzt\b",
            r"\baktuell\s+angeschlagen\b",
            r"\bderzeit\s+angeschlagen\b",
            r"\baktuell\s+krank\b",
            r"\bderzeit\s+krank\b",
            r"\baktuell\s+erkrankt\b",
            r"\bderzeit\s+erkrankt\b",
        )

        suspension_patterns = (
            r"\baktuell\s+gesperrt\b",
            r"\bderzeit\s+gesperrt\b",
            r"\baktuelle\s+sperre\b",
            r"\bderzeitige\s+sperre\b",
        )

        injury_hit = next(
            (pattern for pattern in injury_patterns if re.search(pattern, lower)),
            None,
        )
        suspension_hit = next(
            (pattern for pattern in suspension_patterns if re.search(pattern, lower)),
            None,
        )

        # V14: zuerst die ligaweite offizielle Spieltagübersicht.
        # Das ist ein einziger HTTP-Abruf für alle Kaderspieler.
        official_status = get_matchday_status_for_player(player)

        official_evidence = official_status.get("evidence", "unknown")
        starting = official_status.get(
            "lineupProbability",
            "Öffentlich nicht verfügbar",
        )
        form = _derive_form_from_stats(player)
        yellow_cards = _normalize_yellow_cards(player)

        official_injured = bool(official_status.get("injured"))
        official_suspended = bool(official_status.get("suspended"))

        injured = bool(injury_hit) or official_injured
        suspended = bool(suspension_hit) or official_suspended

        injury_diagnosis = official_status.get("injuryDiagnosis")
        injury_expected_absence = official_status.get("injuryExpectedAbsence")
        injury_source_url = official_status.get("sourceUrl")

        if injured:
            injury = _format_injury(
                True,
                injury_diagnosis,
                injury_expected_absence,
            )
            injury_evidence = "injured"
        elif official_evidence in {"recovered", "fit"}:
            injury = "Fit"
            injury_evidence = official_evidence
        else:
            # Wichtig: "nichts gefunden" ist nicht automatisch "Fit".
            injury = "Status nicht eindeutig verfügbar"
            injury_evidence = "unknown"

        if suspended:
            suspension = "Gesperrt"
            suspension_evidence = "suspended"
        elif official_status.get("checked"):
            suspension = "Keine Sperre"
            suspension_evidence = "checked_no_suspension"
        else:
            suspension = "Noch nicht recherchiert"
            suspension_evidence = "unknown"

        return {
            "available": True,
            "sourceUrl": source_url,
            "appearances": appearances,
            "starts": starts,
            "minutes": minutes,
            "goals": goals,
            "assists": assists,
            "shots": shots,
            "shotsOnTarget": shots_on_target,
            "woodwork": woodwork,
            "penalties": penalties,
            "penaltiesScored": penalties_scored,
            "passAccuracy": pass_accuracy,
            "duelsWon": duels_won,
            "aerialDuelsWon": aerial_duels_won,
            "crosses": crosses,
            "fouls": fouls,
            "yellowCards": yellow_cards,
            "redCards": red_cards,
            "saves": saves,
            "cleanSheets": clean_sheets,
            "goalsAgainst": goals_against,
            "form": form,
            "starting": starting,
            "injury": injury,
            "injuryDiagnosis": injury_diagnosis,
            "injuryExpectedAbsence": injury_expected_absence,
            "injurySourceUrl": injury_source_url,
            "injuryEvidence": injury_evidence,
            "suspension": suspension,
            "suspensionEvidence": suspension_evidence,
            "yellowCards": yellow_cards,
            "lastMatch": last_match,
        }

    except Exception as exc:
        print(
            f"Bundesliga.com-Spielerprofil fehlgeschlagen "
            f"({player.get('name', 'unbekannt')}): {exc}"
        )

        return {
            "available": False,
            "sourceUrl": source_url,
            "appearances": None,
            "goals": None,
            "assists": None,
            "goalsAgainst": None,
            "yellowCards": None,
            "form": "Noch nicht recherchiert",
            "starting": "Noch nicht recherchiert",
            "injury": "Noch nicht recherchiert",
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "injurySourceUrl": None,
            "injuryEvidence": "unknown",
            "suspension": "Noch nicht recherchiert",
            "suspensionEvidence": "unknown",
            "lastMatch": None,
        }


def load_active_roster_ids():
    """
    Liest den persönlichen Kader aus kickbase-roster.json.

    Die Workflow-Eingaben sind bei unserem aktuellen Kader-Input
    kurze Kickbase-Namens-Tokens (z.B. "friedrich") und keine
    Bundesliga.com-Spieler-Slugs. Die Tokens werden deshalb später
    gegen Bundesliga.com-ID, vollständigen Namen und eindeutige
    Nachnamen abgeglichen.
    """
    if not ACTIVE_ROSTER_FILE.exists():
        print("Kein kickbase-roster.json vorhanden.")
        print("Player-Intelligence-Recherche für persönlichen Kader: 0 Spieler.")
        return []

    try:
        raw = json.loads(ACTIVE_ROSTER_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"kickbase-roster.json konnte nicht gelesen werden: {exc}")
        return []

    players = raw.get("players", []) if isinstance(raw, dict) else raw
    if not isinstance(players, list):
        print("kickbase-roster.json hat kein gültiges players-Array.")
        return []

    tokens = []
    seen = set()

    for item in players:
        if isinstance(item, str):
            token = item.strip()
        elif isinstance(item, dict):
            token = str(
                item.get("id")
                or item.get("name")
                or item.get("player")
                or ""
            ).strip()
        else:
            continue

        if token:
            normalized = normalize_name(token)
            if normalized and normalized not in seen:
                seen.add(normalized)
                tokens.append(token)

    if len(tokens) > 18:
        raise RuntimeError(
            f"Der persönliche Kader enthält {len(tokens)} Spieler. "
            "Maximal 18 Spieler erlaubt."
        )

    print(
        f"Aktueller persönlicher Kader für Player Intelligence: "
        f"{len(tokens)}/18 Spieler (kickbase-roster.json)."
    )
    print("Kader-Tokens: " + ", ".join(tokens))
    return tokens


def resolve_active_player_ids(bundesliga_squads, roster_tokens):
    """
    Löst die Kader-Tokens robust gegen Bundesliga.com-Spieler auf.

    Match-Reihenfolge:
    1. exakte Bundesliga.com-ID
    2. exakter voller Name
    3. eindeutiger Nachname
    4. eindeutiger Namensbestandteil / zusammengesetzter Name

    Dabei werden Umlaute, Akzente, Bindestriche und Groß-/Kleinschreibung
    normalisiert. API-Football wird hierfür nicht verwendet.
    """
    def norm(value):
        value = str(value or "").strip().lower()
        value = unicodedata.normalize("NFKD", value)
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        value = value.replace("ß", "ss")
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return " ".join(value.split())

    all_players = []
    for squad in bundesliga_squads.values():
        all_players.extend(squad)

    roster_norms = {norm(token) for token in roster_tokens if norm(token)}

    # Bekannte Kickbase-Kurz-IDs, deren Schreibweise nicht 1:1 dem
    # Bundesliga.com-Namen entspricht. Diese Zuordnung ist bewusst eng
    # begrenzt und dient nur der eindeutigen Auflösung des persönlichen
    # Kaders; es wird keine externe Fußball-API benötigt.
    explicit_aliases = {
        "friedrich": ["marvin friedrich"],
        "koemuer": ["mert koemuer", "mert kömür"],
        "hoeler": ["lucas hoeler", "lucas höler"],
    }

    # Alle möglichen eindeutigen Namensbestandteile vorbereiten.
    surname_counts = {}
    token_counts = {}

    player_meta = []
    for player in all_players:
        player_id = str(player.get("id", "")).strip()
        player_name = str(player.get("name", "")).strip()
        name_norm = norm(player_name)
        parts = name_norm.split()

        surname = parts[-1] if parts else ""
        if surname:
            surname_counts[surname] = surname_counts.get(surname, 0) + 1

        for part in set(parts):
            if len(part) >= 4:
                token_counts[part] = token_counts.get(part, 0) + 1

        player_meta.append((player, player_id, name_norm, parts, surname))

    active_ids = set()
    matches = []
    unresolved = []

    for roster_token in roster_tokens:
        token_norm = norm(roster_token)
        if not token_norm:
            unresolved.append(roster_token)
            continue

        candidates = []

        # 1) Exakte ID / voller Name
        for player, player_id, name_norm, parts, surname in player_meta:
            if token_norm == norm(player_id) or token_norm == name_norm:
                candidates.append(player)

        # 1b) Explizite, eindeutige Kickbase-Kurz-ID.
        # Nur verwenden, wenn genau ein Bundesliga.com-Spieler passt.
        if not candidates and token_norm in explicit_aliases:
            alias_names = {norm(name) for name in explicit_aliases[token_norm]}
            alias_candidates = [
                player
                for player, player_id, name_norm, parts, surname
                in player_meta
                if name_norm in alias_names
            ]
            if len(alias_candidates) == 1:
                candidates = alias_candidates

        # 2) Eindeutiger Nachname
        if not candidates and surname_counts.get(token_norm, 0) == 1:
            candidates = [
                player for player, player_id, name_norm, parts, surname
                in player_meta
                if surname == token_norm
            ]

        # 3) Eindeutiger Namensbestandteil
        if not candidates and len(token_norm) >= 4:
            candidates = [
                player for player, player_id, name_norm, parts, surname
                in player_meta
                if token_norm in parts and token_counts.get(token_norm, 0) == 1
            ]

        # 4) Zusammengesetzte/leicht abweichende Schreibweise:
        #    Token muss vollständig in genau einem Namen vorkommen.
        if not candidates and len(token_norm) >= 5:
            fuzzy = [
                player for player, player_id, name_norm, parts, surname
                in player_meta
                if token_norm in name_norm
            ]
            if len(fuzzy) == 1:
                candidates = fuzzy

        if len(candidates) == 1:
            player = candidates[0]
            player_id = str(player.get("id", "")).strip()
            if player_id:
                active_ids.add(player_id)
                matches.append(
                    f"{roster_token} -> "
                    f"{player.get('name', 'Unbekannt')} [{player_id}]"
                )
        else:
            unresolved.append(roster_token)

    print(
        "Aktive Spieler für intensive Recherche: "
        f"{len(active_ids)}/{len(roster_tokens)} Kader-Tokens zugeordnet."
    )

    if matches:
        print("Zugeordnete Spieler: " + "; ".join(matches))

    if unresolved:
        print("Nicht eindeutig zugeordnet: " + ", ".join(unresolved))

    return active_ids


def _extract_roster_ids(players):
    if not isinstance(players, list):
        return []

    ids = []
    seen = set()

    for item in players:
        if isinstance(item, str):
            player_id = item.strip()
        elif isinstance(item, dict):
            player_id = str(item.get("id", "")).strip()
        else:
            continue

        if player_id and player_id not in seen:
            seen.add(player_id)
            ids.append(player_id)

    if len(ids) > 18:
        raise RuntimeError(
            f"Der persönliche Kader enthält {len(ids)} Spieler. "
            "Maximal 18 Spieler erlaubt."
        )

    return ids


def build_player_intelligence(
    player,
    club_name,
    next_match,
    old_player=None,
    public_source=None,
    research_player=False,
    matches=None,
):
    """
    Baut einen Spieler-Eintrag aus vorhandenen Daten und optionaler
    Einzelspieler-Recherche.

    V10-Änderung:
    - Standardmäßig KEIN Abruf des einzelnen Spielerprofils.
    - Nur IDs aus kickbase-roster.json werden intensiv recherchiert.
    - Vorhandene Intelligence bleibt für alle anderen Spieler erhalten.
    - Keine Kickbase-Daten werden erfunden.
    """
    player_id = player.get("id")
    name = player.get("name", "Unbekannter Spieler")
    old_player = old_player or {}
    public_source = public_source or {}

    if next_match:
        opponent = next_match.get("opponent")
        home_away = next_match.get("homeAway")
    else:
        opponent = None
        home_away = None

    if research_player:
        research_input = dict(player)
        research_input["club"] = club_name
        research_input["_opponent"] = (
            next_match.get("opponent")
            if isinstance(next_match, dict)
            else None
        )
        research_input["_matchday"] = _matchday_from_next_match(next_match)
        public_player = extract_player_profile_intelligence(research_input)

        if _is_goalkeeper(research_input):
            _v48_ga = v48_resolve_goalkeeper_goals_against(
                research_input,
                club_name,
                matches,
                public_player,
            )
            public_player["goalsAgainst"] = _v48_ga.get("value")
            public_player["goalsAgainstSource"] = _v48_ga.get("source")
            public_player["goalsAgainstEvidence"] = _v48_ga.get("evidence")
            print(
                f"V49-GK-GA {name}: "
                f"value={_v48_ga.get('value')} | "
                f"status={_v48_ga.get('status')} | "
                f"source={_v48_ga.get('source')} | "
                f"evidence={_v48_ga.get('evidence')}"
            )

        print(
            f"PLAYER-STATUS {name} [{player_id}]: "
            f"injury={public_player.get('injury')} | "
            f"injuryEvidence={public_player.get('injuryEvidence')} | "
            f"suspension={public_player.get('suspension')} | "
            f"suspensionEvidence={public_player.get('suspensionEvidence')} | "
            f"injurySourceUrl={public_player.get('injurySourceUrl')}"
        )

        average = old_player.get("average")
        starting = public_player.get("starting", old_player.get("starting", "Noch nicht recherchiert"))
        form = public_player.get("form", old_player.get("form", "Noch nicht recherchiert"))
        injury = public_player.get("injury", old_player.get("injury", "Noch nicht recherchiert"))
        injury_diagnosis = public_player.get(
            "injuryDiagnosis",
            old_player.get("injuryDiagnosis"),
        )
        injury_expected_absence = public_player.get(
            "injuryExpectedAbsence",
            old_player.get("injuryExpectedAbsence"),
        )
        injury_source_url = public_player.get(
            "injurySourceUrl",
            old_player.get("injurySourceUrl"),
        )
        injury_evidence = public_player.get("injuryEvidence", "unknown")

        suspension = public_player.get(
            "suspension",
            old_player.get("suspension", "Noch nicht recherchiert"),
        )
        suspension_evidence = public_player.get(
            "suspensionEvidence",
            "unknown",
        )

        injury = _normalize_injury_label(injury)
        suspension = _normalize_suspension_label(suspension)

        # --------------------------------------------------------
        # GENERISCHES STATUS-GEDÄCHTNIS
        # --------------------------------------------------------
        # Eine bereits offiziell bestätigte Verletzung darf durch einen
        # späteren erfolglosen Parser-Lauf nicht verschwinden.
        # Sie endet erst bei neuer Verletzungsevidenz oder expliziter
        # Rückkehr-/Fit-Evidenz.
        old_injury = _normalize_injury_label(
            old_player.get("injury")
        )
        old_injury_confirmed = (
            isinstance(old_injury, str)
            and old_injury.startswith("Verletzt")
        )

        if (
            injury_evidence in {"unknown", "budget_exhausted"}
            and old_injury_confirmed
        ):
            injury = old_injury
            injury_diagnosis = old_player.get("injuryDiagnosis")
            injury_expected_absence = old_player.get(
                "injuryExpectedAbsence"
            )
            injury_source_url = old_player.get("injurySourceUrl")
            injury_evidence = old_player.get(
                "injuryEvidence",
                "carried_confirmed_injury",
            )

        if (
            suspension_evidence in {"unknown", "budget_exhausted"}
            and old_player.get("suspension") in {"Gesperrt", "Keine Sperre"}
        ):
            suspension = old_player.get("suspension")
            suspension_evidence = old_player.get(
                "suspensionEvidence",
                "carried_status",
            )

        appearances = public_player.get("appearances", old_player.get("appearances"))
        goals = public_player.get("goals", old_player.get("goals"))
        assists = public_player.get("assists", old_player.get("assists"))
        goals_against = public_player.get("goalsAgainst", old_player.get("goalsAgainst"))
        yellow_cards = public_player.get("yellowCards", old_player.get("yellowCards"))
        last_match = public_player.get("lastMatch", old_player.get("lastMatch"))
        profile_available = public_player.get("available", False)
        profile_url = public_player.get("sourceUrl") or player.get("sourceUrl")
    else:
        # Keine Netzwerkanfrage: vorhandene Daten werden vollständig erhalten.
        average = old_player.get("average")
        starting = old_player.get("starting", "Noch nicht recherchiert")
        form = old_player.get("form", "Noch nicht recherchiert")
        injury = old_player.get("injury", "Noch nicht recherchiert")
        injury_diagnosis = old_player.get("injuryDiagnosis")
        injury_expected_absence = old_player.get("injuryExpectedAbsence")
        injury_source_url = old_player.get("injurySourceUrl")
        injury_evidence = old_player.get("injuryEvidence", "unknown")

        suspension = old_player.get("suspension", "Noch nicht recherchiert")
        suspension_evidence = old_player.get(
            "suspensionEvidence",
            "unknown",
        )

        injury = _normalize_injury_label(injury)
        suspension = _normalize_suspension_label(suspension)

        appearances = old_player.get("appearances")
        goals = old_player.get("goals")
        assists = old_player.get("assists")
        goals_against = old_player.get("goalsAgainst")
        yellow_cards = old_player.get("yellowCards")
        last_match = old_player.get("lastMatch")
        profile_available = bool(old_player.get("dataStatus", {}).get("playerProfileSource") == "erreichbar")
        profile_url = old_player.get("sourceUrl") or player.get("sourceUrl")

    # V19: Performance-Layer.
    # Spielerprofil + ligaweite Bundesliga-Rankings werden zusammengeführt.
    old_performance = old_player.get("performance") or {}
    performance_sources = dict(old_player.get("performanceSources") or {})

    if research_player:
        ranking_values, ranking_sources = collect_bundesliga_rankings_for_player(
            research_input
        )
        (
            historical_prior,
            historical_prior_sources,
            historical_prior_coverage,
        ) = collect_bundesliga_historical_prior(research_input)
        (
            historical_prior,
            historical_prior_sources,
            historical_prior_coverage,
        ) = _v30_goalkeeper_historical_fallback(
            research_input,
            historical_prior,
            historical_prior_sources,
            historical_prior_coverage,
        )

        perf_values = {
            "appearances": public_player.get("appearances"),
            "starts": public_player.get("starts"),
            "minutes": public_player.get("minutes"),
            "goals": public_player.get("goals"),
            "assists": public_player.get("assists"),
            "shots": public_player.get("shots"),
            "shotsOnTarget": public_player.get("shotsOnTarget"),
            "woodwork": public_player.get("woodwork"),
            "penalties": public_player.get("penalties"),
            "penaltiesScored": public_player.get("penaltiesScored"),
            "passAccuracy": public_player.get("passAccuracy"),
            "duelsWon": public_player.get("duelsWon"),
            "aerialDuelsWon": public_player.get("aerialDuelsWon"),
            "crosses": public_player.get("crosses"),
            "fouls": public_player.get("fouls"),
            "yellowCards": public_player.get("yellowCards"),
            "redCards": public_player.get("redCards"),
            "saves": public_player.get("saves"),
            "cleanSheets": public_player.get("cleanSheets"),
            "goalsAgainst": public_player.get("goalsAgainst"),
            "distanceKm": public_player.get("distanceKm"),
            "sprints": public_player.get("sprints"),
            "intensiveRuns": public_player.get("intensiveRuns"),
            "topSpeedKmh": public_player.get("topSpeedKmh"),
        }

        # V32 official Bundesliga player-profile fallback for current GK facts.
        if _v29_position_group(research_input.get("position")) == "TW":
            gk_current, gk_sources, gk_meta = _v32_official_goalkeeper_current_profile(research_input)
            for metric_key, value in gk_current.items():
                if perf_values.get(metric_key) is None:
                    perf_values[metric_key] = value
                    performance_sources[metric_key] = gk_sources.get(metric_key)
            if gk_meta:
                historical_prior_coverage["currentGoalkeeperProfile"] = gk_meta
            historical_prior_coverage["currentGoalkeeperProfileValues"] = dict(gk_current)

        # Ranking-Werte ergänzen/überschreiben nur, wenn sie tatsächlich
        # verfügbar sind. So bleiben Profilwerte für nicht abgedeckte Felder.
        for metric_key, value in ranking_values.items():
            if value is not None:
                perf_values[metric_key] = value

        performance_sources.update(ranking_sources)

    else:
        perf_values = dict(old_performance)
        historical_prior = old_player.get("historicalPrior") or {}
        historical_prior_sources = old_player.get("historicalPriorSources") or {}
        historical_prior_coverage = old_player.get("historicalPriorCoverage") or {
            "season": BUNDESLIGA_PRIOR_SEASON,
            "competition": None,
            "league": None,
            "availableMetrics": [],
            "missingMetrics": list(BUNDESLIGA_PRIOR_STAT_CATEGORIES.keys()),
            "coveragePercent": 0,
        }

    performance, data_coverage = build_performance_layer(
        perf_values,
        old_performance=old_performance,
    )

    # V52: reject stale/prior-season occurrence totals before they can affect
    # either the Kickbase projection or the UI.
    performance, performance_sources, v52_current_fact_evidence = (
        v52_sanitize_current_performance(
            performance,
            performance_sources,
            club_name,
            matches,
        )
    )
    if research_player:
        print(
            f"V52-CURRENT-FACT-GUARD {name}: "
            f"goals={performance.get('goals')} "
            f"[{v52_current_fact_evidence.get('goals')}] | "
            f"yellowCards={performance.get('yellowCards')} "
            f"[{v52_current_fact_evidence.get('yellowCards')}] | "
            f"appearances={performance.get('appearances')} "
            f"[{v52_current_fact_evidence.get('appearances')}]"
        )

    # V56: if V52 rejected/missed current goals, resolve them from the explicit
    # current-season block of the official player profile first, then use the
    # strict current-season goals ranking as fallback. Never infer zero.
    v54_goals_fact = {
        "value": performance.get("goals"),
        "status": "observed" if performance.get("goals") is not None else "unknown",
        "source": performance_sources.get("goals"),
        "evidence": (
            "already_available_after_v52"
            if performance.get("goals") is not None
            else "not_checked"
        ),
    }
    if performance.get("goals") is None and research_player:
        v54_goals_fact = v55_current_season_goals(research_input, club_name, matches)
        if v54_goals_fact.get("status") == "observed":
            performance["goals"] = v54_goals_fact.get("value")
            performance_sources["goals"] = v54_goals_fact.get("source")
    elif performance.get("goals") is None:
        # V55.1: research_input is created only for actively researched players.
        # Non-researched players must not enter the current-profile resolver.
        v54_goals_fact = {
            "value": None,
            "status": "unknown",
            "source": None,
            "evidence": "not_researched_preserve_existing_state",
        }

    if research_player:
        print(
            f"V56-CURRENT-GOALS {name}: "
            f"value={v54_goals_fact.get('value')} | "
            f"status={v54_goals_fact.get('status')} | "
            f"evidence={v54_goals_fact.get('evidence')} | "
            f"source={v54_goals_fact.get('source')}"
        )

    kickbase_factor_coverage = build_kickbase_factor_coverage(performance, player.get("position"))

    # V28: Expected-Points-Engine aktiv. Ausschließlich öffentlich gefundene
    # Performance-Werte + Status/Startelf + historische Lineup-Evidenz.
    if research_player:
        evidence_adj = _v27_match_evidence_adjustment(historical_prior_coverage)
        projection_player = dict(player)
        projection_player.update({
            "starting": starting,
            "injury": injury,
            "suspension": suspension,
            "homeAway": home_away,
        })
        kickbase_ai_projection = build_kickbase_ai_projection(
            projection_player,
            performance,
            data_coverage,
            evidence_adj=evidence_adj,
            historical_prior=historical_prior,
            historical_prior_coverage=historical_prior_coverage,
            official_gk_profile=(
                (historical_prior_coverage or {}).get("currentGoalkeeperProfileValues") or {}
            ),
            kickbase_match_history=v51_player_kickbase_history(
                player_id,
                old_player,
            ),
        )
        evidence_ratio = evidence_adj.get("ratio")
        evidence_pct = (
            f"{round(evidence_ratio * 100)}%"
            if evidence_ratio is not None else "n/a"
        )
        _v51_kb_history = v51_player_kickbase_history(player_id, old_player)
        _v51_form = v51_kickbase_form_label(_v51_kb_history)
        if _v51_form:
            form = _v51_form
        print(
            f"V51-KB-HISTORY {name}: "
            f"matches={len(_v51_kb_history)} | form={_v51_form or 'none'}"
        )
        print(
            f"AI-PROJECTION {name}: V51 Kickbase Expected Points="
            f"{kickbase_ai_projection.get('expectedPoints')} "
            f"[{kickbase_ai_projection.get('rangeMin')}-"
            f"{kickbase_ai_projection.get('rangeMax')}] | "
            f"Position={kickbase_ai_projection.get('positionModel')} | "
            f"Start={kickbase_ai_projection.get('startProbability')}% | "
            f"MinStart={kickbase_ai_projection.get('minutesIfStart')} | "
            f"MinBank={kickbase_ai_projection.get('minutesIfBench')} | "
            f"ØMin={kickbase_ai_projection.get('expectedMinutes')} | "
            f"StarterPts={kickbase_ai_projection.get('scenario', {}).get('starterPoints')} | "
            f"PriorBonus={kickbase_ai_projection.get('historicalPriorStrength', {}).get('bonus')} | "
            f"GKPriorStarts={((kickbase_ai_projection.get('goalkeeperPrior') or {}).get('starts'))} | "
            f"GKPerf={kickbase_ai_projection.get('goalkeeperPriorComponents')} | "
            f"GKCurrentExplicit={((kickbase_ai_projection.get('goalkeeperCurrentSeasonPerformance') or {}).get('explicit'))} | "
            f"GKPriorExplicit={((kickbase_ai_projection.get('goalkeeperSeasonPerformancePrior') or {}).get('explicit'))} | "
            f"GKLiveWeight={((kickbase_ai_projection.get('goalkeeperPerformancePrior') or {}).get('liveSeasonWeight'))} | "
            f"GKLiveSource={((kickbase_ai_projection.get('goalkeeperPerformancePrior') or {}).get('liveSeasonSource'))} | "
            f"GKBlendPrior={((kickbase_ai_projection.get('goalkeeperPerformancePrior') or {}).get('blendPriorSource'))} | "
            f"GKOfficial={((historical_prior_coverage or {}).get('currentGoalkeeperProfile') or {}).get('metrics')} | "
            f"Confidence={kickbase_ai_projection.get('confidence')}% | "
            f"Evidence={evidence_pct} "
            f"({evidence_adj.get('matchesFound', 0)}/{evidence_adj.get('sample', 0)}) "
            f"{evidence_adj.get('roleSignal')} x"
            f"{evidence_adj.get('projectionMultiplier'):.2f}"
        )
    else:
        kickbase_ai_projection = old_player.get("kickbaseAiProjection")
        _v51_kb_history = v51_player_kickbase_history(player_id, old_player)
        _v51_form = v51_kickbase_form_label(_v51_kb_history)
        if _v51_form:
            form = _v51_form

    if research_player:
        print(
            f"PERFORMANCE {name}: Current {data_coverage['coveragePercent']}% | "
            f"vorhanden={len(data_coverage['availableMetrics'])} | "
            f"fehlend={len(data_coverage['missingMetrics'])} | "
            f"Prior {historical_prior_coverage.get('coveragePercent', 0)}%"
        )
        print(
            f"EVENT-COVERAGE {name}: "
            f"Position={kickbase_factor_coverage.get('positionModel')} | "
            f"Readiness={kickbase_factor_coverage.get('scoringReadinessPercent')}% | "
            f"Reliability={kickbase_factor_coverage.get('reliabilityBand')} | "
            f"kritisch vorhanden={len(kickbase_factor_coverage.get('criticalAvailable', []))}/"
            f"{len(kickbase_factor_coverage.get('criticalFactors', []))} | "
            f"fehlt={','.join(kickbase_factor_coverage.get('criticalMissing', [])) or '-'}"
        )
        # V44: build a transparent historical-open-data prior for critical
        # metrics that are missing from the current observed data.
        # V44.1 wiring fix: use the exact same source that EVENT-COVERAGE
        # prints above. This prevents the calibration layer from silently
        # seeing Missing=none while EVENT-COVERAGE reports criticalMissing.
        _v44_missing = list(kickbase_factor_coverage.get("criticalMissing", []) or [])
        _v44_pos = (
            kickbase_factor_coverage.get("positionModel")
            or kickbase_ai_projection.get("positionModel")
        )
        v44_apply_missing_event_calibration(
            kickbase_ai_projection, _v44_pos, _v44_missing
        )
        _v44_cal = kickbase_ai_projection.get("openDataCalibration") or {}
        print(
            f"V44-CALIBRATION {name}: "
            f"Position={_v44_cal.get('position')} | "
            f"Missing={','.join(_v44_missing) if _v44_missing else 'none'} | "
            f"Estimated={_v44_cal.get('estimates')} | "
            f"RawPts={_v44_cal.get('rawEstimatedPoints')} | "
            f"CappedPts={_v44_cal.get('cappedEstimatedPoints')} | "
            f"Applied=False"
        )
        print(
            f"POSITION-SCORING {name}: "
            f"Position={kickbase_ai_projection.get('positionModel')} | "
            f"Goal={((kickbase_ai_projection.get('positionScoring') or {}).get('goal'))} | "
            f"Assist={((kickbase_ai_projection.get('positionScoring') or {}).get('assist'))} | "
            f"CS/10={((kickbase_ai_projection.get('positionScoring') or {}).get('cleanSheetPer10'))} | "
            f"Startelf={((kickbase_ai_projection.get('positionScoring') or {}).get('startingXI'))} | "
            f"Min/10={((kickbase_ai_projection.get('positionScoring') or {}).get('minutePer10'))}"
        )
        print(
            f"POINT-COMPONENTS {name}: "
            f"{kickbase_ai_projection.get('components')}"
        )
        print(
            f"UI-BRIDGE {name}: "
            f"Points={kickbase_ai_projection.get('expectedPoints')} | "
            f"Start={kickbase_ai_projection.get('startProbability')}% | "
            f"Recommendation={kickbase_ai_projection.get('recommendation')}"
        )
        print(
            f"V45-AI-DISPLAY {name}: canonical UI payload wird erzeugt | "
            f"legacy bridge bleibt kompatibel"
        )

    # Dynamische Spieltagsdaten immer neu berechnen.
    old_recommendation = old_player.get("recommendation")
    if old_recommendation and old_recommendation != "Noch nicht recherchiert":
        recommendation = old_recommendation
    elif next_match:
        recommendation = "Nächstes Spiel vorhanden"
    else:
        recommendation = "Noch nicht ausreichend Daten"

    # V44.2 UI bridge:
    # The current frontend still reads legacy fields such as `average`,
    # `starting` and `recommendation`. The projection already exists under
    # kickbaseAiProjection, so expose conservative fallbacks there until the
    # frontend is migrated to the richer object.
    display_expected_points = (
        kickbase_ai_projection.get("expectedPoints")
        if isinstance(kickbase_ai_projection, dict)
        else None
    )
    display_start_probability = (
        kickbase_ai_projection.get("startProbability")
        if isinstance(kickbase_ai_projection, dict)
        else None
    )

    display_average = average
    if display_average is None and display_expected_points is not None:
        display_average = display_expected_points

    display_starting = starting
    if (
        isinstance(display_start_probability, (int, float))
        and (
            not display_starting
            or str(display_starting).strip().lower()
            in {"noch nicht recherchiert", "öffentlich nicht verfügbar"}
        )
    ):
        if display_start_probability >= 90:
            display_starting = "Sehr wahrscheinlich"
        elif display_start_probability >= 75:
            display_starting = "Wahrscheinlich"
        elif display_start_probability >= 50:
            display_starting = "Offen / leicht positiv"
        elif display_start_probability >= 30:
            display_starting = "Eher Bank / offen"
        else:
            display_starting = "Eher nicht"

    print(
        f"V45.2-SCOPE {name}: "
        f"ProfileStarting={starting} | DisplayStarting={display_starting} | "
        f"StartProbability={display_start_probability} | "
        f"StartDisplay={display_starting} | "
        f"Injury={injury} | Suspension={suspension}"
    )

    # V52: UI occurrence fields must use the sanitized CURRENT_SEASON
    # performance values, never an unsanitized profile/old-player carry-over.
    goals = performance.get("goals")
    yellow_cards = performance.get("yellowCards")

    # V45.3 UI semantics
    # Startelf: show the actual probability instead of a qualitative label.
    if isinstance(display_start_probability, (int, float)):
        display_starting = f"{int(round(display_start_probability))}%"

    # V46: canonical observed facts. One resolver for every player/position.
    _profile_facts = {
        "goals": goals,
        "goalsAgainst": goals_against,
        "yellowCards": yellow_cards,
        "sourceUrl": profile_url if profile_available else None,
    }
    if _is_goalkeeper(player):
        # V48: generic profile URL is not valid provenance for keeper GA.
        _profile_facts["goalsAgainst"] = None
    actual_facts = v46_build_actual_facts(
        performance,
        performance_sources,
        _profile_facts,
    )

    if _is_goalkeeper(player) and research_player:
        _v48_fact = v48_resolve_goalkeeper_goals_against(
            player,
            club_name,
            matches,
            public_player,
        )
        if _v48_fact.get("status") == "observed":
            actual_facts["goalsAgainst"] = {
                "value": _v48_fact.get("value"),
                "status": "observed",
                "source": _v48_fact.get("source"),
                "origin": "openligadb_completed_matches",
                "evidence": _v48_fact.get("evidence"),
            }
        else:
            actual_facts["goalsAgainst"] = {
                "value": None,
                "status": "unknown",
                "source": None,
                "origin": None,
                "evidence": _v48_fact.get("evidence"),
            }

    display_goals = actual_facts["goals"]["value"]
    display_goals_against = actual_facts["goalsAgainst"]["value"]
    display_yellow_cards = actual_facts["yellowCards"]["value"]

    display_injury = injury if injury not in (None, "", "Noch nicht recherchiert") else None


    print(
        f"V46-ACTUAL-FACTS {name}: "
        f"Goals={actual_facts['goals']} | "
        f"GA={actual_facts['goalsAgainst']} | "
        f"YC={actual_facts['yellowCards']}"
    )

    print(
        f"V45.4-UI-STATS {name}: "
        f"StartDisplay={display_starting} | "
        f"Goals={display_goals} | "
        f"GoalsAgainst={display_goals_against}"
    )

    projection_recommendation = (
        kickbase_ai_projection.get("recommendation")
        if isinstance(kickbase_ai_projection, dict)
        else None
    )
    display_recommendation = (
        projection_recommendation
        if projection_recommendation
        else recommendation
    )

    # V45: canonical UI payload. The frontend should render this object instead
    # of interpreting legacy fields such as `average`. Legacy fields remain for
    # backwards compatibility, but all AI semantics live here explicitly.
    _proj = kickbase_ai_projection if isinstance(kickbase_ai_projection, dict) else {}
    _range_min = _proj.get("rangeMin")
    _range_max = _proj.get("rangeMax")
    _expected_minutes = _proj.get("expectedMinutes")
    _confidence = _proj.get("confidence")
    _readiness = (kickbase_factor_coverage or {}).get("scoringReadinessPercent")
    _reliability = (kickbase_factor_coverage or {}).get("reliabilityBand")
    _components = dict(_proj.get("components") or {})
    _position_scoring = dict(_proj.get("positionScoring") or {})

    if isinstance(display_start_probability, (int, float)):
        _start_label = f"{int(round(display_start_probability))}%"
    else:
        _start_label = "Noch nicht recherchiert"

    if isinstance(display_expected_points, (int, float)):
        _points_label = f"{int(round(display_expected_points))} Punkte"
    else:
        _points_label = "Noch nicht recherchiert"

    if isinstance(_range_min, (int, float)) and isinstance(_range_max, (int, float)):
        _range_label = f"{int(round(_range_min))}–{int(round(_range_max))} Punkte"
    else:
        _range_label = "Noch nicht recherchiert"

    if isinstance(_expected_minutes, (int, float)):
        _minutes_label = f"{int(round(_expected_minutes))} Min."
    else:
        _minutes_label = "Noch nicht recherchiert"

    if isinstance(_confidence, (int, float)):
        _confidence_label = f"{int(round(_confidence))}%"
    else:
        _confidence_label = "Noch nicht recherchiert"

    v45_player_intelligence = {
        "schemaVersion": 51,
        "headline": {
            "projectedPoints": display_expected_points,
            "projectedPointsLabel": _points_label,
            "rangeMin": _range_min,
            "rangeMax": _range_max,
            "rangeLabel": _range_label,
            "recommendation": display_recommendation,
        },
        "availability": {
            "startProbability": display_start_probability,
            "startProbabilityLabel": (
                f"{int(round(display_start_probability))}%"
                if isinstance(display_start_probability, (int, float))
                else "Noch nicht recherchiert"
            ),
            "startingAssessment": display_starting,
            "expectedMinutes": _expected_minutes,
            "expectedMinutesLabel": _minutes_label,
            "injury": injury,
            "suspension": suspension,
        },
        "quality": {
            "confidence": _confidence,
            "confidenceLabel": _confidence_label,
            "scoringReadinessPercent": _readiness,
            "reliabilityBand": _reliability,
            "currentDataCoveragePercent": (data_coverage or {}).get("coveragePercent"),
            "historicalPriorCoveragePercent": (historical_prior_coverage or {}).get("coveragePercent"),
        },
        "context": {
            "opponent": opponent,
            "homeAway": home_away,
            "positionModel": _proj.get("positionModel"),
        },
        "stats": {
            "goals": display_goals,
            "goalsStatus": actual_facts["goals"]["status"],
            "goalsAgainst": display_goals_against,
            "goalsAgainstStatus": actual_facts["goalsAgainst"]["status"],
            "yellowCards": display_yellow_cards,
            "yellowCardsStatus": actual_facts["yellowCards"]["status"],
        },
        "pointDrivers": _components,
        "positionScoring": _position_scoring,
        "scenario": dict(_proj.get("scenario") or {}),
        "source": "kickbaseAiProjection",
    }

    return {
        "id": player_id,
        "name": name,
        "club": club_name,
        "position": player.get("position"),
        "number": player.get("number"),
        "sourceUrl": player.get("sourceUrl"),
        "average": display_average,
        "starting": display_starting,
        "form": form,
        "footballRating": (
            kickbase_ai_projection.get("expectedPoints")
            if kickbase_ai_projection
            else old_player.get("footballRating")
        ),
        "kickbaseAiScore": (
            kickbase_ai_projection.get("expectedPoints")
            if kickbase_ai_projection
            else None
        ),
        "kickbaseAiProjection": kickbase_ai_projection,
        "kickbaseMatchHistory": _v51_kb_history,
        "playerIntelligenceV45": v45_player_intelligence,
        "actualFacts": actual_facts,
        "playerIntelligenceUiRows": {
            "projectedPoints": display_expected_points,
            "startProbability": display_start_probability,
            "goals": display_goals,
            "goalsAgainst": display_goals_against,
            "opponent": opponent,
            "homeAway": home_away,
            "injury": display_injury,
            "suspension": suspension,
            "yellowCards": display_yellow_cards,
            "recommendation": display_recommendation,
        },
        "displayIntelligence": {
            "schemaVersion": 51,
            "projectedPoints": display_expected_points,
            "projectedPointsLabel": _points_label,
            "rangeLabel": _range_label,
            "startProbabilityLabel": _start_label,
            "expectedMinutesLabel": _minutes_label,
            "confidenceLabel": _confidence_label,
            "startingAssessment": display_starting,
            "goals": display_goals,
            "goalsAgainst": display_goals_against,
            "yellowCards": display_yellow_cards,
            "injury": display_injury,
            "suspension": suspension,
            "opponent": opponent,
            "homeAway": home_away,
            "pointDrivers": _components,
            "positionScoring": _position_scoring,
            "projectedPoints": display_expected_points,
            "rangeMin": (
                kickbase_ai_projection.get("rangeMin")
                if isinstance(kickbase_ai_projection, dict) else None
            ),
            "rangeMax": (
                kickbase_ai_projection.get("rangeMax")
                if isinstance(kickbase_ai_projection, dict) else None
            ),
            "confidence": (
                kickbase_ai_projection.get("confidence")
                if isinstance(kickbase_ai_projection, dict) else None
            ),
            "startProbability": display_start_probability,
            "expectedMinutes": (
                kickbase_ai_projection.get("expectedMinutes")
                if isinstance(kickbase_ai_projection, dict) else None
            ),
            "recommendation": display_recommendation,
            "scoringReadinessPercent": (
                kickbase_factor_coverage.get("scoringReadinessPercent")
                if isinstance(kickbase_factor_coverage, dict) else None
            ),
            "reliabilityBand": (
                kickbase_factor_coverage.get("reliabilityBand")
                if isinstance(kickbase_factor_coverage, dict) else None
            ),
            "source": "kickbaseAiProjection",
        },
        "performance": performance,
        "performanceSources": performance_sources,
        "currentSeasonFactGuard": v52_current_fact_evidence,
        "currentSeasonGoalsFact": v54_goals_fact,
        "dataCoverage": data_coverage,
        "historicalPrior": historical_prior,
        "historicalPriorSources": historical_prior_sources,
        "historicalPriorCoverage": historical_prior_coverage,
        "kickbaseFactorCoverage": kickbase_factor_coverage,
        "appearances": appearances,
        "starts": old_player.get("starts"),
        "minutes": old_player.get("minutes"),
        "goals": display_goals,
        "assists": assists,
        "goalsAgainst": goals_against,
        "yellowCards": display_yellow_cards,
        "lastMatch": last_match,
        "opponent": opponent,
        "homeAway": home_away,
        "injury": display_injury,
        "injuryDiagnosis": injury_diagnosis,
        "injuryExpectedAbsence": injury_expected_absence,
        "injurySourceUrl": injury_source_url,
        "injuryEvidence": injury_evidence,
        "suspension": suspension,
        "suspensionEvidence": suspension_evidence,
        "recommendation": display_recommendation,
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sources": [
            {"name": "Bundesliga.com", "url": BUNDESLIGA_PLAYERS_URL},
            {"name": "Bundesliga.com Statistik", "url": BUNDESLIGA_STATS_URL},
            {"name": "Bundesliga.com Spielerprofil", "url": profile_url},
            *(
                [{"name": "Offizielle Vereinsmeldung", "url": injury_source_url}]
                if injury_source_url
                else []
            ),
            {"name": "OpenLigaDB", "url": "https://www.openligadb.de/"},
        ],
        "dataStatus": {
            "kickbaseAverage": "vorhanden" if average is not None else "nicht verfügbar",
            "publicStatsSource": "erreichbar" if public_source.get("available") else "nicht erreichbar",
            "playerProfileSource": "erreichbar" if profile_available else "nicht recherchiert",
            "playerValues": "nicht erfunden",
            "researchMode": "intensiv" if research_player else "bestehende_daten_beibehalten",
        },
    }


# ============================================================
# HAUPTPROGRAMM
# ============================================================

def main():

    print("=" * 60)
    print(
        "Starte Bundesliga "
        "Player-Intelligence..."
    )
    print("=" * 60)

    print(
        f"Saison: {CURRENT_SEASON}"
    )

    print(
        "Liga: 1. Bundesliga"
    )

    teams = get_bundesliga_teams()

    print(
        f"{len(teams)} "
        "Bundesliga-Teams gefunden."
    )

    if len(teams) != 18:

        raise RuntimeError(
            "FEHLER: Es müssen "
            f"18 Teams sein, "
            f"gefunden: {len(teams)}"
        )

    print(
        "Lade Bundesliga-Spielplan..."
    )

    matches = get_bundesliga_matches()

    print(
        f"{len(matches)} "
        "Spiele geladen."
    )

    print(
        "Lade Bundesliga-Kader von "
        "Bundesliga.com..."
    )

    bundesliga_squads = (
        get_bundesliga_squads()
    )

    print()
    print("Prüfe öffentliche Bundesliga-Statistikquelle...")
    public_source = get_public_player_intelligence_source()

    old_data = load_old_data()
    old_teams = old_data.get("teams", {}) if isinstance(old_data, dict) else {}
    old_players = old_data.get("players", {}) if isinstance(old_data, dict) else {}
    active_roster_ids = load_active_roster_ids()
    active_player_ids = resolve_active_player_ids(
        bundesliga_squads,
        active_roster_ids,
    )
    print(
        "V32-Official-GK: robuster Stammkeeper-Startfloor + offizielles Bundesliga-GK-Profil ohne externen Provider + Szenario-Engine nur für den aufgelösten "
        f"aktiven Kader ({len(active_player_ids)} Spieler); "
        "alle übrigen Spieler behalten ihre vorhandenen Werte."
    )

    # Neue, saubere Datenbank.
    data = {
        "teams": {},
        "players": {},
    }

    updated_teams = 0

    for team in teams:

        team_name = team.get(
            "teamName",
            "",
        )

        if not team_name:
            continue

        print()
        print("=" * 60)
        print(
            f"Verarbeite: "
            f"{team_name}"
        )
        print("=" * 60)

        old_entry = old_teams.get(
            team_name,
            {},
        )

        # ----------------------------------------------------
        # KADER VON BUNDESLIGA.COM
        # ----------------------------------------------------

        squad = bundesliga_squads.get(
            team_name,
            [],
        )

        # Falls die offizielle Seite kurzfristig einen
        # einzelnen Club nicht liefert, behalten wir den
        # vorhandenen Kader aus der letzten JSON.
        if not squad:
            old_squad = old_entry.get(
                "players",
                [],
            )

            if old_squad:
                squad = old_squad
                print(
                    "Bundesliga.com lieferte "
                    "keinen Kader; alter Kader "
                    "wird beibehalten."
                )

        print(
            f"Kader: "
            f"{len(squad)} Spieler"
        )

        # Alte IDs bleiben aus Kompatibilitätsgründen erhalten.
        api_team_id = old_entry.get(
            "apiTeamId"
        )
        api_team_name = old_entry.get(
            "apiTeamName"
        )

        # ----------------------------------------------------
        # NÄCHSTES SPIEL
        # ----------------------------------------------------

        next_match = (
            get_next_match_for_team(
                team_name,
                matches,
            )
        )

        if next_match:

            print(
                "Nächstes Spiel: "
                f"{team_name} vs. "
                f"{next_match['opponent']}"
            )

        else:

            print(
                "Kein nächstes Spiel "
                "gefunden."
            )

        # ----------------------------------------------------
        # INTELLIGENCE
        # ----------------------------------------------------

        intelligence = (
            build_intelligence(
                squad,
                next_match,
            )
        )

        # ----------------------------------------------------
        # SPIELER-DATEN
        # ----------------------------------------------------
        player_count_before = len(
            data["players"]
        )

        for player in squad:
            player_id = player.get("id")

            if not player_id:
                continue

            old_player = old_players.get(
                player_id,
                {},
            )

            data["players"][player_id] = (
                build_player_intelligence(
                    player,
                    team_name,
                    next_match,
                    old_player=old_player,
                    public_source=public_source,
                    research_player=(player_id in active_player_ids),
                    matches=matches,
                )
            )

            # V10: Kein Sleep für normale Spieler, weil dort kein
            # Einzelspieler-Profil abgerufen wird. Bei aktivem Kader
            # wird zwischen öffentlichen Profilabrufen kurz pausiert.
            if player_id in active_player_ids:
                time.sleep(0.25)

        player_count_after = len(
            data["players"]
        )

        print(
            "Spieler-Intelligence: "
            f"{player_count_after - player_count_before} "
            "Spieler hinzugefügt."
        )

        # ----------------------------------------------------
        # TEAM-DATEN
        # ----------------------------------------------------

        team_output = {
            "club": team_name,
            "season": CURRENT_SEASON,
            "league": "1. Bundesliga",

            "apiTeamId": api_team_id,
            "apiTeamName": api_team_name,

            "average": intelligence[
                "average"
            ],

            "starting": intelligence[
                "starting"
            ],

            "form": intelligence[
                "form"
            ],

            "opponent": intelligence[
                "opponent"
            ],

            "homeAway": intelligence[
                "homeAway"
            ],

            "injury": intelligence[
                "injury"
            ],

            "suspension": intelligence[
                "suspension"
            ],

            "recommendation": intelligence[
                "recommendation"
            ],

            "players": intelligence[
                "players"
            ],

            "playerCount": len(squad),

            "nextMatch": next_match,

            "sources": [
                {
                    "title": "Bundesliga.com",
                    "url": (
                        BUNDESLIGA_PLAYERS_URL
                    ),
                },
                {
                    "title": "OpenLigaDB",
                    "url": (
                        "https://www.openligadb.de/"
                    ),
                },
            ],
        }

        # Ausschließlich die 18 offiziellen
        # Namen als JSON-Keys.
        data["teams"][team_name] = (
            team_output
        )

        updated_teams += 1

        print(
            f"OK: {team_name} "
            "aktualisiert."
        )

    # ========================================================
    # META
    # ========================================================

    data["lastUpdated"] = (
        datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")
    )

    data["season"] = CURRENT_SEASON
    data["league"] = "1. Bundesliga"

    data["teamCount"] = len(
        data["teams"]
    )

    data["playerCount"] = len(
        data["players"]
    )

    data["architecture"] = {
        "kickbaseSource": "nicht verwendet",
        "teamsAndPlayers": "Bundesliga.com",
        "fixtures": "OpenLigaDB",
        "playerStats": "bestehende Werte + optionale Einzelspieler-Web-Recherche für aktiven Kader",
        "externalFootballApi": False,
        "activeRosterSource": "kickbase-roster.json (optional)",
    }

    # ========================================================
    # PRÜFUNG
    # ========================================================

    if len(data["teams"]) != 18:

        raise RuntimeError(
            "FEHLER: JSON enthält "
            f"{len(data['teams'])} "
            "Teams statt 18."
        )

    missing_squads = []

    for name, entry in (
        data["teams"].items()
    ):
        if not entry.get(
            "players"
        ):
            missing_squads.append(
                name
            )

    print()
    print("=" * 60)
    print("ERGEBNIS")
    print("=" * 60)

    print(
        f"Teams gespeichert: "
        f"{len(data['teams'])}/18"
    )

    print(
        "Teams mit Bundesliga-Kader: "
        f"{18 - len(missing_squads)}/18"
    )

    print(
        "Spieler gespeichert: "
        f"{len(data['players'])}"
    )

    print(
        "Keine externe Fußball-API für Player-Intelligence verwendet."
    )

    if missing_squads:
        print()
        print(
            "Teams ohne Kader:"
        )

        for name in missing_squads:
            print(
                f"  - {name}"
            )

        print()
        print(
            "HINWEIS: Wenn hier ein Team fehlt, "
            "ist die Zuordnung auf Bundesliga.com "
            "fehlgeschlagen."
        )

    # ========================================================
    # JSON SCHREIBEN
    # ========================================================

    with INTELLIGENCE_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )

    print()
    print(
        "player-intelligence.json "
        "gespeichert."
    )

    print(
        f"{updated_teams} Teams aktualisiert."
    )

    print(
        f"{len(data['players'])} Spieler "
        "gespeichert."
    )


if __name__ == "__main__":
    _v43_policy = v43_provider_policy_summary()
    print(
        "V43-PROVIDER-POLICY "
        f"allowed={','.join(_v43_policy['allowed'])} | "
        f"blocked={','.join(_v43_policy['blocked'])} | "
        f"production={','.join(_v43_policy['productionEnabled'])} | "
        f"rule={_v43_policy['rule']}"
    )
    main()
