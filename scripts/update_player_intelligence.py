import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"

API_URL = "https://v3.football.api-sports.io"
API_KEY = os.environ.get("API_FOOTBALL_KEY")

SEASON = 2026
TEAM_NAME = "Elversberg"


def api_get(endpoint, params=None):
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY fehlt.")

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
        data = json.loads(response.read().decode("utf-8"))

    if data.get("errors"):
        raise RuntimeError(f"API-Football Fehler: {data['errors']}")

    return data


def load_intelligence():
    with INTELLIGENCE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_intelligence(data):
    with INTELLIGENCE_FILE.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def find_team():
    data = api_get(
        "teams",
        {
            "search": TEAM_NAME,
        },
    )

    if not data.get("response"):
        raise RuntimeError(
            f"Verein '{TEAM_NAME}' wurde nicht gefunden."
        )

    for entry in data["response"]:
        team = entry.get("team", {})

        if team.get("name", "").lower() == TEAM_NAME.lower():
            return team

    return data["response"][0]["team"]


def get_next_fixture(team_id):
    data = api_get(
        "fixtures",
        {
            "team": team_id,
            "season": SEASON,
            "next": 1,
        },
    )

    fixtures = data.get("response", [])

    if not fixtures:
        return None

    return fixtures[0]


def get_team_injuries(team_id):
    data = api_get(
        "injuries",
        {
            "team": team_id,
            "season": SEASON,
        },
    )

    return data.get("response", [])


def build_kristof_intelligence(team, fixture, injuries):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    result = {
        "average": None,
        "starting": None,
        "form": None,
        "opponent": None,
        "homeAway": None,
        "injury": None,
        "suspension": None,
        "recommendation": None,
        "sources": [
            {
                "title": "API-Football",
                "url": "https://www.api-football.com/",
                "date": now,
            }
        ],
        "lastUpdated": now,
    }

    if fixture:
        home = fixture["teams"]["home"]
        away = fixture["teams"]["away"]

        if home["id"] == team["id"]:
            opponent = away["name"]
            home_away = "Heim"
        else:
            opponent = home["name"]
            home_away = "Auswärts"

        result["opponent"] = opponent
        result["homeAway"] = home_away

    kristof_injuries = []

    for item in injuries:
        player = item.get("player", {})

        name = (player.get("name") or "").lower()

        if "kristof" in name:
            kristof_injuries.append(item)

    if kristof_injuries:
        item = kristof_injuries[0]

        injury_type = item.get("type")
        reason = item.get("reason")

        if injury_type == "Suspension":
            result["suspension"] = reason or "Aktuelle Sperre"
        else:
            result["injury"] = reason or "Aktuelle Verletzung"
    else:
        result["injury"] = "Keine aktuelle Meldung gefunden"
        result["suspension"] = "Keine aktuelle Sperrmeldung gefunden"

    return result


def main():
    print("Starte kostenlose Player-Intelligence-Recherche...")

    data = load_intelligence()

    team = find_team()

    print(
        f"Verein gefunden: {team.get('name')} "
        f"(ID {team.get('id')})"
    )

    fixture = get_next_fixture(team["id"])

    if fixture:
        print(
            "Nächstes Spiel:",
            fixture["teams"]["home"]["name"],
            "vs.",
            fixture["teams"]["away"]["name"],
        )
    else:
        print("Kein nächstes Spiel gefunden.")

    injuries = get_team_injuries(team["id"])

    print(f"Verfügbare Verletzungs-/Sperrdaten: {len(injuries)}")

    data["kristof"] = build_kristof_intelligence(
        team,
        fixture,
        injuries,
    )

    save_intelligence(data)

    print("player-intelligence.json wurde aktualisiert.")


if __name__ == "__main__":
    main()
