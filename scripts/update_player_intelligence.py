import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ============================================================
# KONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"

OPENLIGADB_URL = "https://api.openligadb.de"
API_FOOTBALL_URL = "https://v3.football.api-sports.io"
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")

BUNDESLIGA_SHORTCUT = "bl1"
CURRENT_SEASON = "2026/27"
OPENLIGADB_SEASON = 2026

# API-Football Free Plan:
# maximal ein Request etwa alle 6.5 Sekunden.
# Dadurch vermeiden wir HTTP 429.
API_MIN_INTERVAL = 6.5
_last_api_call = 0.0

# ============================================================
# DIE EXAKTEN 18 BUNDESLIGA-TEAMS
# ============================================================

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
    "SV Elversberg",
    "TSG Hoffenheim",
    "1. FC Union Berlin",
    "VfB Stuttgart",
    "SV Werder Bremen",
]

# API-Football-Namen/Suchvarianten.
# Diese beeinflussen NICHT die Namen in unserer JSON-Datei.
API_SEARCH_ALIASES = {
    "1. FC Köln": [
        "FC Koln",
        "Koln",
        "1 FC Koln",
    ],
    "Bayer Leverkusen": [
        "Bayer 04 Leverkusen",
        "Bayer Leverkusen",
        "Leverkusen",
    ],
    "FC Bayern München": [
        "Bayern Munich",
        "Bayern Munchen",
        "Bayern",
    ],
    "Borussia Dortmund": [
        "Borussia Dortmund",
        "Dortmund",
    ],
    "Borussia Mönchengladbach": [
        "Borussia Monchengladbach",
        "Monchengladbach",
        "Gladbach",
    ],
    "Eintracht Frankfurt": [
        "Eintracht Frankfurt",
        "Frankfurt",
    ],
    "FC Augsburg": [
        "FC Augsburg",
        "Augsburg",
    ],
    "1. FSV Mainz 05": [
        "FSV Mainz 05",
        "Mainz 05",
        "Mainz",
    ],
    "Hamburger SV": [
        "Hamburger SV",
        "Hamburg",
        "HSV",
    ],
    "RB Leipzig": [
        "RB Leipzig",
        "Leipzig",
    ],
    "SC Freiburg": [
        "SC Freiburg",
        "Freiburg",
    ],
    "SC Paderborn 07": [
        "SC Paderborn 07",
        "Paderborn",
    ],
    "FC Schalke 04": [
        "FC Schalke 04",
        "Schalke 04",
        "Schalke",
    ],
    "SV Elversberg": [
        "SV Elversberg",
        "Elversberg",
    ],
    "TSG Hoffenheim": [
        "TSG Hoffenheim",
        "Hoffenheim",
    ],
    "1. FC Union Berlin": [
        "1. FC Union Berlin",
        "Union Berlin",
    ],
    "VfB Stuttgart": [
        "VfB Stuttgart",
        "Stuttgart",
    ],
    "SV Werder Bremen": [
        "SV Werder Bremen",
        "Werder Bremen",
        "Werder",
    ],
}


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
    Wir verwenden bewusst die feste Liste.
    Dadurch kommen exakt 18 Teams in die JSON-Datei.
    """

    return [
        {
            "teamName": team_name
        }
        for team_name in BUNDESLIGA_TEAMS
    ]


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
# API-FOOTBALL
# ============================================================

def api_football_get(
    endpoint,
    params=None,
):
    """
    Sicherer API-Football-Request.

    Wichtig:
    - mindestens 6.5 Sekunden zwischen Requests
    - HTTP 429 wird nicht aggressiv wiederholt
    - kein pip/requests nötig
    """

    global _last_api_call

    if not API_FOOTBALL_KEY:
        print(
            "WARNUNG: "
            "API_FOOTBALL_KEY fehlt."
        )
        return None

    params = params or {}

    elapsed = (
        time.monotonic()
        - _last_api_call
    )

    if elapsed < API_MIN_INTERVAL:

        wait = (
            API_MIN_INTERVAL
            - elapsed
        )

        print(
            "API-Football Pause: "
            f"{wait:.1f}s"
        )

        time.sleep(wait)

    query = urlencode(params)

    url = (
        f"{API_FOOTBALL_URL}/"
        f"{endpoint.lstrip('/')}"
    )

    if query:
        url += f"?{query}"

    try:

        _last_api_call = (
            time.monotonic()
        )

        data = http_get_json(
            url,
            headers={
                "x-apisports-key":
                    API_FOOTBALL_KEY
            },
        )

    except HTTPError as exc:

        if exc.code == 429:
            print(
                "API-Football HTTP 429: "
                "Rate Limit erreicht."
            )
        else:
            print(
                f"API-Football HTTP "
                f"{exc.code}: {exc}"
            )

        return None

    except (
        URLError,
        TimeoutError,
        OSError,
    ) as exc:

        print(
            "API-Football "
            f"Netzwerkfehler: {exc}"
        )

        return None

    except Exception as exc:

        print(
            f"API-Football Fehler: "
            f"{exc}"
        )

        return None

    errors = data.get(
        "errors"
    )

    if errors:
        print(
            f"API-Football Fehler: "
            f"{errors}"
        )
        return None

    return data


def find_api_football_team(
    team_name,
):
    """
    Sucht einen Verein bei API-Football.

    Diese Suche wird nur gebraucht, wenn noch
    keine API-Team-ID im vorherigen JSON gespeichert ist.
    """

    aliases = API_SEARCH_ALIASES.get(
        team_name,
        [team_name],
    )

    search_names = []

    for alias in aliases:

        normalized = normalize_name(
            alias
        )

        if (
            normalized
            and normalized not in search_names
        ):
            search_names.append(
                normalized
            )

    print(
        f"API-Suche für "
        f"'{team_name}': "
        f"{search_names}"
    )

    for search_name in search_names:

        data = api_football_get(
            "teams",
            {
                "search": search_name
            },
        )

        if not data:
            continue

        candidates = data.get(
            "response",
            [],
        )

        if not candidates:
            continue

        target = normalize_name(
            team_name
        )

        target_words = set(
            target.split()
        )

        best_team = None
        best_score = -1

        for entry in candidates:

            team = entry.get(
                "team",
                {},
            )

            api_name = team.get(
                "name",
                "",
            )

            if not api_name:
                continue
# Frauenmannschaften ausschließen
api_name_normalized = normalize_name(api_name)

if (
    api_name_normalized.endswith(" w")
    or "women" in api_name_normalized.split()
    or "femenino" in api_name_normalized.split()
):
    continue
api_normalized = (
    normalize_name(
        api_name
    )
)

api_words = set(
    api_normalized.split()
)

score = len(
    target_words
    & api_words
)

if api_normalized == target:
    score += 100

if (
    target
    in api_normalized
):
    score += 20

if (
    api_normalized
    in target
):
    score += 20

if (
    api_normalized
    == normalize_name(
        search_name
    )
):
    score += 50

if score > best_score:
    best_score = score
    best_team = team

        if best_team:

            print(
                "API-Football Team: "
                f"{best_team.get('name')} "
                f"(ID "
                f"{best_team.get('id')})"
            )

            return best_team

    print(
        "API-Football Team "
        "nicht gefunden."
    )

    return None


def get_current_squad(
    api_team_id,
):
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
        },
    )

    if not data:

        print(
            "Keine Kader-Daten erhalten."
        )

        return []

    response = data.get(
        "response",
        [],
    )

    if not response:

        print(
            "API-Football liefert "
            "keinen Kader."
        )

        return []

    squad = []

    for entry in response:

        players = entry.get(
            "players",
            [],
        )

        if not players:
            continue

        for player in players:

            if not isinstance(
                player,
                dict,
            ):
                continue

            player_id = player.get(
                "id"
            )

            name = player.get(
                "name"
            )

            if not player_id or not name:
                continue

            squad.append(
                {
                    "id": player_id,
                    "name": name,
                    "age": player.get(
                        "age"
                    ),
                    "number": player.get(
                        "number"
                    ),
                    "position": player.get(
                        "position"
                    ),
                    "photo": player.get(
                        "photo"
                    ),
                    "injury": None,
                }
            )

    print(
        "Kader geladen: "
        f"{len(squad)} Spieler"
    )

    return squad


# ============================================================
# ALTE JSON LADEN
# ============================================================

def load_old_data():
    """
    Liest die alte JSON-Datei.

    Wichtig:
    Bereits gefundene API-Team-IDs werden wiederverwendet.
    Dadurch müssen wir die Team-Suche nicht täglich
    erneut ausführen.
    """

    if not INTELLIGENCE_FILE.exists():
        return {}

    try:

        with INTELLIGENCE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(file)

        return data.get(
            "teams",
            {},
        )

    except Exception as exc:

        print(
            "Alte player-intelligence.json "
            "konnte nicht geladen werden: "
            f"{exc}"
        )

        return {}


# ============================================================
# INTELLIGENCE
# ============================================================

def build_intelligence(
    squad,
    next_match,
):
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
        "starting": None,
        "form": None,
        "opponent": opponent,
        "homeAway": home_away,
        "injury": (
            "Keine aktuelle "
            "Verletzungsmeldung gefunden"
        ),
        "suspension": None,
        "recommendation": recommendation,
        "players": squad,
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

    if not API_FOOTBALL_KEY:

        raise RuntimeError(
            "API_FOOTBALL_KEY "
            "ist nicht gesetzt."
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

    old_teams = load_old_data()

    # Neue, saubere Datenbank.
    data = {
        "teams": {}
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

        # ----------------------------------------------------
        # API-TEAM-ID
        # ----------------------------------------------------

        old_entry = old_teams.get(
            team_name,
            {},
        )

        api_team_id = old_entry.get(
            "apiTeamId"
        )

        api_team_name = old_entry.get(
            "apiTeamName"
        )

        if api_team_id:

            print(
                "Gespeicherte "
                "API-Team-ID: "
                f"{api_team_id}"
            )

        else:

            api_team = (
                find_api_football_team(
                    team_name
                )
            )

            if api_team:

                api_team_id = (
                    api_team.get("id")
                )

                api_team_name = (
                    api_team.get("name")
                )

            else:

                api_team_id = None
                api_team_name = None

        # ----------------------------------------------------
        # KADER
        # ----------------------------------------------------

        squad = []

        if api_team_id:

            squad = get_current_squad(
                api_team_id
            )

        print(
            f"Kader: "
            f"{len(squad)} Spieler"
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

            "nextMatch": next_match,

            "sources": [
                {
                    "title": "OpenLigaDB",
                    "url": (
                        "https://www.openligadb.de/"
                    ),
                },
                {
                    "title": "API-Football",
                    "url": (
                        "https://www.api-football.com/"
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

    # ========================================================
    # PRÜFUNG
    # ========================================================

    if len(data["teams"]) != 18:

        raise RuntimeError(
            "FEHLER: JSON enthält "
            f"{len(data['teams'])} "
            "Teams statt 18."
        )

    missing_api_ids = []
    missing_squads = []

    for name, entry in (
        data["teams"].items()
    ):

        if not entry.get(
            "apiTeamId"
        ):
            missing_api_ids.append(
                name
            )

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
        "Teams mit API-Team-ID: "
        f"{18 - len(missing_api_ids)}/18"
    )

    print(
        "Teams mit Kader: "
        f"{18 - len(missing_squads)}/18"
    )

    if missing_api_ids:

        print()
        print(
            "Teams ohne API-Team-ID:"
        )

        for name in missing_api_ids:
            print(
                f"  - {name}"
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
        f"{updated_teams} Teams "
        "aktualisiert."
    )


if __name__ == "__main__":
    main()
