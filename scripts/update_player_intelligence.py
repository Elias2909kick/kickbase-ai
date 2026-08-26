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

OPENLIGADB_URL = "https://api.openligadb.de"
API_FOOTBALL_URL = "https://v3.football.api-sports.io"
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY")

BUNDESLIGA_SHORTCUT = "bl1"
CURRENT_SEASON = 2026  # Bundesliga 2026/27

BUNDESLIGA_TEAMS = [
    "1. FC Köln",
    "Bayer Leverkusen",
    "FC Bayern München",
    "Borussia Dortmund",
    "Borussia Mönchengladbach",
    "Eintracht Frankfurt",
    "FC Augsburg",
    "1. FSV Mainz 05",
    "Hamburger SV",
    "RB Leipzig",
    "SC Freiburg",
    "SC Paderborn 07",
    "FC Schalke 04",
    "SV 07 Elversberg",
    "TSG Hoffenheim",
    "1. FC Union Berlin",
    "VfB Stuttgart",
    "SV Werder Bremen",
]
# ============================================================
# HTTP
# ============================================================


def http_get_json(url, headers=None):
    headers = headers or {}

    request = Request(
        url,
        headers={
            "User-Agent": "kickbase-ai/1.0",
            "Accept": "application/json",
            **headers,
        },
    )

    with urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8")
        return json.loads(body)


# ============================================================
# OPENLIGADB
# ============================================================


def openligadb_get(path):
    url = f"{OPENLIGADB_URL}{path}"

    try:
        return http_get_json(url)
    except Exception as exc:
        raise RuntimeError(
            f"OpenLigaDB Fehler bei {url}: {exc}"
        ) from exc


def get_bundesliga_teams():
    """
    Liefert exakt die 18 festgelegten Bundesliga-Mannschaften.
    Die Mannschaftsnamen werden nicht mehr von OpenLigaDB übernommen.
    """

    return [
        {"teamName": team_name}
        for team_name in BUNDESLIGA_TEAMS
    ]


def get_bundesliga_matches():
    """
    Holt den kompletten Spielplan der Bundesliga 2026/27.
    """

    matches = openligadb_get(
        f"/getmatchdata/{BUNDESLIGA_SHORTCUT}/{CURRENT_SEASON}"
    )

    if not matches:
        return []

    return matches


def get_next_match_for_team(team_name, matches):
    """
    Findet das nächste noch nicht gestartete Spiel eines Vereins.
    """

    now = datetime.now(timezone.utc)

    upcoming = []

    for match in matches:
        match_date = match.get("matchDateTimeUTC")

        if not match_date:
            match_date = match.get("matchDateTime")

        if not match_date:
            continue

        try:
            if match_date.endswith("Z"):
                dt = datetime.fromisoformat(
                    match_date.replace("Z", "+00:00")
                )
            else:
                dt = datetime.fromisoformat(match_date)

                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)

        except ValueError:
            continue

        team1 = match.get("team1", {})
        team2 = match.get("team2", {})

        name1 = team1.get("teamName", "")
        name2 = team2.get("teamName", "")

        if team_name.lower() not in (
            name1.lower(),
            name2.lower(),
        ):
            continue

        if dt < now:
            continue

        upcoming.append((dt, match))

    if not upcoming:
        return None

    upcoming.sort(key=lambda item: item[0])

    return upcoming[0][1]


# ============================================================
# API-FOOTBALL
# ============================================================


def api_football_get(endpoint, params=None):
    if not API_FOOTBALL_KEY:
        return None

    params = params or {}

    query = urlencode(params)

    url = f"{API_FOOTBALL_URL}/{endpoint}"

    if query:
        url += f"?{query}"

    try:
        data = http_get_json(
            url,
            headers={
                "x-apisports-key": API_FOOTBALL_KEY
            },
        )
    except Exception as exc:
        print(f"API-Football Anfrage fehlgeschlagen: {exc}")
        return None

    errors = data.get("errors")

    if errors:
        print(f"API-Football Fehler: {errors}")
        return None

    return data


# ============================================================
# NAMEN
# ============================================================


def normalize_name(value):
    """
    Macht Vereinsnamen vergleichbarer.

    Beispiel:
    FC Bayern München
    -> fc bayern munchen
    """

    value = value or ""

    value = unicodedata.normalize(
        "NFKD",
        value
    )

    value = "".join(
        char
        for char in value
        if not unicodedata.combining(char)
    )

    value = value.lower()

    value = re.sub(
        r"[^a-z0-9 ]+",
        " ",
        value
    )

    value = re.sub(
        r"\s+",
        " ",
        value
    )

    return value.strip()


def names_match(a, b):
    a = normalize_name(a)
    b = normalize_name(b)

    if not a or not b:
        return False

    if a == b:
        return True

    if a in b or b in a:
        return True

    return False


# ============================================================
# API-FOOTBALL TEAM FINDEN
# ============================================================


def find_api_football_team(team_name):
    """
    Sucht einen Verein bei API-Football.

    Wichtig:
    Wir verwenden NICHT league + season.

    Dadurch umgehen wir die Free-Plan-Saisonbeschränkung.
    """

    if not API_FOOTBALL_KEY:
        return None

    # Mehrere Suchvarianten verwenden, weil API-Football
    # manche deutschen Vereinsnamen anders führt.
    search_names = [
        normalize_name(team_name)
    ]

    aliases = {
        "1 fc koln": ["koln", "fc koln"],
        "borussia monchengladbach": ["monchengladbach", "borussia monchengladbach"],
        "eintracht frankfurt": ["frankfurt"],
        "fc bayern munchen": ["bayern munich", "bayern"],
        "fc schalke 04": ["schalke 04", "schalke"],
        "tsg hoffenheim": ["hoffenheim"],
        "sc paderborn 07": ["paderborn"],
        "sv elversberg": ["elversberg"],
        "hamburger sv": ["hamburger sv", "hamburg"],
        "rb leipzig": ["rb leipzig", "leipzig"],
        "sc freiburg": ["freiburg"],
        "1 fsv mainz 05": ["mainz", "mainz 05"],
        "fc augsburg": ["augsburg"],
        "vfb stuttgart": ["stuttgart"],
        "sv werder bremen": ["werder bremen", "werder"],
        "1 fc union berlin": ["union berlin"],
    }

    search_names.extend(
        aliases.get(
            normalize_name(team_name),
            []
        )
    )

    # Doppelte Suchbegriffe entfernen
    search_names = list(dict.fromkeys(search_names))

    print(
        f"API-Suche für '{team_name}': {search_names}"
    )

candidates = []

for search_name in search_names:
    if len(search_name) < 3:
        continue

    data = api_football_get(
        "teams",
        {
            "search": search_name
        }
    )

    if not data:
        continue

    candidates = data.get("response", [])

    if candidates:
        print(f"Treffer mit '{search_name}'")
        break

    if not candidates:
        return None    

    # Erst exakten Namen suchen
    for entry in candidates:
        team = entry.get("team", {})

        api_name = team.get("name", "")

        if names_match(team_name, api_name):
            return team

    # Danach vorsichtig über Namensbestandteile
    target_words = set(
        normalize_name(team_name).split()
    )

    best_team = None
    best_score = 0

    for entry in candidates:
        team = entry.get("team", {})

        api_name = team.get("name", "")

        api_words = set(
            normalize_name(api_name).split()
        )

        score = len(
            target_words.intersection(api_words)
        )

        if score > best_score:
            best_score = score
            best_team = team

    return best_team


# ============================================================
# AKTUELLER KADER
# ============================================================


def get_current_squad(api_team_id):
    """
    Holt den aktuellen Kader über:

    /players/squads?team=ID

    Dieser Endpoint benötigt keine Saison.
    """

    if not api_team_id:
        return []

    data = api_football_get(
        "players/squads",
        {
            "team": api_team_id
        }
    )

    if not data:
        return []

    response = data.get("response", [])

    if not response:
        return []

    squad = []

    for entry in response:
        players = entry.get("players", [])

        for player in players:
            squad.append(
                {
                    "id": player.get("id"),
                    "name": player.get("name"),
                    "age": player.get("age"),
                    "number": player.get("number"),
                    "position": player.get("position"),
                    "photo": player.get("photo"),
                }
            )

    return squad


# ============================================================
# VERLETZUNGEN
# ============================================================


def get_current_injuries(api_team_id):
    """
    Holt aktuelle Verletzungsdaten.

    Wir verwenden absichtlich KEINEN Saisonparameter.
    """

    if not api_team_id:
        return []

    data = api_football_get(
        "injuries",
        {
            "team": api_team_id
        }
    )

    if not data:
        return []

    return data.get("response", [])


# ============================================================
# SPIELER-INTELLIGENCE
# ============================================================


def build_player_intelligence(
    team,
    api_team,
    squad,
    injuries,
    next_match,
):
    team_name = team.get("teamName", "")

    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d"
    )

    injury_map = {}

    for injury in injuries:
        player = injury.get("player", {})

        player_id = player.get("id")

        if player_id:
            injury_map[player_id] = injury

    players = []

    for player in squad:
        player_id = player.get("id")

        injury = injury_map.get(player_id)

        injury_data = None

        if injury:
            injury_data = {
                "type": injury.get("injury", {}).get("type"),
                "reason": injury.get("injury", {}).get("reason"),
                "date": injury.get("fixture", {}).get("date"),
            }

        players.append(
            {
                "id": player_id,
                "name": player.get("name"),
                "age": player.get("age"),
                "number": player.get("number"),
                "position": player.get("position"),
                "photo": player.get("photo"),
                "injury": injury_data,
            }
        )

    result = {
        "club": team_name,
        "season": "2026/27",
        "league": "1. Bundesliga",
        "average": None,
        "starting": None,
        "form": None,
        "opponent": None,
        "homeAway": None,
        "injury": None,
        "suspension": None,
        "recommendation": None,
        "players": players,
        "sources": [
            {
                "title": "OpenLigaDB",
                "url": "https://www.openligadb.de/",
                "date": now,
            },
            {
                "title": "API-Football",
                "url": "https://www.api-football.com/",
                "date": now,
            },
        ],
        "lastUpdated": now,
        "confidence": {
            "squad": "mittel",
            "injury": "mittel",
            "starting": "niedrig",
            "recommendation": "niedrig",
        },
        "intelligence": {
            "opponent": None,
            "homeAway": None,
        },
    }

    # --------------------------------------------------------
    # NÄCHSTES SPIEL
    # --------------------------------------------------------

    if next_match:
        team1 = next_match.get(
            "team1",
            {}
        )

        team2 = next_match.get(
            "team2",
            {}
        )

        name1 = team1.get(
            "teamName",
            ""
        )

        name2 = team2.get(
            "teamName",
            ""
        )

        if names_match(
            team_name,
            name1
        ):
            opponent = name2
            home_away = "Heim"

        else:
            opponent = name1
            home_away = "Auswärts"

        result["opponent"] = opponent
        result["homeAway"] = home_away

        result["intelligence"]["opponent"] = opponent
        result["intelligence"]["homeAway"] = home_away

        result["recommendation"] = (
            "Nächstes Spiel vorhanden"
        )

    else:
        result["recommendation"] = (
            "Kein kommendes Spiel gefunden"
        )

    # --------------------------------------------------------
    # VERLETZUNGEN
    # --------------------------------------------------------

    injured_players = [
        player
        for player in players
        if player.get("injury")
    ]

    if injured_players:
        result["injury"] = (
            f"{len(injured_players)} Spieler "
            "mit aktueller Verletzung"
        )
    else:
        result["injury"] = (
            "Keine aktuelle Verletzungsmeldung gefunden"
        )

    return result


# ============================================================
# JSON LADEN / SPEICHERN
# ============================================================


def load_intelligence():
    if not INTELLIGENCE_FILE.exists():
        return {}

    try:
        with INTELLIGENCE_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:
            return json.load(file)

    except json.JSONDecodeError:
        print(
            "WARNUNG: player-intelligence.json "
            "ist kein gültiges JSON."
        )
        return {}


def save_intelligence(data):
    with INTELLIGENCE_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )

        file.write("\n")


# ============================================================
# HAUPTPROGRAMM
# ============================================================


def main():
    print(
        "Starte Bundesliga Player-Intelligence..."
    )

    print(
        f"Saison: {CURRENT_SEASON}/{str(CURRENT_SEASON + 1)[-2:]}"
    )

    print(
        "Liga: 1. Bundesliga"
    )

    # --------------------------------------------------------
    # BUNDESLIGA-TEAMS
    # --------------------------------------------------------

    teams = get_bundesliga_teams()

    print(
        f"{len(teams)} Bundesliga-Teams gefunden."
    )

    if len(teams) != 18:
        print(
            f"WARNUNG: Erwartet wurden 18 Teams, "
            f"gefunden wurden {len(teams)}."
        )

    # --------------------------------------------------------
    # SPIELPLAN
    # --------------------------------------------------------

    print(
        "Lade Bundesliga-Spielplan..."
    )

    matches = get_bundesliga_matches()

    print(
        f"{len(matches)} Spiele geladen."
    )

    # --------------------------------------------------------
    # ALTE DATEN LADEN
    # --------------------------------------------------------

# Neue Datenbasis für diesen Lauf aufbauen.
# Dadurch bleiben keine alten/falschen Teams aus vorherigen Läufen erhalten.
    data = {
        "teams": {}
    }

    # --------------------------------------------------------
    # ALLE 18 TEAMS
    # --------------------------------------------------------

    updated_teams = 0
    
    for team in teams:
        team_name = team.get(
            "teamName",
            ""
        )

        if not team_name:
            continue

        print("")
        print("=" * 60)
        print(
            f"Verarbeite: {team_name}"
        )
        print("=" * 60)

        # ----------------------------------------------------
        # API-FOOTBALL TEAM
        # ----------------------------------------------------

        api_team = find_api_football_team(
            team_name
        )

        if api_team:
            api_team_id = api_team.get(
                "id"
            )

            print(
                f"API-Football Team: "
                f"{api_team.get('name')} "
                f"(ID {api_team_id})"
            )

        else:
            api_team_id = None

            print(
                "API-Football Team nicht gefunden."
            )

        # ----------------------------------------------------
        # KADER
        # ----------------------------------------------------

        squad = []

        if api_team_id:
            squad = get_current_squad(
                api_team_id
            )

        print(
            f"Kader: {len(squad)} Spieler"
        )

        # ----------------------------------------------------
        # VERLETZUNGEN
        # ----------------------------------------------------

        injuries = []

        if api_team_id:
            injuries = get_current_injuries(
                api_team_id
            )

        print(
            f"Verletzungsdaten: "
            f"{len(injuries)} Einträge"
        )

        # ----------------------------------------------------
        # NÄCHSTES SPIEL
        # ----------------------------------------------------

        next_match = get_next_match_for_team(
            team_name,
            matches
        )

        if next_match:
            team1 = next_match.get(
                "team1",
                {}
            )

            team2 = next_match.get(
                "team2",
                {}
            )

            print(
                "Nächstes Spiel: "
                f"{team1.get('teamName')} "
                "vs. "
                f"{team2.get('teamName')}"
            )

        else:
            print(
                "Kein kommendes Spiel gefunden."
            )

        # ----------------------------------------------------
        # INTELLIGENCE
        # ----------------------------------------------------

        intelligence = build_player_intelligence(
            team=team,
            api_team=api_team,
            squad=squad,
            injuries=injuries,
            next_match=next_match,
        )

        # ----------------------------------------------------
        # SPEICHERN
        # ----------------------------------------------------

        data["teams"][team_name] = intelligence



        updated_teams += 1

        print(
            f"OK: {team_name} aktualisiert."
        )

    # --------------------------------------------------------
    # META
    # --------------------------------------------------------

    data["lastUpdated"] = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    data["season"] = "2026/27"

    data["league"] = "1. Bundesliga"

    data["teamCount"] = len(
        data["teams"]
    )

    # --------------------------------------------------------
    # SPEICHERN
    # --------------------------------------------------------

    save_intelligence(data)

    print("")
    print("=" * 60)
    print(
        f"FERTIG: {updated_teams} Teams aktualisiert."
    )
    print(
        f"Gespeichert in: {INTELLIGENCE_FILE}"
    )
    print("=" * 60)


if __name__ == "__main__":
    main()
