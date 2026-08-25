import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"

OPENLIGADB_URL = "https://api.openligadb.de"

# Saison 2026 = Saison 2026/27
CURRENT_SEASON = 2026

# Alle drei deutschen Profiligen
LEAGUES = {
    "bl1": "1. Bundesliga",
    "bl2": "2. Bundesliga",
    "bl3": "3. Liga",
}


# ---------------------------------------------------------
# OpenLigaDB
# ---------------------------------------------------------

def openligadb_get(path):
    url = f"{OPENLIGADB_URL}{path}"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Kickbase-AI/1.0",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    except HTTPError as error:
        raise RuntimeError(
            f"OpenLigaDB HTTP-Fehler {error.code}: {error.reason}"
        )

    except URLError as error:
        raise RuntimeError(
            f"OpenLigaDB Netzwerkfehler: {error.reason}"
        )


# ---------------------------------------------------------
# JSON laden / speichern
# ---------------------------------------------------------

def load_intelligence():
    if not INTELLIGENCE_FILE.exists():
        return {}

    with INTELLIGENCE_FILE.open(
        "r",
        encoding="utf-8",
    ) as file:
        return json.load(file)


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


# ---------------------------------------------------------
# Verein automatisch finden
# ---------------------------------------------------------

TEAM_CACHE = {}


def get_teams(league):
    if league in TEAM_CACHE:
        return TEAM_CACHE[league]

    data = openligadb_get(
        f"/getavailableteams/{league}/{CURRENT_SEASON}"
    )

    TEAM_CACHE[league] = data or []

    return TEAM_CACHE[league]


def find_team(club_name):
    """
    Sucht einen Verein automatisch in
    Bundesliga, 2. Bundesliga und 3. Liga.
    """

    if not club_name:
        return None

    search = club_name.strip().lower()

    aliases = {
        "elversberg": "sv elversberg",
        "sv elversberg": "sv elversberg",

        "schalke": "fc schalke 04",
        "fc schalke": "fc schalke 04",
        "schalke 04": "fc schalke 04",

        "paderborn": "sc paderborn 07",
        "sc paderborn": "sc paderborn 07",

        "köln": "1. fc köln",
        "fc köln": "1. fc köln",

        "hamburg": "hamburger sv",
        "hsv": "hamburger sv",

        "hertha": "hertha bsc",
        "hertha bsc": "hertha bsc",
    }

    target = aliases.get(search, search)

    for league, league_name in LEAGUES.items():

        teams = get_teams(league)

        for team in teams:

            team_name = (
                team.get("teamName")
                or team.get("name")
                or ""
            )

            normalized = team_name.lower()

            # Exakter Treffer
            if normalized == target:
                return {
                    "team": team,
                    "league": league,
                    "leagueName": league_name,
                }

            # Enthält-Treffer
            if (
                target in normalized
                or normalized in target
            ):
                return {
                    "team": team,
                    "league": league,
                    "leagueName": league_name,
                }

    return None


# ---------------------------------------------------------
# Saisonspiele
# ---------------------------------------------------------

FIXTURE_CACHE = {}


def get_team_matches(team_name, league):
    """
    Holt alle Saisonspiele des Vereins.
    Die Daten werden zwischengespeichert,
    damit die API nicht unnötig oft aufgerufen wird.
    """

    cache_key = f"{league}:{team_name}"

    if cache_key in FIXTURE_CACHE:
        return FIXTURE_CACHE[cache_key]

    matches = openligadb_get(
        f"/getmatchdata/"
        f"{league}/"
        f"{CURRENT_SEASON}/"
        f"{quote(team_name)}"
    )

    FIXTURE_CACHE[cache_key] = matches or []

    return FIXTURE_CACHE[cache_key]


# ---------------------------------------------------------
# Nächstes Spiel
# ---------------------------------------------------------

def get_next_fixture(team_name, league):
    """
    Gibt das nächste noch nicht vergangene Spiel zurück.
    """

    matches = get_team_matches(
        team_name,
        league,
    )

    now = datetime.now(timezone.utc)

    upcoming = []

    for match in matches:

        match_date = match.get(
            "matchDateTimeUTC"
        )

        if not match_date:
            continue

        try:
            match_datetime = datetime.fromisoformat(
                match_date.replace(
                    "Z",
                    "+00:00",
                )
            )

        except ValueError:
            continue

        if match_datetime >= now:
            upcoming.append(match)

    if not upcoming:
        return None

    upcoming.sort(
        key=lambda match: match.get(
            "matchDateTimeUTC",
            "",
        )
    )

    return upcoming[0]


# ---------------------------------------------------------
# Letzte Spiele / Form
# ---------------------------------------------------------

def get_final_score(match):
    """
    Holt das Endergebnis eines abgeschlossenen Spiels.

    OpenLigaDB kennzeichnet das Endergebnis
    mit resultTypeID == 2.
    """

    results = match.get(
        "matchResults",
        [],
    )

    for result in results:

        if result.get("resultTypeID") == 2:
            return (
                result.get("pointsTeam1"),
                result.get("pointsTeam2"),
            )

    # Fallback: falls resultTypeID nicht vorhanden ist,
    # nehmen wir das letzte vorhandene Ergebnis.
    if results:

        result = results[-1]

        return (
            result.get("pointsTeam1"),
            result.get("pointsTeam2"),
        )

    return None, None


def get_recent_form(team_name, league, limit=5):
    """
    Berechnet die letzten abgeschlossenen Spiele
    des Vereins.

    Rückgabe:
        last5
        wins
        draws
        losses
        points
        form
    """

    matches = get_team_matches(
        team_name,
        league,
    )

    now = datetime.now(timezone.utc)

    finished_matches = []

    for match in matches:

        match_date = match.get(
            "matchDateTimeUTC"
        )

        if not match_date:
            continue

        try:
            match_datetime = datetime.fromisoformat(
                match_date.replace(
                    "Z",
                    "+00:00",
                )
            )

        except ValueError:
            continue

        # Nur Spiele aus der Vergangenheit
        if match_datetime >= now:
            continue

        team1 = match.get(
            "team1",
            {},
        )

        team2 = match.get(
            "team2",
            {},
        )

        team1_name = team1.get(
            "teamName",
            "",
        )

        team2_name = team2.get(
            "teamName",
            "",
        )

        points1, points2 = get_final_score(
            match
        )

        # Kein Endergebnis vorhanden
        if points1 is None or points2 is None:
            continue

        team_lower = team_name.lower()

        if team1_name.lower() == team_lower:
            own_goals = points1
            opponent_goals = points2
            opponent = team2_name

        elif team2_name.lower() == team_lower:
            own_goals = points2
            opponent_goals = points1
            opponent = team1_name

        else:
            # Fallback bei leicht abweichenden Namen
            if team_lower in team1_name.lower():
                own_goals = points1
                opponent_goals = points2
                opponent = team2_name

            elif team_lower in team2_name.lower():
                own_goals = points2
                opponent_goals = points1
                opponent = team1_name

            else:
                continue

        if own_goals > opponent_goals:
            result = "Sieg"
            points = 3

        elif own_goals == opponent_goals:
            result = "Unentschieden"
            points = 1

        else:
            result = "Niederlage"
            points = 0

        finished_matches.append(
            {
                "date": match_date[:10],
                "opponent": opponent,
                "result": result,
                "goalsFor": own_goals,
                "goalsAgainst": opponent_goals,
                "points": points,
            }
        )

    # Neueste Spiele zuerst
    finished_matches.sort(
        key=lambda match: match["date"],
        reverse=True,
    )

    last5 = finished_matches[:limit]

    wins = sum(
        1
        for match in last5
        if match["result"] == "Sieg"
    )

    draws = sum(
        1
        for match in last5
        if match["result"] == "Unentschieden"
    )

    losses = sum(
        1
        for match in last5
        if match["result"] == "Niederlage"
    )

    points = sum(
        match["points"]
        for match in last5
    )

    if not last5:
        form = "Keine Daten"

    elif points >= 10:
        form = "Sehr gut"

    elif points >= 7:
        form = "Gut"

    elif points >= 4:
        form = "Mittel"

    else:
        form = "Schwach"

    return {
        "last5": last5,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points": points,
        "form": form,
    }


# ---------------------------------------------------------
# Heim / Auswärts + Gegner
# ---------------------------------------------------------

def get_fixture_information(
    fixture,
    team_name,
):

    if not fixture:
        return {
            "opponent": None,
            "homeAway": None,
        }

    home = fixture.get(
        "team1",
        {},
    )

    away = fixture.get(
        "team2",
        {},
    )

    home_name = home.get(
        "teamName",
        "",
    )

    away_name = away.get(
        "teamName",
        "",
    )

    team_lower = team_name.lower()

    if home_name.lower() == team_lower:
        return {
            "opponent": away_name,
            "homeAway": "Heim",
        }

    if away_name.lower() == team_lower:
        return {
            "opponent": home_name,
            "homeAway": "Auswärts",
        }

    # Fallback
    if team_lower in home_name.lower():
        return {
            "opponent": away_name,
            "homeAway": "Heim",
        }

    if team_lower in away_name.lower():
        return {
            "opponent": home_name,
            "homeAway": "Auswärts",
        }

    return {
        "opponent": None,
        "homeAway": None,
    }


# ---------------------------------------------------------
# Player Intelligence
# ---------------------------------------------------------

def update_player(
    player_id,
    player_data,
):

    club_name = player_data.get(
        "club"
    )

    if not club_name:
        print(
            f"  Kein Verein für "
            f"{player_id} gefunden."
        )

        return player_data

    print(
        f"  Verein: {club_name}"
    )

    found = find_team(
        club_name
    )

    if not found:
        print(
            f"  Verein '{club_name}' "
            f"nicht in OpenLigaDB gefunden."
        )

        intelligence = player_data.setdefault(
            "intelligence",
            {},
        )

        intelligence["opponent"] = None
        intelligence["homeAway"] = None

        return player_data

    team = found["team"]
    league = found["league"]
    league_name = found["leagueName"]

    team_name = (
        team.get("teamName")
        or team.get("name")
        or club_name
    )

    print(
        f"  Gefunden: {team_name} "
        f"({league_name})"
    )

    # -----------------------------------------------------
    # Nächstes Spiel
    # -----------------------------------------------------

    fixture = get_next_fixture(
        team_name,
        league,
    )

    fixture_info = get_fixture_information(
        fixture,
        team_name,
    )

    # -----------------------------------------------------
    # Form
    # -----------------------------------------------------

    form = get_recent_form(
        team_name,
        league,
    )

    intelligence = player_data.setdefault(
        "intelligence",
        {},
    )

    # Nächstes Spiel
    intelligence["opponent"] = (
        fixture_info["opponent"]
    )

    intelligence["homeAway"] = (
        fixture_info["homeAway"]
    )

    # Form der Mannschaft
    intelligence["form"] = form

    # Verletzungen / Sperren
    #
    # OpenLigaDB liefert diese Informationen
    # nicht zuverlässig.
    #
    # Deshalb nichts erfinden.
    if "injury" not in intelligence:
        intelligence["injury"] = None

    if "suspension" not in intelligence:
        intelligence["suspension"] = None

    # Quelle dokumentieren
    intelligence["sources"] = [
        {
            "title": (
                f"OpenLigaDB – "
                f"{league_name} "
                f"{CURRENT_SEASON}/"
                f"{str(CURRENT_SEASON + 1)[-2:]}"
            ),
            "url": (
                "https://www.openligadb.de/"
            ),
            "date": datetime.now(
                timezone.utc
            ).strftime("%Y-%m-%d"),
        }
    ]

    intelligence["lastUpdated"] = (
        datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d")
    )

    return player_data


# ---------------------------------------------------------
# Hauptprogramm
# ---------------------------------------------------------

def main():

    print(
        "Starte kostenlose "
        "Player-Intelligence-Recherche..."
    )

    data = load_intelligence()

    if not data:
        print(
            "Keine Spieler in "
            "player-intelligence.json gefunden."
        )

        return

    print(
        f"{len(data)} Spieler gefunden."
    )

    updated = 0

    for player_id, player_data in data.items():

        print(
            f"\nBearbeite Spieler: "
            f"{player_id}"
        )

        try:

            update_player(
                player_id,
                player_data,
            )

            updated += 1

        except Exception as error:

            print(
                f"  Fehler bei "
                f"{player_id}: "
                f"{error}"
            )

    save_intelligence(data)

    print(
        f"\nFertig. "
        f"{updated} Spieler verarbeitet."
    )

    print(
        "player-intelligence.json "
        "wurde aktualisiert."
    )


if __name__ == "__main__":
    main()
