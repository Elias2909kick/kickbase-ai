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

# Saison 2026/27
CURRENT_SEASON = 2026

# Bundesliga
LEAGUE_SHORTCUT = "bl1"


# ============================================================
# DIE 18 BUNDESLIGA-MANNSCHAFTEN 2026/27
# ============================================================

BUNDESLIGA_TEAMS = [
    {
        "key": "bayern",
        "name": "FC Bayern München",
        "aliases": ["FC Bayern München", "Bayern München"],
    },
    {
        "key": "dortmund",
        "name": "Borussia Dortmund",
        "aliases": ["Borussia Dortmund", "Dortmund"],
    },
    {
        "key": "leipzig",
        "name": "RB Leipzig",
        "aliases": ["RB Leipzig", "Leipzig"],
    },
    {
        "key": "stuttgart",
        "name": "VfB Stuttgart",
        "aliases": ["VfB Stuttgart", "Stuttgart"],
    },
    {
        "key": "hoffenheim",
        "name": "TSG Hoffenheim",
        "aliases": ["TSG Hoffenheim", "Hoffenheim"],
    },
    {
        "key": "leverkusen",
        "name": "Bayer 04 Leverkusen",
        "aliases": ["Bayer 04 Leverkusen", "Bayer Leverkusen", "Leverkusen"],
    },
    {
        "key": "freiburg",
        "name": "Sport-Club Freiburg",
        "aliases": ["Sport-Club Freiburg", "SC Freiburg", "Freiburg"],
    },
    {
        "key": "frankfurt",
        "name": "Eintracht Frankfurt",
        "aliases": ["Eintracht Frankfurt", "Frankfurt"],
    },
    {
        "key": "augsburg",
        "name": "FC Augsburg",
        "aliases": ["FC Augsburg", "Augsburg"],
    },
    {
        "key": "mainz",
        "name": "1. FSV Mainz 05",
        "aliases": ["1. FSV Mainz 05", "Mainz 05", "Mainz"],
    },
    {
        "key": "union_berlin",
        "name": "1. FC Union Berlin",
        "aliases": ["1. FC Union Berlin", "Union Berlin"],
    },
    {
        "key": "monchengladbach",
        "name": "Borussia Mönchengladbach",
        "aliases": [
            "Borussia Mönchengladbach",
            "Borussia M'gladbach",
            "Mönchengladbach",
            "Gladbach",
        ],
    },
    {
        "key": "hamburg",
        "name": "Hamburger SV",
        "aliases": ["Hamburger SV", "Hamburg"],
    },
    {
        "key": "koln",
        "name": "1. FC Köln",
        "aliases": ["1. FC Köln", "FC Köln", "Köln"],
    },
    {
        "key": "bremen",
        "name": "SV Werder Bremen",
        "aliases": ["SV Werder Bremen", "Werder Bremen", "Bremen"],
    },
    {
        "key": "schalke",
        "name": "FC Schalke 04",
        "aliases": ["FC Schalke 04", "Schalke"],
    },
    {
        "key": "elversberg",
        "name": "SV Elversberg",
        "aliases": ["SV Elversberg", "Elversberg"],
    },
    {
        "key": "paderborn",
        "name": "SC Paderborn 07",
        "aliases": ["SC Paderborn 07", "SC Paderborn", "Paderborn"],
    },
]


# ============================================================
# DATUM
# ============================================================

def today_string():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_utc():
    return datetime.now(timezone.utc)


# ============================================================
# OPENLIGADB
# ============================================================

def openligadb_get(path):
    """
    Kostenloser Zugriff auf OpenLigaDB.

    Keine API-ID.
    Kein API-Key.
    Kein kostenpflichtiger Dienst.
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


def load_all_bundesliga_matches():
    """
    Holt ALLE Spiele der Bundesliga-Saison 2026/27
    mit genau EINEM API-Aufruf.
    """

    print(
        f"Lade Bundesliga-Spielplan "
        f"{CURRENT_SEASON}/{CURRENT_SEASON + 1} ..."
    )

    path = (
        f"/getmatchdata/"
        f"{LEAGUE_SHORTCUT}/"
        f"{CURRENT_SEASON}"
    )

    matches = openligadb_get(path)

    if not isinstance(matches, list):
        print("OpenLigaDB hat keine gültige Match-Liste geliefert.")
        return []

    print(
        f"OpenLigaDB: {len(matches)} Spiele erhalten."
    )

    return matches


# ============================================================
# JSON
# ============================================================

def load_intelligence():
    if not INTELLIGENCE_FILE.exists():
        return {}

    try:
        with INTELLIGENCE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        if isinstance(data, dict):
            return data

        return {}

    except Exception as error:
        print(
            f"Fehler beim Lesen der JSON-Datei: {error}"
        )

        return {}


def save_intelligence(data):
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

        file.write("\n")


# ============================================================
# TEAM-HILFSFUNKTIONEN
# ============================================================

def normalize_name(name):
    """
    Vereinheitlicht Namen für den Vergleich.
    """

    if not name:
        return ""

    value = str(name).strip().lower()

    replacements = {
        "ä": "a",
        "ö": "o",
        "ü": "u",
        "ß": "ss",
        ".": "",
        ",": "",
        "'": "",
        "-": " ",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    return " ".join(value.split())


def team_matches_name(team_name, config):
    """
    Prüft, ob ein OpenLigaDB-Team zu unserem Team gehört.
    """

    normalized = normalize_name(team_name)

    if not normalized:
        return False

    for alias in config["aliases"]:
        alias_normalized = normalize_name(alias)

        if normalized == alias_normalized:
            return True

        if alias_normalized in normalized:
            return True

        if normalized in alias_normalized:
            return True

    return False


def get_match_team(match, key):
    team = match.get(key)

    if not isinstance(team, dict):
        return {}

    return team


def match_contains_team(match, config):
    """
    Prüft beide Mannschaften eines Spiels.
    """

    home = get_match_team(match, "team1")
    away = get_match_team(match, "team2")

    home_name = home.get("teamName", "")
    away_name = away.get("teamName", "")

    return (
        team_matches_name(home_name, config)
        or team_matches_name(away_name, config)
    )


# ============================================================
# SPIELDATUM
# ============================================================

def parse_match_datetime(match):
    """
    OpenLigaDB kann unterschiedliche Datumsfelder liefern.
    """

    possible_fields = [
        "matchDateTimeUTC",
        "matchDateTime",
    ]

    value = None

    for field in possible_fields:
        if match.get(field):
            value = match.get(field)
            break

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


# ============================================================
# NÄCHSTES SPIEL
# ============================================================

def get_next_match(matches, config):
    """
    Sucht das nächste Spiel des Vereins.
    """

    now = now_utc()

    upcoming = []

    for match in matches:

        if not isinstance(match, dict):
            continue

        if not match_contains_team(match, config):
            continue

        match_datetime = parse_match_datetime(match)

        if match_datetime is None:
            continue

        if match_datetime >= now:
            upcoming.append(
                (
                    match_datetime,
                    match,
                )
            )

    if not upcoming:
        return None

    upcoming.sort(
        key=lambda item: item[0]
    )

    return upcoming[0][1]


def build_next_match(match, config):
    """
    Erstellt einen sauberen JSON-Block zum nächsten Spiel.
    """

    if not match:
        return {
            "opponent": None,
            "homeAway": None,
            "matchDate": None,
            "matchId": None,
        }

    home = get_match_team(
        match,
        "team1",
    )

    away = get_match_team(
        match,
        "team2",
    )

    home_name = home.get(
        "teamName",
        "Unbekannt",
    )

    away_name = away.get(
        "teamName",
        "Unbekannt",
    )

    if team_matches_name(home_name, config):

        opponent = away_name
        home_away = "Heim"

    elif team_matches_name(away_name, config):

        opponent = home_name
        home_away = "Auswärts"

    else:

        opponent = None
        home_away = None

    match_datetime = parse_match_datetime(match)

    return {
        "opponent": opponent,
        "homeAway": home_away,
        "matchDate": (
            match_datetime.isoformat()
            if match_datetime
            else None
        ),
        "matchId": match.get("matchID"),
    }


# ============================================================
# TEAM-DATEN
# ============================================================

def build_team_data(config, matches):
    """
    Erstellt den Datenblock für einen Bundesliga-Verein.
    """

    next_match = get_next_match(
        matches,
        config,
    )

    return {
        "club": config["name"],
        "league": "Bundesliga",
        "season": CURRENT_SEASON,

        "nextMatch": build_next_match(
            next_match,
            config,
        ),

        "lastUpdated": today_string(),

        "source": {
            "name": "OpenLigaDB",
            "url": "https://www.openligadb.de/",
        },
    }


# ============================================================
# KRISTOF
# ============================================================

def update_kristof(data):
    """
    Kristof bleibt als Spielerprofil erhalten.

    OpenLigaDB liefert keine Kickbase-Punkte,
    Verletzungsdaten oder Startelf-Wahrscheinlichkeiten.

    Deshalb werden diese Werte NICHT erfunden.
    """

    kristof = data.get("kristof")

    if not isinstance(kristof, dict):
        kristof = {}

    kristof["club"] = "SV Elversberg"

    elversberg = (
        data.get("teams", {})
        .get("elversberg", {})
    )

    next_match = (
        elversberg.get("nextMatch", {})
        if isinstance(elversberg, dict)
        else {}
    )

    kristof["opponent"] = next_match.get(
        "opponent"
    )

    kristof["homeAway"] = next_match.get(
        "homeAway"
    )

    if "average" not in kristof:
        kristof["average"] = None

    if "starting" not in kristof:
        kristof["starting"] = None

    if "form" not in kristof:
        kristof["form"] = None

    if "injury" not in kristof:
        kristof["injury"] = None

    if "suspension" not in kristof:
        kristof["suspension"] = None

    if "recommendation" not in kristof:
        kristof["recommendation"] = None

    kristof["lastUpdated"] = today_string()

    data["kristof"] = kristof

    return data


# ============================================================
# HAUPTPROGRAMM
# ============================================================

def main():

    print(
        "Starte kostenlose Bundesliga "
        "Player-Intelligence-Recherche..."
    )

    print()

    data = load_intelligence()

    # --------------------------------------------------------
    # EINMALIG ALLE BUNDESLIGA-SPIELE LADEN
    # --------------------------------------------------------

    matches = load_all_bundesliga_matches()

    if not matches:
        raise RuntimeError(
            "OpenLigaDB hat keine Bundesliga-Spiele geliefert."
        )

    # --------------------------------------------------------
    # TEAM-BEREICH INITIALISIEREN
    # --------------------------------------------------------

    if not isinstance(
        data.get("teams"),
        dict,
    ):
        data["teams"] = {}

    successful_teams = 0

    # --------------------------------------------------------
    # ALLE 18 TEAMS VERARBEITEN
    # --------------------------------------------------------

    for config in BUNDESLIGA_TEAMS:

        print()
        print("----------------------------------------")
        print(
            f"Verarbeite: {config['name']}"
        )

        team_matches = [
            match
            for match in matches
            if match_contains_team(
                match,
                config,
            )
        ]

        print(
            f"{config['name']}: "
            f"{len(team_matches)} Spiele gefunden"
        )

        team_data = build_team_data(
            config,
            matches,
        )

        next_match = team_data["nextMatch"]

        if next_match.get("opponent"):

            print(
                f"Nächstes Spiel: "
                f"{config['name']} "
                f"gegen "
                f"{next_match['opponent']}"
            )

            print(
                f"Heim/Auswärts: "
                f"{next_match['homeAway']}"
            )

            print(
                f"Datum: "
                f"{next_match['matchDate']}"
            )

            successful_teams += 1

        else:

            print(
                f"Kein kommendes Spiel "
                f"für {config['name']}"
            )

        # ----------------------------------------------------
        # TEAM SPEICHERN
        # ----------------------------------------------------

        data["teams"][config["key"]] = team_data

    # --------------------------------------------------------
    # KRISTOF AKTUALISIEREN
    # --------------------------------------------------------

    data = update_kristof(data)

    # --------------------------------------------------------
    # META-INFORMATION
    # --------------------------------------------------------

    data["league"] = "Bundesliga"

    data["season"] = CURRENT_SEASON

    data["lastUpdated"] = today_string()

    data["teamCount"] = len(
        BUNDESLIGA_TEAMS
    )

    # --------------------------------------------------------
    # SPEICHERN
    # --------------------------------------------------------

    save_intelligence(data)

    print()
    print("----------------------------------------")
    print(
        f"{successful_teams}/"
        f"{len(BUNDESLIGA_TEAMS)} "
        f"Teams mit nächstem Spiel gefunden."
    )

    print(
        "player-intelligence.json wurde "
        "erfolgreich aktualisiert."
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
