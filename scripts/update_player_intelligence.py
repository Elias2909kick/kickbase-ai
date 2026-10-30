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
