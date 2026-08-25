import json
import os
import re
import unicodedata
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
BUNDESLIGA_LEAGUE = "bl1"

# Bundesliga-Saison automatisch bestimmen:
# August 2026 -> Saison 2026/27, Januar 2027 -> ebenfalls 2026/27.
now = datetime.now(timezone.utc)
CURRENT_SEASON = now.year if now.month >= 7 else now.year - 1

# Die 18 Teams der Bundesliga 2026/27.
# Die offiziellen Bundesliga-Seiten führen für 2026/27 u. a. Schalke,
# Elversberg und Paderborn als Aufsteiger und nicht Wolfsburg/Heidenheim/St. Pauli.
TEAMS = {
    "bayern": {
        "name": "FC Bayern München",
        "search": "FC Bayern Munich",
    },
    "dortmund": {
        "name": "Borussia Dortmund",
        "search": "Borussia Dortmund",
    },
    "leipzig": {
        "name": "RB Leipzig",
        "search": "RB Leipzig",
    },
    "stuttgart": {
        "name": "VfB Stuttgart",
        "search": "VfB Stuttgart",
    },
    "hoffenheim": {
        "name": "TSG Hoffenheim",
        "search": "TSG Hoffenheim",
    },
    "leverkusen": {
        "name": "Bayer 04 Leverkusen",
        "search": "Bayer Leverkusen",
    },
    "freiburg": {
        "name": "Sport-Club Freiburg",
        "search": "SC Freiburg",
    },
    "frankfurt": {
        "name": "Eintracht Frankfurt",
        "search": "Eintracht Frankfurt",
    },
    "augsburg": {
        "name": "FC Augsburg",
        "search": "FC Augsburg",
    },
    "mainz": {
        "name": "1. FSV Mainz 05",
        "search": "Mainz 05",
    },
    "union_berlin": {
        "name": "1. FC Union Berlin",
        "search": "Union Berlin",
    },
    "monchengladbach": {
        "name": "Borussia Mönchengladbach",
        "search": "Borussia Monchengladbach",
    },
    "hamburg": {
        "name": "Hamburger SV",
        "search": "Hamburg",
    },
    "koln": {
        "name": "1. FC Köln",
        "search": "FC Koln",
    },
    "bremen": {
        "name": "SV Werder Bremen",
        "search": "Werder Bremen",
    },
    "schalke": {
        "name": "FC Schalke 04",
        "search": "Schalke 04",
    },
    "elversberg": {
        "name": "SV Elversberg",
        "search": "Elversberg",
    },
    "paderborn": {
        "name": "SC Paderborn 07",
        "search": "Paderborn",
    },
}

# ============================================================
# ALLGEMEINE HILFSFUNKTIONEN
# ============================================================


def now_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def normalize_name(value):
    """Vergleich von Vereins-/Spielernamen ohne Umlaute/Akzente."""
    value = str(value or "").lower().strip()
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = value.replace("ß", "ss")
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def api_search_query(value):
    """API-Football akzeptiert im search-Feld nur Buchstaben/Ziffern/Leerzeichen."""
    return normalize_name(value)


def player_key(club_name, player_name):
    value = normalize_name(f"{club_name} {player_name}")
    return value.replace(" ", "_")


# ============================================================
# JSON
# ============================================================


def load_intelligence():
    if not INTELLIGENCE_FILE.exists():
        return {}

    try:
        with INTELLIGENCE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (json.JSONDecodeError, OSError):
        return {}


def save_intelligence(data):
    with INTELLIGENCE_FILE.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


# ============================================================
# API-FOOTBALL
# ============================================================


def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY fehlt in den GitHub Secrets.")

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

    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        raise RuntimeError(f"API-Football Anfrage fehlgeschlagen: {error}") from error

    if data.get("errors"):
        raise RuntimeError(f"API-Football Fehler: {data['errors']}")

    return data


def find_api_team(team_name, search_name=None):
    """
    Findet ein API-Football-Team.

    Wichtig: API-Football akzeptiert im search-Parameter keine Umlaute,
    Punkte oder andere Sonderzeichen. Deshalb wird ausschließlich eine
    bereinigte Suchzeichenkette an die API geschickt.
    """
    queries = []

    for candidate in (search_name, team_name):
        query = api_search_query(candidate)
        if query and query not in queries:
            queries.append(query)

    for query in queries:
        data = api_get("teams", {"search": query})
        matches = data.get("response", [])

        if not matches:
            continue

        wanted = normalize_name(team_name)

        # Exakte Namensübereinstimmung bevorzugen.
        for entry in matches:
            team = entry.get("team", {})
            api_name = team.get("name", "")
            if normalize_name(api_name) == wanted:
                return team

        # Häufige API-Namen wie "Bayern Munich" / "FC Bayern Munich".
        # Bei mehreren Treffern bevorzugen wir einen Treffer, dessen Name
        # die Suchbegriffe enthält.
        for entry in matches:
            team = entry.get("team", {})
            api_name = team.get("name", "")
            normalized_api = normalize_name(api_name)
            normalized_query = normalize_name(query)

            if normalized_api and (
                normalized_query in normalized_api
                or normalized_api in normalized_query
            ):
                return team

        # Fallback: erster echter Team-Treffer.
        for entry in matches:
            team = entry.get("team", {})
            if team.get("id") and team.get("name"):
                return team

    return None


def get_team_squad(api_team_id):
    """Aktuellen registrierten Kader holen; ohne season-Parameter."""
    data = api_get("players/squads", {"team": api_team_id})
    response = data.get("response", [])

    if not response:
        return []

    return response[0].get("players", [])


def get_team_injuries(api_team_id):
    """Aktuelle Verletzungen/Sperren; bewusst ohne season-Parameter."""
    try:
        data = api_get("injuries", {"team": api_team_id})
        return data.get("response", [])
    except Exception as error:
        print(f"  Warnung: Verletzungsdaten nicht verfügbar: {error}")
        return []


# ============================================================
# OPENLIGADB
# ============================================================


def openligadb_get(path):
    url = f"{OPENLIGADB_URL}{path}"
    request = Request(url, headers={"Accept": "application/json"})

    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def get_next_match(*team_names):
    """Nächstes Bundesliga-Spiel des Vereins aus OpenLigaDB."""
    try:
        matches = openligadb_get(
            f"/getmatchdata/{BUNDESLIGA_LEAGUE}/{CURRENT_SEASON}"
        )
    except Exception as error:
        print(f"  Warnung: OpenLigaDB nicht erreichbar: {error}")
        return None

    now_utc = datetime.now(timezone.utc)
    wanted_names = [normalize_name(name) for name in team_names if name]
    wanted_names = [name for name in wanted_names if name]
    upcoming = []

    for match in matches:
        match_date = match.get("matchDateTimeUTC")
        if not match_date:
            continue

        try:
            dt = datetime.fromisoformat(match_date.replace("Z", "+00:00"))
        except ValueError:
            continue

        if dt < now_utc:
            continue

        team1 = match.get("team1", {})
        team2 = match.get("team2", {})
        name1 = team1.get("teamName", "")
        name2 = team2.get("teamName", "")

        normalized_name1 = normalize_name(name1)
        normalized_name2 = normalize_name(name2)

        def matches_any(candidate):
            return any(
                wanted == candidate
                or wanted in candidate
                or candidate in wanted
                for wanted in wanted_names
            )

        if matches_any(normalized_name1) or matches_any(normalized_name2):
            upcoming.append(match)

    if not upcoming:
        return None

    upcoming.sort(key=lambda item: item.get("matchDateTimeUTC", ""))
    match = upcoming[0]

    team1 = match.get("team1", {})
    team2 = match.get("team2", {})
    name1 = team1.get("teamName", "")

    if any(
        wanted == normalize_name(name1)
        or wanted in normalize_name(name1)
        or normalize_name(name1) in wanted
        for wanted in wanted_names
    ):
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
# VERLETZUNG / SPERRE
# ============================================================


def find_player_status(player_id, injuries):
    injury = None
    suspension = None

    for item in injuries:
        player = item.get("player", {})

        if player.get("id") != player_id:
            continue

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

        if "susp" in str(type_name).lower():
            suspension = reason
        else:
            injury = reason

    return {
        "injury": injury,
        "suspension": suspension,
    }


def recommendation(injury, suspension):
    if injury or suspension:
        return "Nicht aufstellen"
    return "Beobachten"


# ============================================================
# SPIELER-INTELLIGENCE
# ============================================================


def build_player_intelligence(player, club_name, next_match, status):
    player_id = player.get("id")
    name = player.get("name", "Unbekannter Spieler")
    position = player.get("position")
    number = player.get("number")

    injury = status.get("injury")
    suspension = status.get("suspension")

    if injury or suspension:
        starting = "Unwahrscheinlich"
    else:
        starting = None

    return {
        "id": player_id,
        "name": name,
        "club": club_name,
        "position": position,
        "number": number,
        "starting": starting,
        "form": None,
        "opponent": next_match.get("opponent") if next_match else None,
        "homeAway": next_match.get("homeAway") if next_match else None,
        "injury": injury or "Keine aktuelle Verletzungsmeldung gefunden",
        "suspension": suspension or "Keine aktuelle Sperrmeldung gefunden",
        "recommendation": recommendation(injury, suspension),
        "sources": [
            {
                "title": "API-Football",
                "url": "https://www.api-football.com/",
                "date": now_date(),
            }
        ],
        "lastUpdated": now_date(),
        "confidence": {
            "starting": "niedrig" if starting is None else "hoch",
            "injury": "hoch" if injury else "mittel",
            "suspension": "hoch" if suspension else "mittel",
            "recommendation": "mittel",
        },
    }


def build_team_entry(team_key, club_name, api_team, next_match, player_count):
    return {
        "club": club_name,
        "league": "Bundesliga",
        "season": CURRENT_SEASON,
        "apiFootballTeamId": api_team.get("id") if api_team else None,
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
    print("Starte Bundesliga Multi-Team Player-Intelligence-Recherche...")
    print(f"Saison: {CURRENT_SEASON}/{str(CURRENT_SEASON + 1)[-2:]}")
    print(f"Teams: {len(TEAMS)}")

    data = load_intelligence()
    if not isinstance(data, dict):
        data = {}

    # Wir bauen die Bundesliga-Daten bei jedem Lauf neu auf.
    # Dadurch bleiben keine alten Spieler aus vorherigen Kadern übrig.
    new_players = {}
    new_teams = {}

    processed_teams = 0
    processed_players = 0

    for team_key, config in TEAMS.items():
        club_name = config["name"]
        search_name = config["search"]

        print()
        print("=" * 60)
        print(f"Verarbeite: {club_name}")
        print("=" * 60)

        # --------------------------------------------------------
        # API-Football-Team finden
        # --------------------------------------------------------
        try:
            api_team = find_api_team(club_name, search_name)
        except Exception as error:
            print(f"  API-Team-Suche fehlgeschlagen: {error}")
            continue

        if not api_team:
            print(f"  TEAM NICHT GEFUNDEN: {club_name}")
            continue

        api_team_id = api_team.get("id")
        print(
            f"  API-Team gefunden: {api_team.get('name')} "
            f"(ID {api_team_id})"
        )

        # --------------------------------------------------------
        # Nächstes Spiel
        # --------------------------------------------------------
        next_match = get_next_match(
            club_name,
            api_team.get("name"),
            search_name,
        )
        if next_match:
            print(
                f"  Nächstes Spiel: {next_match['opponent']} "
                f"({next_match['homeAway']})"
            )
        else:
            print("  Kein nächstes Spiel gefunden.")

        # --------------------------------------------------------
        # Kader
        # --------------------------------------------------------
        try:
            squad = get_team_squad(api_team_id)
        except Exception as error:
            print(f"  Kader konnte nicht geladen werden: {error}")
            squad = []

        print(f"  Kader: {len(squad)} Spieler")

        # --------------------------------------------------------
        # Verletzungen / Sperren
        # --------------------------------------------------------
        injuries = get_team_injuries(api_team_id)
        print(f"  Verletzungs-/Sperrmeldungen: {len(injuries)}")

        # --------------------------------------------------------
        # Spieler speichern
        # --------------------------------------------------------
        team_player_count = 0

        for player in squad:
            player_id = player.get("id")
            player_name = player.get("name", "Unbekannter Spieler")

            if not player_id:
                continue

            status = find_player_status(player_id, injuries)
            intelligence = build_player_intelligence(
                player,
                club_name,
                next_match,
                status,
            )

            key = player_key(club_name, player_name)
            new_players[key] = intelligence
            team_player_count += 1
            processed_players += 1

        new_teams[team_key] = build_team_entry(
            team_key,
            club_name,
            api_team,
            next_match,
            team_player_count,
        )

        processed_teams += 1
        print(f"  {team_player_count} Spieler gespeichert.")

    # ------------------------------------------------------------
    # Sicherheitsprüfung
    # ------------------------------------------------------------
    if processed_teams == 0:
        raise RuntimeError("Kein einziger Verein konnte aktualisiert werden.")

    if processed_players == 0:
        raise RuntimeError("Kein einziger Spieler konnte geladen werden.")

    # ------------------------------------------------------------
    # JSON schreiben
    # ------------------------------------------------------------
    output = {
        "players": new_players,
        "teams": new_teams,
        "lastUpdated": now_date(),
        "league": "Bundesliga",
        "season": CURRENT_SEASON,
        "teamCount": len(new_teams),
        "playerCount": len(new_players),
    }

    save_intelligence(output)

    print()
    print("=" * 60)
    print("ERFOLGREICH ABGESCHLOSSEN")
    print("=" * 60)
    print(f"Teams aktualisiert: {processed_teams}/{len(TEAMS)}")
    print(f"Spieler aktualisiert: {processed_players}")
    print(f"Gesamt Teams in JSON: {len(new_teams)}")
    print(f"Gesamt Spieler in JSON: {len(new_players)}")
    print(f"Datei: {INTELLIGENCE_FILE}")


if __name__ == "__main__":
    main()
