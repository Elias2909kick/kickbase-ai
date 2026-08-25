#!/usr/bin/env python3

"""
Player Intelligence – Bundesliga 2026/27

Ziel:
- automatisch alle 18 Bundesliga-Teams laden
- kompletten Kader jedes Teams laden
- nächstes Spiel jedes Teams ermitteln
- Verletzungen und Sperren berücksichtigen
- aktuelle Saisonstatistiken auswerten
- Startelf-Wahrscheinlichkeit berechnen
- Form bewerten
- Empfehlung erzeugen
- Ergebnis als player-intelligence.json speichern
- Workflow mit Fehler beenden, wenn nicht alle 18 Teams verarbeitet wurden

API:
    API-Football / API-Sports

Benötigte Umgebungsvariable:
    API_FOOTBALL_KEY

Optional:
    OUTPUT_FILE
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


# ============================================================
# KONFIGURATION
# ============================================================

API_BASE_URL = "https://v3.football.api-sports.io"

# Bundesliga
BUNDESLIGA_LEAGUE_ID = 78

# API-Football verwendet das Startjahr der Saison.
# 2026 = Saison 2026/27
SEASON = 2026

EXPECTED_TEAM_COUNT = 18

OUTPUT_FILE = Path(
    os.getenv(
        "OUTPUT_FILE",
        "player-intelligence.json"
    )
)

API_KEY = os.getenv("API_FOOTBALL_KEY")

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3

# Nicht unnötig viele API-Anfragen machen.
REQUEST_DELAY = 0.15

TODAY = datetime.now(timezone.utc).date().isoformat()


# ============================================================
# HTTP / API
# ============================================================

session = requests.Session()


def die(message: str) -> None:
    print(f"\nFEHLER: {message}", file=sys.stderr)
    sys.exit(1)


def api_get(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:

    if not API_KEY:
        die(
            "Umgebungsvariable API_FOOTBALL_KEY fehlt."
        )

    url = f"{API_BASE_URL}/{endpoint.lstrip('/')}"

    headers = {
        "x-apisports-key": API_KEY,
        "Accept": "application/json",
    }

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            response = session.get(
                url,
                headers=headers,
                params=params or {},
                timeout=REQUEST_TIMEOUT,
            )

        except requests.RequestException as exc:

            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    f"API-Verbindung fehlgeschlagen: {exc}"
                )

            time.sleep(attempt)
            continue

        if response.status_code == 429:

            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    "API-Football Rate Limit erreicht."
                )

            retry_after = response.headers.get(
                "Retry-After",
                str(attempt * 2)
            )

            try:
                wait_seconds = int(retry_after)
            except ValueError:
                wait_seconds = attempt * 2

            print(
                f"Rate Limit – warte {wait_seconds}s..."
            )

            time.sleep(wait_seconds)
            continue

        if response.status_code >= 400:

            try:
                body = response.json()
            except Exception:
                body = response.text

            raise RuntimeError(
                f"API HTTP {response.status_code}: {body}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"API lieferte kein gültiges JSON: {exc}"
            )

        errors = data.get("errors")

        if errors:

            # API-Football kann errors als dict oder list liefern.
            if isinstance(errors, dict):
                error_text = "; ".join(
                    f"{k}: {v}"
                    for k, v in errors.items()
                )
            else:
                error_text = str(errors)

            raise RuntimeError(
                f"API-Football Fehler: {error_text}"
            )

        time.sleep(REQUEST_DELAY)

        return data

    raise RuntimeError(
        f"API-Anfrage fehlgeschlagen: {endpoint}"
    )


# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def safe_int(value: Any) -> Optional[int]:

    if value is None:
        return None

    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def safe_float(value: Any) -> Optional[float]:

    if value is None:
        return None

    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def clean_name(value: Any) -> str:

    if value is None:
        return ""

    return str(value).strip()


def slugify(value: str) -> str:

    value = value.lower().strip()

    # Unicode-Buchstaben zunächst erhalten.
    value = re.sub(
        r"[^\w\s-]",
        "",
        value,
        flags=re.UNICODE,
    )

    value = re.sub(
        r"[\s-]+",
        "_",
        value,
    )

    return value.strip("_")


def confidence(
    starting: str,
    injury: str,
    suspension: str,
    recommendation: str,
) -> Dict[str, str]:

    return {
        "starting": starting,
        "injury": injury,
        "suspension": suspension,
        "recommendation": recommendation,
    }


# ============================================================
# TEAMS
# ============================================================

def get_bundesliga_teams() -> List[Dict[str, Any]]:

    print(
        "\nLade Bundesliga-Teams "
        f"{SEASON}/{SEASON + 1}..."
    )

    data = api_get(
        "/teams",
        {
            "league": BUNDESLIGA_LEAGUE_ID,
            "season": SEASON,
        },
    )

    teams = []

    for item in data.get("response", []):

        team = item.get("team", {})

        team_id = safe_int(team.get("id"))
        team_name = clean_name(team.get("name"))

        if not team_id or not team_name:
            continue

        teams.append(
            {
                "id": team_id,
                "name": team_name,
                "code": team.get("code"),
                "logo": team.get("logo"),
            }
        )

    teams.sort(
        key=lambda team: team["name"].lower()
    )

    print(
        f"API-Football liefert {len(teams)} Teams."
    )

    if len(teams) != EXPECTED_TEAM_COUNT:

        names = ", ".join(
            team["name"]
            for team in teams
        )

        raise RuntimeError(
            "Es wurden nicht genau 18 Bundesliga-Teams "
            f"gefunden.\n"
            f"Gefunden: {len(teams)}\n"
            f"Teams: {names}"
        )

    return teams


# ============================================================
# SPIELER / KADER
# ============================================================

def get_team_players(
    team_id: int,
) -> List[Dict[str, Any]]:

    players: List[Dict[str, Any]] = []

    page = 1

    while True:

        data = api_get(
            "/players",
            {
                "team": team_id,
                "season": SEASON,
                "page": page,
            },
        )

        response = data.get("response", [])

        players.extend(response)

        paging = data.get("paging", {})

        total_pages = safe_int(
            paging.get("total")
        ) or page

        if page >= total_pages:
            break

        page += 1

    return players


# ============================================================
# NÄCHSTES SPIEL
# ============================================================

def get_next_fixture(
    team_id: int,
) -> Optional[Dict[str, Any]]:

    data = api_get(
        "/fixtures",
        {
            "team": team_id,
            "season": SEASON,
            "next": 1,
        },
    )

    fixtures = data.get("response", [])

    if not fixtures:
        return None

    fixture = fixtures[0]

    fixture_data = fixture.get("fixture", {})
    teams = fixture.get("teams", {})

    home = teams.get("home", {})
    away = teams.get("away", {})

    home_id = safe_int(home.get("id"))
    away_id = safe_int(away.get("id"))

    if home_id == team_id:
        opponent = clean_name(
            away.get("name")
        )
        home_away = "Heim"
    else:
        opponent = clean_name(
            home.get("name")
        )
        home_away = "Auswärts"

    return {
        "opponent": opponent or None,
        "homeAway": home_away,
        "matchDate": fixture_data.get("date"),
        "matchId": safe_int(
            fixture_data.get("id")
        ),
    }


# ============================================================
# VERLETZUNGEN / SPERREN
# ============================================================

def get_team_absences(
    team_id: int,
) -> Dict[int, Dict[str, str]]:

    """
    Liefert:
        {
            player_id: {
                "injury": "...",
                "suspension": "..."
            }
        }
    """

    result: Dict[int, Dict[str, str]] = {}

    data = api_get(
        "/injuries",
        {
            "team": team_id,
            "season": SEASON,
        },
    )

    for item in data.get("response", []):

        player = item.get("player", {})

        player_id = safe_int(
            player.get("id")
        )

        if not player_id:
            continue

        absence_type = clean_name(
            item.get("type")
        )

        reason = clean_name(
            item.get("reason")
        )

        if not reason:
            reason = "Aktuelle Meldung vorhanden"

        entry = result.setdefault(
            player_id,
            {
                "injury": (
                    "Keine aktuelle "
                    "Verletzungsmeldung gefunden"
                ),
                "suspension": (
                    "Keine aktuelle "
                    "Sperrmeldung gefunden"
                ),
            },
        )

        lower_type = absence_type.lower()

        if (
            "suspension" in lower_type
            or "sperr" in lower_type
        ):
            entry["suspension"] = reason

        elif (
            "injury" in lower_type
            or "verletz" in lower_type
        ):
            entry["injury"] = reason

        else:
            # Falls API einen unbekannten Typ liefert,
            # lieber nicht als Verletzung klassifizieren.
            entry["injury"] = reason

    return result


# ============================================================
# SPIELER-STATISTIK
# ============================================================

def get_primary_statistics(
    player_record: Dict[str, Any],
) -> Dict[str, Any]:

    statistics = player_record.get(
        "statistics"
    )

    if not isinstance(statistics, list):
        return {}

    if not statistics:
        return {}

    # Bundesliga-Statistik bevorzugen.
    for stat in statistics:

        league = stat.get("league", {})

        if safe_int(league.get("id")) == BUNDESLIGA_LEAGUE_ID:
            return stat

    return statistics[0]


def extract_player_stats(
    player_record: Dict[str, Any],
) -> Dict[str, Any]:

    player = player_record.get(
        "player",
        {}
    )

    stats = get_primary_statistics(
        player_record
    )

    games = stats.get("games") or {}
    goals = stats.get("goals") or {}
    cards = stats.get("cards") or {}

    appearances = safe_int(
        games.get("appearences")
    )

    lineups = safe_int(
        games.get("lineups")
    )

    minutes = safe_int(
        games.get("minutes")
    )

    rating = safe_float(
        games.get("rating")
    )

    goals_total = safe_int(
        goals.get("total")
    ) or 0

    assists = safe_int(
        goals.get("assists")
    ) or 0

    yellow = safe_int(
        cards.get("yellow")
    ) or 0

    red = safe_int(
        cards.get("red")
    ) or 0

    position = clean_name(
        games.get("position")
    )

    number = safe_int(
        games.get("number")
    )

    if not position:
        position = None

    return {
        "id": safe_int(
            player.get("id")
        ),
        "name": clean_name(
            player.get("name")
        ),
        "position": position,
        "number": number,
        "appearances": appearances,
        "lineups": lineups,
        "minutes": minutes,
        "rating": rating,
        "goals": goals_total,
        "assists": assists,
        "yellow": yellow,
        "red": red,
        "injuredFlag": player.get("injured"),
    }


# ============================================================
# INTELLIGENCE
# ============================================================

def calculate_starting_probability(
    stats: Dict[str, Any],
    injury: str,
    suspension: str,
) -> Dict[str, Any]:

    appearances = stats.get("appearances")
    lineups = stats.get("lineups")
    minutes = stats.get("minutes")

    if (
        "Keine aktuelle" not in injury
        or "Keine aktuelle" not in suspension
    ):
        return {
            "value": 0,
            "label": "sehr unwahrscheinlich",
            "confidence": "hoch",
        }

    if appearances is None:
        return {
            "value": None,
            "label": "unbekannt",
            "confidence": "niedrig",
        }

    if lineups is None:
        lineups = 0

    # Bei noch sehr wenigen Saisonspielen
    # keine übertriebene Sicherheit erzeugen.
    if appearances <= 0:
        probability = 5

    else:

        lineup_ratio = (
            lineups / appearances
            if appearances
            else 0
        )

        probability = round(
            lineup_ratio * 100
        )

        # Minuten geben zusätzliche Information.
        if minutes is not None and appearances > 0:

            avg_minutes = (
                minutes / appearances
            )

            if avg_minutes >= 75:
                probability += 10

            elif avg_minutes >= 55:
                probability += 5

            elif avg_minutes < 20:
                probability -= 10

    probability = max(
        0,
        min(95, probability),
    )

    if probability >= 75:
        label = "hoch"
    elif probability >= 45:
        label = "mittel"
    elif probability >= 20:
        label = "niedrig"
    else:
        label = "sehr unwahrscheinlich"

    return {
        "value": probability,
        "label": label,
        "confidence": (
            "mittel"
            if appearances >= 3
            else "niedrig"
        ),
    }


def calculate_form(
    stats: Dict[str, Any],
) -> Dict[str, Any]:

    rating = stats.get("rating")
    appearances = stats.get("appearances")
    goals = stats.get("goals") or 0
    assists = stats.get("assists") or 0

    if rating is not None:

        if rating >= 7.5:
            label = "sehr gut"
        elif rating >= 7.0:
            label = "gut"
        elif rating >= 6.5:
            label = "solide"
        elif rating >= 6.0:
            label = "durchwachsen"
        else:
            label = "schwach"

        return {
            "rating": round(rating, 2),
            "label": label,
            "confidence": (
                "mittel"
                if (appearances or 0) >= 3
                else "niedrig"
            ),
        }

    if appearances and appearances > 0:

        contribution = (
            goals + assists
        )

        if contribution > 0:
            label = "positiv"
        else:
            label = "unauffällig"

        return {
            "rating": None,
            "label": label,
            "confidence": "niedrig",
        }

    return {
        "rating": None,
        "label": None,
        "confidence": "niedrig",
    }


def calculate_recommendation(
    starting: Dict[str, Any],
    form: Dict[str, Any],
    injury: str,
    suspension: str,
) -> str:

    if (
        "Keine aktuelle" not in injury
        or "Keine aktuelle" not in suspension
    ):
        return "Nicht berücksichtigen"

    starting_value = starting.get("value")

    form_label = form.get("label")

    if starting_value is not None:

        if starting_value >= 75:

            if form_label in (
                "sehr gut",
                "gut",
            ):
                return "Starten"

            return "Startkandidat"

        if starting_value >= 45:

            if form_label in (
                "sehr gut",
                "gut",
            ):
                return "Beobachten"

            return "Rotation"

        if starting_value >= 20:
            return "Beobachten"

    return "Beobachten"


# ============================================================
# SPIELER-ID / KEY
# ============================================================

def player_key(
    club_name: str,
    player: Dict[str, Any],
) -> str:

    player_id = player.get("id")

    if player_id:
        return (
            f"{slugify(club_name)}"
            f"_{player_id}"
        )

    return (
        f"{slugify(club_name)}"
        f"_{slugify(player.get('name', 'unknown'))}"
    )


# ============================================================
# EIN TEAM VERARBEITEN
# ============================================================

def process_team(
    team: Dict[str, Any],
) -> Dict[str, Any]:

    team_id = team["id"]
    team_name = team["name"]

    print("\n" + "=" * 60)
    print(
        f"Verarbeite: {team_name}"
    )
    print("=" * 60)

    print("Lade Kader...")

    raw_players = get_team_players(
        team_id
    )

    print(
        f"{team_name}: "
        f"{len(raw_players)} Spieler gefunden"
    )

    if not raw_players:
        raise RuntimeError(
            f"Kein Kader für {team_name} gefunden."
        )

    print("Lade nächstes Spiel...")

    next_match = get_next_fixture(
        team_id
    )

    if next_match:
        print(
            f"Nächstes Spiel: "
            f"{next_match['opponent']} "
            f"({next_match['homeAway']})"
        )
    else:
        print(
            "Kein nächstes Spiel gefunden."
        )

    print("Lade Verletzungen/Sperren...")

    absences = get_team_absences(
        team_id
    )

    print(
        f"{len(absences)} "
        "Verletzungs-/Sperrmeldungen gefunden"
    )

    team_players: Dict[str, Any] = {}

    for record in raw_players:

        stats = extract_player_stats(
            record
        )

        player_id = stats.get("id")

        if not player_id:
            continue

        injury_data = absences.get(
            player_id,
            {
                "injury": (
                    "Keine aktuelle "
                    "Verletzungsmeldung gefunden"
                ),
                "suspension": (
                    "Keine aktuelle "
                    "Sperrmeldung gefunden"
                ),
            },
        )

        injury = injury_data["injury"]
        suspension = injury_data["suspension"]

        starting = calculate_starting_probability(
            stats,
            injury,
            suspension,
        )

        form = calculate_form(
            stats
        )

        recommendation = calculate_recommendation(
            starting,
            form,
            injury,
            suspension,
        )

        starting_value = starting.get(
            "value"
        )

        team_player_key = player_key(
            team_name,
            stats,
        )

        team_players[team_player_key] = {
            "id": player_id,
            "name": stats.get("name"),
            "club": team_name,
            "position": stats.get("position"),
            "number": stats.get("number"),

            "starting": starting_value,
            "startingLabel": starting.get(
                "label"
            ),

            "form": form.get(
                "rating"
            ),

            "opponent": (
                next_match["opponent"]
                if next_match
                else None
            ),

            "homeAway": (
                next_match["homeAway"]
                if next_match
                else None
            ),

            "injury": injury,
            "suspension": suspension,

            "recommendation": recommendation,

            "seasonStats": {
                "appearances": stats.get(
                    "appearances"
                ),
                "lineups": stats.get(
                    "lineups"
                ),
                "minutes": stats.get(
                    "minutes"
                ),
                "rating": stats.get(
                    "rating"
                ),
                "goals": stats.get(
                    "goals"
                ),
                "assists": stats.get(
                    "assists"
                ),
                "yellow": stats.get(
                    "yellow"
                ),
                "red": stats.get(
                    "red"
                ),
            },

            "sources": [
                {
                    "title": "API-Football",
                    "url": "https://www.api-football.com/",
                    "date": TODAY,
                }
            ],

            "lastUpdated": TODAY,

            "confidence": confidence(
                starting=starting.get(
                    "confidence",
                    "niedrig",
                ),
                injury=(
                    "mittel"
                    if injury_data.get("injury")
                    else "niedrig"
                ),
                suspension=(
                    "mittel"
                    if injury_data.get("suspension")
                    else "niedrig"
                ),
                recommendation=(
                    "mittel"
                    if recommendation
                    else "niedrig"
                ),
            ),
        }

    if not team_players:
        raise RuntimeError(
            f"{team_name}: "
            "Kader konnte nicht in Spieler "
            "umgewandelt werden."
        )

    print(
        f"{team_name}: "
        f"{len(team_players)} Spieler verarbeitet"
    )

    return {
        "club": team_name,
        "league": "Bundesliga",
        "season": SEASON,
        "apiFootballTeamId": team_id,

        "nextMatch": next_match,

        "playerCount": len(
            team_players
        ),

        "lastUpdated": TODAY,

        "source": {
            "name": "API-Football",
            "url": "https://www.api-football.com/",
        },
    }, team_players


# ============================================================
# VALIDIERUNG
# ============================================================

def validate_result(
    result: Dict[str, Any],
    teams: List[Dict[str, Any]],
) -> None:

    expected_names = {
        team["name"]
        for team in teams
    }

    actual_teams = result.get(
        "teams",
        {}
    )

    actual_names = set(
        actual_teams.keys()
    )

    missing = expected_names - actual_names

    extra = actual_names - expected_names

    if missing:
        raise RuntimeError(
            "Folgende Teams fehlen im Ergebnis: "
            + ", ".join(sorted(missing))
        )

    if extra:
        print(
            "Warnung: zusätzliche Teams im Ergebnis: "
            + ", ".join(sorted(extra))
        )

    if len(actual_teams) != EXPECTED_TEAM_COUNT:
        raise RuntimeError(
            "Validierung fehlgeschlagen: "
            f"{len(actual_teams)} Teams statt "
            f"{EXPECTED_TEAM_COUNT}."
        )

    players = result.get(
        "players",
        {}
    )

    if not players:
        raise RuntimeError(
            "Validierung fehlgeschlagen: "
            "keine Spieler vorhanden."
        )

    teams_with_zero_players = []

    for team_name, team_data in actual_teams.items():

        count = safe_int(
            team_data.get("playerCount")
        ) or 0

        if count <= 0:
            teams_with_zero_players.append(
                team_name
            )

    if teams_with_zero_players:
        raise RuntimeError(
            "Teams ohne Spieler: "
            + ", ".join(
                teams_with_zero_players
            )
        )

    # Prüfen, ob die Player-Datensätze zu den Teams passen.
    team_player_counts: Dict[str, int] = {}

    for player in players.values():

        club = player.get("club")

        if not club:
            raise RuntimeError(
                "Spieler ohne club gefunden."
            )

        team_player_counts[club] = (
            team_player_counts.get(club, 0)
            + 1
        )

    for team_name in expected_names:

        if team_player_counts.get(
            team_name,
            0
        ) <= 0:

            raise RuntimeError(
                f"Keine Spieler für {team_name}."
            )

    print("\n" + "=" * 60)
    print("VALIDIERUNG ERFOLGREICH")
    print("=" * 60)

    print(
        f"Teams:   {len(actual_teams)}/18"
    )

    print(
        f"Spieler: {len(players)}"
    )

    print("=" * 60)


# ============================================================
# JSON SPEICHERN
# ============================================================

def save_json(
    result: Dict[str, Any],
) -> None:

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_file = OUTPUT_FILE.with_suffix(
        OUTPUT_FILE.suffix + ".tmp"
    )

    with temporary_file.open(
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            result,
            file,
            ensure_ascii=False,
            indent=2,
        )

        file.write("\n")

    temporary_file.replace(
        OUTPUT_FILE
    )

    print(
        f"\nJSON gespeichert: "
        f"{OUTPUT_FILE}"
    )


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    print(
        "Starte Bundesliga "
        "Player-Intelligence..."
    )

    print(
        f"Saison: {SEASON}/{SEASON + 1}"
    )

    print(
        f"Liga-ID: {BUNDESLIGA_LEAGUE_ID}"
    )

    print(
        "Quelle: API-Football"
    )

    # --------------------------------------------------------
    # 1. Alle 18 Bundesliga-Teams laden
    # --------------------------------------------------------

    teams = get_bundesliga_teams()

    # --------------------------------------------------------
    # 2. Ergebnis-Grundstruktur
    # --------------------------------------------------------

    result: Dict[str, Any] = {
        "players": {},
        "teams": {},
        "lastUpdated": TODAY,
        "league": "Bundesliga",
        "season": SEASON,
        "teamCount": 0,
        "playerCount": 0,
    }

    # --------------------------------------------------------
    # 3. Jedes Team verarbeiten
    # --------------------------------------------------------

    successful_teams = 0

    for team in teams:

        team_name = team["name"]

        try:

            team_data, player_data = process_team(
                team
            )

            result["teams"][team_name] = (
                team_data
            )

            result["players"].update(
                player_data
            )

            successful_teams += 1

        except Exception as exc:

            print(
                f"\nFEHLER bei {team_name}: "
                f"{exc}",
                file=sys.stderr,
            )

            # Absichtlich sofort abbrechen.
            # Dadurch kann kein "Success"-JSON
            # mit unvollständigen Teams entstehen.
            raise

    # --------------------------------------------------------
    # 4. Counts aktualisieren
    # --------------------------------------------------------

    result["teamCount"] = len(
        result["teams"]
    )

    result["playerCount"] = len(
        result["players"]
    )

    # --------------------------------------------------------
    # 5. Harte Validierung
    # --------------------------------------------------------

    validate_result(
        result,
        teams,
    )

    # --------------------------------------------------------
    # 6. Speichern
    # --------------------------------------------------------

    save_json(
        result
    )

    print(
        "\nPlayer Intelligence erfolgreich "
        "aktualisiert."
    )

    print(
        f"{successful_teams} Teams / "
        f"{result['playerCount']} Spieler"
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:
        print(
            "\nAbgebrochen."
        )
        sys.exit(130)

    except Exception as exc:

        print(
            "\n" + "=" * 60,
            file=sys.stderr,
        )

        print(
            "UPDATE FEHLGESCHLAGEN",
            file=sys.stderr,
        )

        print(
            "=" * 60,
            file=sys.stderr,
        )

        print(
            str(exc),
            file=sys.stderr,
        )

        sys.exit(1)
