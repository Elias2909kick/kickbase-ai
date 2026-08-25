import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# ============================================================
# KONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"

OPENLIGADB_URL = "https://api.openligadb.de"

CURRENT_SEASON = 2026

# Alle Mannschaften, die wir überwachen wollen.
# Die Liga ist für alle drei aktuell die 2. Bundesliga.
TEAMS = [
    {
        "name": "SV Elversberg",
        "search": "Elversberg",
        "league": "bl2",
    },
    {
        "name": "FC Schalke 04",
        "search": "Schalke",
        "league": "bl2",
    },
    {
        "name": "SC Paderborn 07",
        "search": "Paderborn",
        "league": "bl2",
    },
]


# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def now_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def openligadb_get(path):
    """
    Kostenloser Zugriff auf die öffentliche OpenLigaDB API.
    Keine API-Key / kein kostenpflichtiger Dienst.
    """

    url = f"{OPENLIGADB_URL}{path}"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kickbase-ai-player-intelligence/1.0",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")

            if not raw:
                return []

            return json.loads(raw)

    except HTTPError as error:
        print(f"OpenLigaDB HTTP-Fehler {error.code}: {url}")
        return []

    except URLError as error:
        print(f"OpenLigaDB Netzwerkfehler: {error}")
        return []

    except Exception as error:
        print(f"OpenLigaDB Fehler: {error}")
        return []


def load_intelligence():
    """
    Bestehende player-intelligence.json laden.
    Falls die Datei noch nicht existiert, wird eine leere Struktur angelegt.
    """

    if not INTELLIGENCE_FILE.exists():
        return {}

    try:
        with INTELLIGENCE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data

        return {}

    except Exception as error:
        print(f"Fehler beim Lesen von player-intelligence.json: {error}")
        return {}


def save_intelligence(data):
    """
    JSON sauber speichern.
    """

    with INTELLIGENCE_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")


# ============================================================
# TEAM / SPIELDATEN
# ============================================================

def get_team_matches(team_name, search_name, league):
    """
    Holt sämtliche Saisonspiele des Vereins über OpenLigaDB.

    Wir verwenden bewusst:
        /getmatchdata/bl2/2026/Schalke

    OpenLigaDB unterstützt diesen Team-Filter direkt.
    """

    path = (
        f"/getmatchdata/"
        f"{quote(league)}/"
        f"{CURRENT_SEASON}/"
        f"{quote(search_name)}"
    )

    matches = openligadb_get(path)

    if not isinstance(matches, list):
        return []

    print(
        f"{team_name}: "
        f"{len(matches)} Spiele von OpenLigaDB erhalten"
    )

    return matches


def get_team_id(match, wanted_name):
    """
    Team-ID aus einem Match ermitteln.
    """

    wanted = wanted_name.lower()

    for key in ("team1", "team2"):
        team = match.get(key) or {}

        name = str(team.get("teamName") or "").strip()

        if name.lower() == wanted:
            return team.get("teamId")

    # Falls OpenLigaDB einen leicht anderen Namen liefert:
    for key in ("team1", "team2"):
        team = match.get(key) or {}

        name = str(team.get("teamName") or "").strip()

        if wanted in name.lower() or name.lower() in wanted:
            return team.get("teamId")

    return None


def match_belongs_to_team(match, search_name):
    """
    Prüft, ob das Match wirklich zum gewünschten Verein gehört.
    """

    wanted = search_name.lower()

    for key in ("team1", "team2"):
        team = match.get(key) or {}

        name = str(team.get("teamName") or "").lower()

        if wanted in name:
            return True

    return False


def parse_match_date(match):
    """
    OpenLigaDB liefert matchDateTime beispielsweise als:
    2026-08-29T13:00:00

    Wir wandeln das in einen UTC-Datetime-Wert um.
    """

    value = match.get("matchDateTime")

    if not value:
        return None

    try:
        value = str(value)

        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def get_next_match(matches, search_name):
    """
    Sucht das nächste noch nicht vergangene Spiel.
    """

    now = datetime.now(timezone.utc)

    upcoming = []

    for match in matches:

        if not isinstance(match, dict):
            continue

        if not match_belongs_to_team(match, search_name):
            continue

        match_date = parse_match_date(match)

        if match_date is None:
            continue

        if match_date >= now:
            upcoming.append(
                (
                    match_date,
                    match,
                )
            )

    if not upcoming:
        return None

    upcoming.sort(
        key=lambda item: item[0]
    )

    return upcoming[0][1]


def get_match_teams(match):
    """
    Gibt Heim- und Auswärtsteam zurück.
    """

    team1 = match.get("team1") or {}
    team2 = match.get("team2") or {}

    return (
        team1.get("teamName") or "Unbekannt",
        team2.get("teamName") or "Unbekannt",
    )


# ============================================================
# SPIEL-INFORMATIONEN
# ============================================================

def build_match_intelligence(team_config, match):
    """
    Baut die Informationen zum nächsten Spiel.
    """

    if not match:
        return {
            "opponent": None,
            "homeAway": None,
            "matchDate": None,
            "matchId": None,
        }

    home, away = get_match_teams(match)

    team_name = team_config["name"]

    if home.lower() == team_name.lower():
        opponent = away
        home_away = "Heim"

    elif away.lower() == team_name.lower():
        opponent = home
        home_away = "Auswärts"

    else:
        # Fallback, falls OpenLigaDB einen leicht anderen Vereinsnamen liefert.
        home_lower = home.lower()
        away_lower = away.lower()
        wanted = team_config["search"].lower()

        if wanted in home_lower:
            opponent = away
            home_away = "Heim"
        elif wanted in away_lower:
            opponent = home
            home_away = "Auswärts"
        else:
            opponent = None
            home_away = None

    match_date = parse_match_date(match)

    return {
        "opponent": opponent,
        "homeAway": home_away,
        "matchDate": (
            match_date.isoformat()
            if match_date
            else None
        ),
        "matchId": match.get("matchID"),
    }


# ============================================================
# TEAM-DATEN
# ============================================================

def build_team_data(team_config, matches):
    """
    Baut den kompletten Datenblock für einen Verein.
    """

    today = now_date()

    next_match = get_next_match(
        matches,
        team_config["search"],
    )

    match_info = build_match_intelligence(
        team_config,
        next_match,
    )

    return {
        "club": team_config["name"],
        "league": team_config["league"],
        "season": CURRENT_SEASON,

        "nextMatch": match_info,

        "lastUpdated": today,

        "sources": [
            {
                "title": "OpenLigaDB",
                "url": "https://www.openligadb.de/",
                "date": today,
            }
        ],
    }


# ============================================================
# SPIELER / KRISTOF
# ============================================================

def update_existing_kristof(data, team_data):
    """
    Aktualisiert den bestehenden Kristof-Datenblock,
    ohne bereits vorhandene Informationen unnötig zu löschen.

    Wichtig:
    OpenLigaDB liefert keine verlässlichen Spieler-Verletzungs-
    oder Aufstellungsdaten. Deshalb erfinden wir hier nichts.
    """

    kristof = data.get("kristof")

    if not isinstance(kristof, dict):
        kristof = {}

    kristof["club"] = "SV Elversberg"

    next_match = team_data.get("nextMatch") or {}

    kristof["opponent"] = next_match.get("opponent")
    kristof["homeAway"] = next_match.get("homeAway")

    if "injury" not in kristof:
        kristof["injury"] = "Keine aktuelle Meldung"

    if "suspension" not in kristof:
        kristof["suspension"] = "Keine aktuelle Meldung"

    if "starting" not in kristof:
        kristof["starting"] = None

    if "form" not in kristof:
        kristof["form"] = None

    if "average" not in kristof:
        kristof["average"] = None

    if "recommendation" not in kristof:
        kristof["recommendation"] = None

    kristof["lastUpdated"] = now_date()

    data["kristof"] = kristof

    return data


# ============================================================
# HAUPTPROGRAMM
# ============================================================

def main():

    print("Starte kostenlose Multi-Team Player-Intelligence-Recherche...")
    print()

    data = load_intelligence()

    successful_teams = 0

    # --------------------------------------------------------
    # ALLE VEREINE DURCHLAUFEN
    # --------------------------------------------------------

    for team_config in TEAMS:

        team_name = team_config["name"]

        print("----------------------------------------")
        print(f"Verarbeite: {team_name}")

        matches = get_team_matches(
            team_name=team_name,
            search_name=team_config["search"],
            league=team_config["league"],
        )

        if not matches:

            print(
                f"KEINE DATEN für {team_name}"
            )

            # Wichtig:
            # Nicht den gesamten Workflow abbrechen.
            # Der nächste Verein wird trotzdem verarbeitet.
            continue

        team_data = build_team_data(
            team_config,
            matches,
        )

        next_match = team_data["nextMatch"]

        if next_match.get("opponent"):

            print(
                f"Nächstes Spiel: "
                f"{team_name} "
                f"gegen "
                f"{next_match['opponent']}"
            )

            print(
                f"Spielort: "
                f"{next_match['homeAway']}"
            )

            print(
                f"Datum: "
                f"{next_match['matchDate']}"
            )

        else:
            print(
                f"Kein kommendes Spiel für {team_name}"
            )

        # Unter "teams" speichern.
        if "teams" not in data:
            data["teams"] = {}

        # JSON-Key möglichst stabil halten.
        if team_name == "SV Elversberg":
            key = "elversberg"
        elif team_name == "FC Schalke 04":
            key = "schalke"
        elif team_name == "SC Paderborn 07":
            key = "paderborn"
        else:
            key = team_name.lower().replace(" ", "_")

        data["teams"][key] = team_data

        successful_teams += 1

        print(
            f"Daten für {team_name} aktualisiert."
        )
        print()

    # --------------------------------------------------------
    # KRISTOF
    # --------------------------------------------------------

    elversberg_data = (
        data.get("teams", {})
        .get("elversberg")
    )

    if isinstance(elversberg_data, dict):

        data = update_existing_kristof(
            data,
            elversberg_data,
        )

    # --------------------------------------------------------
    # METADATEN
    # --------------------------------------------------------

    data["lastUpdated"] = now_date()

    # --------------------------------------------------------
    # SPEICHERN
    # --------------------------------------------------------

    if successful_teams == 0:
        raise RuntimeError(
            "Kein einziger Verein konnte aktualisiert werden."
        )

    save_intelligence(data)

    print("----------------------------------------")
    print(
        f"{successful_teams} von "
        f"{len(TEAMS)} Vereinen aktualisiert."
    )
    print(
        "player-intelligence.json wurde aktualisiert."
    )


if __name__ == "__main__":
    main()
