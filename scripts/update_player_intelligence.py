import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


# ============================================================
# KONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"

API_URL = "https://v3.football.api-sports.io"
API_KEY = os.environ.get("API_FOOTBALL_KEY")

OPENLIGADB_URL = "https://api.openligadb.de"

CURRENT_SEASON = 2026
BUNDESLIGA_LEAGUE = "bl1"

# Die 18 Teams kommen aus unserer bereits vorhandenen JSON.
# Dadurch müssen wir die Vereine nicht doppelt pflegen.
DEFAULT_TEAMS = {
    "bayern": "FC Bayern München",
    "dortmund": "Borussia Dortmund",
    "leipzig": "RB Leipzig",
    "stuttgart": "VfB Stuttgart",
    "hoffenheim": "TSG Hoffenheim",
    "leverkusen": "Bayer 04 Leverkusen",
    "freiburg": "Sport-Club Freiburg",
    "frankfurt": "Eintracht Frankfurt",
    "augsburg": "FC Augsburg",
    "mainz": "1. FSV Mainz 05",
    "union_berlin": "1. FC Union Berlin",
    "monchengladbach": "Borussia Mönchengladbach",
    "hamburg": "Hamburger SV",
    "koln": "1. FC Köln",
    "bremen": "SV Werder Bremen",
    "schalke": "FC Schalke 04",
    "elversberg": "SV Elversberg",
    "paderborn": "SC Paderborn 07",
}


# ============================================================
# ALLGEMEINE HILFSFUNKTIONEN
# ============================================================

def now_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_intelligence():
    if not INTELLIGENCE_FILE.exists():
        return {}

    with INTELLIGENCE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_intelligence(data):
    with INTELLIGENCE_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )
        file.write("\n")


# ============================================================
# API-FOOTBALL
# ============================================================

def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError(
            "API_FOOTBALL_KEY fehlt in den GitHub Secrets."
        )

    params = params or {}

    url = f"{API_URL}/{endpoint}"

    if params:
        url += "?" + urlencode(params)

    request = Request(
        url,
        headers={
            "x-apisports-key": API_KEY,
            "Accept": "application/json",
        },
    )

    with urlopen(request, timeout=30) as response:
        data = json.loads(
            response.read().decode("utf-8")
        )

    if data.get("errors"):
        raise RuntimeError(
            f"API-Football Fehler: {data['errors']}"
        )

    return data


# ============================================================
# OPENLIGADB
# ============================================================

def openligadb_get(path):
    url = f"{OPENLIGADB_URL}{path}"

    request = Request(
        url,
        headers={
            "Accept": "application/json"
        },
    )

    with urlopen(request, timeout=30) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


# ============================================================
# TEAM-ID VON API-FOOTBALL FINDEN
# ============================================================

def find_api_team(team_name):
    """
    Sucht einen Verein bei API-Football.

    Wir verwenden bewusst 'search', damit wir nicht von
    manuell gepflegten API-Team-IDs abhängig sind.
    """

    data = api_get(
        "teams",
        {
            "search": team_name
        }
    )

    matches = data.get("response", [])

    if not matches:
        return None

    wanted = team_name.lower()

    # Exakte Übereinstimmung bevorzugen
    for entry in matches:
        team = entry.get("team", {})
        name = team.get("name", "")

        if name.lower() == wanted:
            return team

    # Falls API einen leicht anderen Namen liefert:
    # ersten sinnvollen Treffer nehmen.
    for entry in matches:
        team = entry.get("team", {})
        name = team.get("name", "")

        if name:
            return team

    return None


# ============================================================
# KADER LADEN
# ============================================================

def get_team_squad(api_team_id):
    """
    Holt den aktuellen registrierten Kader.

    Wichtig:
    /players/squads benötigt keine Saison.
    Dadurch umgehen wir das Free-Plan-Problem mit season=2026.
    """

    data = api_get(
        "players/squads",
        {
            "team": api_team_id
        }
    )

    response = data.get("response", [])

    if not response:
        return []

    squad = response[0].get("players", [])

    return squad


# ============================================================
# AKTUELLE VERLETZUNGEN / SPERREN
# ============================================================

def get_team_injuries(api_team_id):
    """
    Holt aktuelle Verletzungs-/Sperrdaten.

    Wir verwenden bewusst keine Saisonabfrage.
    """

    try:
        data = api_get(
            "injuries",
            {
                "team": api_team_id
            }
        )

        return data.get("response", [])

    except Exception as error:
        print(
            f"  Warnung: Verletzungsdaten nicht verfügbar: {error}"
        )
        return []


# ============================================================
# SPIELER-ID NORMALISIEREN
# ============================================================

def player_key(player_name):
    """
    Erzeugt einen stabilen JSON-Key.
    """

    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "é": "e",
        "è": "e",
        "á": "a",
        "à": "a",
        "í": "i",
        "ó": "o",
        "ú": "u",
    }

    value = player_name.lower().strip()

    for old, new in replacements.items():
        value = value.replace(old, new)

    result = []

    for char in value:
        if char.isalnum():
            result.append(char)
        elif char in (" ", "-", "_"):
            result.append("_")

    key = "".join(result)

    while "__" in key:
        key = key.replace("__", "_")

    return key.strip("_")


# ============================================================
# VERLETZUNG FÜR EINEN SPIELER FINDEN
# ============================================================

def find_player_status(player_id, injuries):
    """
    Sucht aktuelle Verletzung/Sperre eines Spielers.
    """

    matches = []

    for item in injuries:
        player = item.get("player", {})
        item_player_id = player.get("id")

        if item_player_id == player_id:
            matches.append(item)

    if not matches:
        return {
            "injury": None,
            "suspension": None,
        }

    injury = None
    suspension = None

    for item in matches:
        player = item.get("player", {})

        type_name = (
            player.get("type")
            or item.get("type")
            or ""
        )

        reason = (
            player.get("reason")
            or item.get("reason")
            or "Aktuelle Meldung"
        )

        type_lower = str(type_name).lower()

        if "susp" in type_lower:
            suspension = reason
        else:
            injury = reason

    return {
        "injury": injury,
        "suspension": suspension,
    }


# ============================================================
# EMPFEHLUNG
# ============================================================

def calculate_recommendation(
    injury,
    suspension,
    position
):
    """
    Bewusst konservative Empfehlung.

    Wir behaupten NICHT, dass ein Spieler sicher startet,
    wenn dafür keine belastbaren Aufstellungsdaten vorliegen.
    """

    if suspension:
        return "Nicht aufstellen"

    if injury:
        return "Nicht aufstellen"

    return "Beobachten"


# ============================================================
# NÄCHSTES SPIEL AUS OPENLIGADB
# ============================================================

def get_next_match(team_name):
    """
    Holt die kommenden Spiele über OpenLigaDB.

    Dadurch brauchen wir für das nächste Spiel keine
    API-Football-season-Abfrage.
    """

    try:
        matches = openligadb_get(
            f"/getmatchdata/{BUNDESLIGA_LEAGUE}/{CURRENT_SEASON}"
        )
    except Exception as error:
        print(
            f"  Warnung: OpenLigaDB nicht erreichbar: {error}"
        )
        return None

    now = datetime.now(timezone.utc)

    upcoming = []

    for match in matches:
        match_date = match.get("matchDateTimeUTC")

        if not match_date:
            continue

        try:
            dt = datetime.fromisoformat(
                match_date.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        if dt < now:
            continue

        team1 = match.get("team1", {})
        team2 = match.get("team2", {})

        name1 = team1.get("teamName", "")
        name2 = team2.get("teamName", "")

        wanted = team_name.lower()

        if (
            wanted in name1.lower()
            or name1.lower() in wanted
            or wanted in name2.lower()
            or name2.lower() in wanted
        ):
            upcoming.append(match)

    if not upcoming:
        return None

    upcoming.sort(
        key=lambda match: match.get(
            "matchDateTimeUTC",
            ""
        )
    )

    match = upcoming[0]

    team1 = match.get("team1", {})
    team2 = match.get("team2", {})

    if team_name.lower() in team1.get(
        "teamName",
        ""
    ).lower():
        opponent = team2.get("teamName")
        home_away = "Heim"
    else:
        opponent = team1.get("teamName")
        home_away = "Auswärts"

    return {
        "opponent": opponent,
        "homeAway": home_away,
        "matchDate": match.get("matchDateTimeUTC"),
        "matchId": match.get("matchID"),
    }


# ============================================================
# SPIELER-INTELLIGENCE
# ============================================================

def build_player_intelligence(
    player,
    club_name,
    team_match,
    status
):
    player_id = player.get("id")
    name = player.get("name", "Unbekannter Spieler")

    position = player.get("position")
    number = player.get("number")

    injury = status.get("injury")
    suspension = status.get("suspension")

    recommendation = calculate_recommendation(
        injury,
        suspension,
        position
    )

    if injury:
        injury_value = injury
    else:
        injury_value = "Keine aktuelle Verletzungsmeldung gefunden"

    if suspension:
        suspension_value = suspension
    else:
        suspension_value = "Keine aktuelle Sperrmeldung gefunden"

    if injury:
        starting = "Unwahrscheinlich"
    elif suspension:
        starting = "Unwahrscheinlich"
    else:
        # Ohne bestätigte Aufstellung nicht künstlich behaupten.
        starting = None

    return {
        "id": player_id,
        "name": name,
        "club": club_name,
        "position": position,
        "number": number,
        "starting": starting,
        "form": None,
        "opponent": (
            team_match.get("opponent")
            if team_match
            else None
        ),
        "homeAway": (
            team_match.get("homeAway")
            if team_match
            else None
        ),
        "injury": injury_value,
        "suspension": suspension_value,
        "recommendation": recommendation,
        "sources": [
            {
                "title": "API-Football",
                "url": "https://www.api-football.com/",
                "date": now_date(),
            }
        ],
        "lastUpdated": now_date(),
        "confidence": {
            "starting": (
                "niedrig"
                if starting is None
                else "hoch"
            ),
            "injury": (
                "hoch"
                if injury
                else "mittel"
            ),
            "suspension": (
                "hoch"
                if suspension
                else "mittel"
            ),
            "recommendation": "mittel",
        },
    }


# ============================================================
# TEAM-INTELLIGENCE
# ============================================================

def build_team_entry(
    team_key,
    club_name,
    api_team,
    next_match,
    player_count
):
    return {
        "club": club_name,
        "league": "Bundesliga",
        "season": CURRENT_SEASON,
        "apiFootballTeamId": (
            api_team.get("id")
            if api_team
            else None
        ),
        "nextMatch": next_match,
        "playerCount": player_count,
        "lastUpdated": now_date(),
        "source": {
            "name": "OpenLigaDB + API-Football",
            "url": "https://www.openligadb.de/",
        },
    }


# ============================================================
# HAUPTPROGRAMM
# ============================================================

def main():
    print(
        "Starte kostenlose Multi-Team "
        "Player-Intelligence-Recherche..."
    )

    data = load_intelligence()

    if not data:
        data = {}

    # --------------------------------------------------------
    # Grundstruktur
    # --------------------------------------------------------

    data.setdefault("players", {})
    data.setdefault("teams", {})

    # --------------------------------------------------------
    # Vorhandene Teamnamen übernehmen
    # --------------------------------------------------------

    existing_teams = data.get("teams", {})

    teams_to_process = {}

    for key, default_name in DEFAULT_TEAMS.items():
        existing = existing_teams.get(key, {})

        club_name = existing.get(
            "club",
            default_name
        )

        teams_to_process[key] = club_name

    # --------------------------------------------------------
    # Jeden Verein verarbeiten
    # --------------------------------------------------------

    processed_teams = 0
    processed_players = 0

    for team_key, club_name in teams_to_process.items():

        print()
        print("=" * 60)
        print(f"Verarbeite: {club_name}")
        print("=" * 60)

        # --------------------------------------------
        # API-Football Team finden
        # --------------------------------------------

        api_team = find_api_team(club_name)

        if not api_team:
            print(
                f"  TEAM NICHT GEFUNDEN: {club_name}"
            )
            continue

        api_team_id = api_team.get("id")

        print(
            f"  API-Team gefunden: "
            f"{api_team.get('name')} "
            f"(ID {api_team_id})"
        )

        # --------------------------------------------
        # Nächstes Spiel
        # --------------------------------------------

        next_match = get_next_match(club_name)

        if next_match:
            print(
                f"  Nächstes Spiel: "
                f"{next_match['opponent']} "
                f"({next_match['homeAway']})"
            )
        else:
            print(
                "  Kein nächstes Spiel gefunden."
            )

        # --------------------------------------------
        # Kader
        # --------------------------------------------

        try:
            squad = get_team_squad(api_team_id)
        except Exception as error:
            print(
                f"  Kader konnte nicht geladen werden: "
                f"{error}"
            )
            squad = []

        print(
            f"  Kader: {len(squad)} Spieler"
        )

        # --------------------------------------------
        # Verletzungen / Sperren
        # --------------------------------------------

        injuries = get_team_injuries(api_team_id)

        print(
            f"  Verletzungs-/Sperrmeldungen: "
            f"{len(injuries)}"
        )

        # --------------------------------------------
        # Spieler verarbeiten
        # --------------------------------------------

        team_player_count = 0

        for player in squad:

            player_id = player.get("id")
            player_name = player.get(
                "name",
                "Unbekannter Spieler"
            )

            if not player_id:
                continue

            status = find_player_status(
                player_id,
                injuries
            )

            intelligence = build_player_intelligence(
                player,
                club_name,
                next_match,
                status
            )

            key = player_key(
                f"{club_name}_{player_name}"
            )

            data["players"][key] = intelligence

            team_player_count += 1
            processed_players += 1

        # --------------------------------------------
        # Team speichern
        # --------------------------------------------

        data["teams"][team_key] = build_team_entry(
            team_key,
            club_name,
            api_team,
            next_match,
            team_player_count
        )

        processed_teams += 1

        print(
            f"  {team_player_count} Spieler gespeichert."
        )

    # --------------------------------------------------------
    # Metadaten
    # --------------------------------------------------------

    data["lastUpdated"] = now_date()
    data["league"] = "Bundesliga"
    data["season"] = CURRENT_SEASON
    data["teamCount"] = len(data["teams"])
    data["playerCount"] = len(data["players"])

    # --------------------------------------------------------
    # Sicherheitsprüfung
    # --------------------------------------------------------

    if processed_teams == 0:
        raise RuntimeError(
            "Kein einziger Verein konnte aktualisiert werden."
        )

    if processed_players == 0:
        raise RuntimeError(
            "Kein einziger Spieler konnte geladen werden."
        )

    # --------------------------------------------------------
    # Speichern
    # --------------------------------------------------------

    save_intelligence(data)

    print()
    print("=" * 60)
    print("ERFOLGREICH ABGESCHLOSSEN")
    print("=" * 60)
    print(
        f"Teams aktualisiert: {processed_teams}"
    )
    print(
        f"Spieler aktualisiert: {processed_players}"
    )
    print(
        f"Gesamt Teams in JSON: {len(data['teams'])}"
    )
    print(
        f"Gesamt Spieler in JSON: {len(data['players'])}"
    )
    print(
        f"Datei: {INTELLIGENCE_FILE}"
    )


if __name__ == "__main__":
    main()
