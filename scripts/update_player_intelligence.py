import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"

OPENLIGADB_URL = "https://api.openligadb.de"

CURRENT_SEASON = 2026

# Vereine, die wir beobachten wollen.
# league = OpenLigaDB-Kürzel:
# bl1 = 1. Bundesliga
# bl2 = 2. Bundesliga
# bl3 = 3. Liga
TEAMS = [
    {
        "name": "SV Elversberg",
        "league": "bl2",
    },
    {
        "name": "FC Schalke 04",
        "league": "bl2",
    },
    {
        "name": "SC Paderborn 07",
        "league": "bl2",
    },
]


def openligadb_get(path):
    """Kostenlos Daten von OpenLigaDB abrufen."""
    url = f"{OPENLIGADB_URL}{path}"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Kickbase-AI-Player-Intelligence/1.0",
        },
    )

    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


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
            indent=2,
        )
        file.write("\n")


def find_team(team_name, league):
    """
    Findet ein Team zuverlässig über die offizielle OpenLigaDB-Teamliste.
    Funktioniert für mehrere Vereine und Ligen.
    """

    teams = openligadb_get(
        f"/getavailableteams/{league}/{CURRENT_SEASON}"
    )

    if not teams:
        print(f"Keine Teams für Liga {league} gefunden.")
        return None

    wanted = team_name.lower().strip()

    # 1. Exakter Treffer
    for team in teams:
        official_name = team.get("teamName", "").strip()

        if official_name.lower() == wanted:
            return team

    # 2. Teilname / vereinfachter Name
    for team in teams:
        official_name = team.get("teamName", "").strip()
        name_lower = official_name.lower()

        if wanted in name_lower or name_lower in wanted:
            return team

    # 3. Bekannte Varianten
    aliases = {
        "sv elversberg": [
            "sv 07 elversberg",
            "sv elversberg",
            "elversberg",
        ],
        "fc schalke 04": [
            "fc schalke 04",
            "schalke 04",
            "schalke",
        ],
        "sc paderborn 07": [
            "sc paderborn 07",
            "paderborn 07",
            "paderborn",
        ],
    }

    for alias in aliases.get(wanted, []):
        for team in teams:
            official_name = team.get("teamName", "").strip()

            if official_name.lower() == alias.lower():
                return team

    print(f"Team nicht gefunden: {team_name}")
    return None


def get_team_matches(team_name, league):
    """Alle Saisonspiele des Vereins laden."""

    matches = openligadb_get(
        f"/getmatchdata/{league}/{CURRENT_SEASON}/{quote(team_name)}"
    )

    if not matches:
        return []

    return matches


def get_next_fixture(team_name, league):
    """Nächstes noch nicht abgeschlossenes Spiel finden."""

    matches = get_team_matches(team_name, league)

    now = datetime.now(timezone.utc)

    upcoming = []

    for match in matches:
        if match.get("matchIsFinished"):
            continue

        date_string = match.get("matchDateTimeUTC")

        if not date_string:
            date_string = match.get("matchDateTime")

        if not date_string:
            continue

        try:
            match_date = datetime.fromisoformat(
                date_string.replace("Z", "+00:00")
            )

            if match_date.tzinfo is None:
                match_date = match_date.replace(tzinfo=timezone.utc)

        except ValueError:
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

    upcoming.sort(key=lambda item: item[0])

    return upcoming[0][1]


def get_recent_matches(team_name, league, count=5):
    """Letzte abgeschlossene Spiele des Vereins."""

    matches = get_team_matches(team_name, league)

    now = datetime.now(timezone.utc)

    finished = []

    for match in matches:
        if not match.get("matchIsFinished"):
            continue

        date_string = match.get("matchDateTimeUTC")

        if not date_string:
            date_string = match.get("matchDateTime")

        if not date_string:
            continue

        try:
            match_date = datetime.fromisoformat(
                date_string.replace("Z", "+00:00")
            )

            if match_date.tzinfo is None:
                match_date = match_date.replace(tzinfo=timezone.utc)

        except ValueError:
            continue

        if match_date <= now:
            finished.append(
                (
                    match_date,
                    match,
                )
            )

    finished.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return [item[1] for item in finished[:count]]


def get_opponent(match, team_name):
    """Gegner aus einem OpenLigaDB-Match bestimmen."""

    team1 = match.get("team1", {})
    team2 = match.get("team2", {})

    team1_name = team1.get("teamName", "")
    team2_name = team2.get("teamName", "")

    if team1_name.lower() == team_name.lower():
        return team2_name

    if team2_name.lower() == team_name.lower():
        return team1_name

    return None


def get_home_away(match, team_name):
    """Ermitteln, ob das Team Heim- oder Auswärtsspiel hat."""

    team1 = match.get("team1", {})
    team2 = match.get("team2", {})

    team1_name = team1.get("teamName", "")
    team2_name = team2.get("teamName", "")

    if team1_name.lower() == team_name.lower():
        return "Heim"

    if team2_name.lower() == team_name.lower():
        return "Auswärts"

    return None


def get_match_date(match):
    date_string = match.get("matchDateTimeUTC")

    if not date_string:
        date_string = match.get("matchDateTime")

    if not date_string:
        return None

    return date_string


def get_result_for_team(match, team_name):
    """Ergebnis eines abgeschlossenen Spiels bestimmen."""

    if not match.get("matchIsFinished"):
        return None

    team1 = match.get("team1", {})
    team2 = match.get("team2", {})

    team1_name = team1.get("teamName", "")
    team2_name = team2.get("teamName", "")

    results = match.get("matchResults", [])

    if not results:
        return None

    final_result = None

    for result in results:
        result_type = result.get("resultTypeID")

        # OpenLigaDB: 2 = Endergebnis
        if result_type == 2:
            final_result = result

    if final_result is None:
        final_result = results[-1]

    goals1 = final_result.get("pointsTeam1")
    goals2 = final_result.get("pointsTeam2")

    if goals1 is None or goals2 is None:
        return None

    if team1_name.lower() == team_name.lower():
        own_goals = goals1
        opponent_goals = goals2
    elif team2_name.lower() == team_name.lower():
        own_goals = goals2
        opponent_goals = goals1
    else:
        return None

    if own_goals > opponent_goals:
        result = "Sieg"
    elif own_goals < opponent_goals:
        result = "Niederlage"
    else:
        result = "Unentschieden"

    return {
        "ownGoals": own_goals,
        "opponentGoals": opponent_goals,
        "result": result,
    }


def calculate_form(team_name, league):
    """Form aus den letzten fünf Spielen berechnen."""

    recent = get_recent_matches(
        team_name,
        league,
        count=5,
    )

    if not recent:
        return None

    results = []

    for match in recent:
        result = get_result_for_team(
            match,
            team_name,
        )

        if result:
            results.append(result["result"])

    if not results:
        return None

    wins = results.count("Sieg")
    draws = results.count("Unentschieden")
    losses = results.count("Niederlage")

    return {
        "lastMatches": len(results),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "results": results,
    }


def build_team_intelligence(team_config):
    team_name = team_config["name"]
    league = team_config["league"]

    team = find_team(
        team_name,
        league,
    )

    if not team:
        print(
            f"Team nicht gefunden: {team_name}"
        )
        return None

    official_name = team.get(
        "teamName",
        team_name,
    )

    team_id = team.get("teamId")

    print(
        f"Verein gefunden: {official_name} "
        f"(ID {team_id})"
    )

    fixture = get_next_fixture(
        official_name,
        league,
    )

    form = calculate_form(
        official_name,
        league,
    )

    now = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    result = {
        "club": official_name,
        "league": league,
        "teamId": team_id,
        "average": None,
        "starting": None,
        "form": form,
        "opponent": None,
        "homeAway": None,
        "injury": None,
        "suspension": None,
        "recommendation": None,
        "nextMatch": None,
        "sources": [
            {
                "title": "OpenLigaDB",
                "url": "https://api.openligadb.de/",
                "date": now,
            }
        ],
        "lastUpdated": now,
        "confidence": {
            "starting": "niedrig",
            "injury": "keine Datenquelle",
            "suspension": "keine Datenquelle",
            "recommendation": "niedrig",
        },
    }

    if fixture:
        opponent = get_opponent(
            fixture,
            official_name,
        )

        home_away = get_home_away(
            fixture,
            official_name,
        )

        result["opponent"] = opponent
        result["homeAway"] = home_away

        result["nextMatch"] = {
            "date": get_match_date(fixture),
            "opponent": opponent,
            "homeAway": home_away,
        }

    # Ohne verlässliche kostenlose Spielerquelle
    # werden Verletzungen/Sperren NICHT erfunden.
    result["injury"] = (
        "Keine verlässliche kostenlose Datenquelle verfügbar"
    )

    result["suspension"] = (
        "Keine verlässliche kostenlose Datenquelle verfügbar"
    )

    # Einfache, transparente Empfehlung.
    if fixture and form:
        wins = form["wins"]
        losses = form["losses"]

        if wins >= 3 and losses <= 1:
            result["recommendation"] = "Positiv"
        elif losses >= 3:
            result["recommendation"] = "Vorsicht"
        else:
            result["recommendation"] = "Neutral"

        result["confidence"]["recommendation"] = "mittel"

    elif fixture:
        result["recommendation"] = "Neutral"

    else:
        result["recommendation"] = "Keine Bewertung möglich"

    return result


def main():
    print(
        "Starte kostenlose Multi-Team "
        "Player-Intelligence-Recherche..."
    )

    data = load_intelligence()

    if "teams" not in data:
        data["teams"] = {}

    successful = 0

    for team_config in TEAMS:
        team_name = team_config["name"]

        print(f"\nVerarbeite: {team_name}")

        try:
            intelligence = build_team_intelligence(
                team_config
            )

            if intelligence is None:
                print(
                    f"KEINE DATEN für {team_name}"
                )
                continue

            key = team_name.lower()

            data["teams"][key] = intelligence

            successful += 1

            print(
                f"OK: {team_name} gespeichert."
            )

        except Exception as error:
            print(
                f"FEHLER bei {team_name}: {error}"
            )

    data["lastUpdated"] = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    if successful == 0:
        raise RuntimeError(
            "Kein einziger Verein konnte "
            "aktualisiert werden."
        )

    save_intelligence(data)

    print(
        f"\n{successful} Verein(e) erfolgreich "
        "aktualisiert."
    )
    print(
        "player-intelligence.json wurde "
        "aktualisiert."
    )


if __name__ == "__main__":
    main()
