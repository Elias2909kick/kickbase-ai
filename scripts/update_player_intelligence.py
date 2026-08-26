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
from html.parser import HTMLParser
from urllib.parse import urljoin

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
# OFFIZIELLE BUNDESLIGA-QUELLEN
# ============================================================

BUNDESLIGA_CLUBS_URL = (
    "https://www.bundesliga.com/de/bundesliga/clubs"
)

BUNDESLIGA_PLAYERS_URL = (
    "https://www.bundesliga.com/de/bundesliga/spieler"
)

# OpenLigaDB bleibt die Quelle für den Spielplan.
BUNDESLIGA_SHORTCUT = "bl1"
CURRENT_SEASON = "2026/27"
OPENLIGADB_SEASON = 2026

# Offizielle Bundesliga-Namen -> stabile JSON-Namen.
BUNDESLIGA_NAME_MAP = {
    "fc augsburg": "FC Augsburg",
    "1 fc union berlin": "1. FC Union Berlin",
    "sv werder bremen": "SV Werder Bremen",
    "borussia dortmund": "Borussia Dortmund",
    "sv elversberg": "SV Elversberg",
    "eintracht frankfurt": "Eintracht Frankfurt",
    "sport club freiburg": "Sport-Club Freiburg",
    "hamburger sv": "Hamburger SV",
    "tsg hoffenheim": "TSG Hoffenheim",
    "1 fc koln": "1. FC Köln",
    "rb leipzig": "RB Leipzig",
    "bayer 04 leverkusen": "Bayer 04 Leverkusen",
    "1 fsv mainz 05": "1. FSV Mainz 05",
    "borussia monchengladbach": "Borussia Mönchengladbach",
    "fc bayern munchen": "FC Bayern München",
    "sc paderborn 07": "SC Paderborn 07",
    "fc schalke 04": "FC Schalke 04",
    "vfb stuttgart": "VfB Stuttgart",
}

POSITION_MAP = {
    "torhüter": "Torhüter",
    "torhueter": "Torhüter",
    "verteidigung": "Verteidigung",
    "mittelfeld": "Mittelfeld",
    "angriff": "Angriff",
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


def http_get_text(
    url,
    headers=None,
    timeout=30,
):
    """
    Holt HTML/Text von einer Webseite.
    """

    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; Kickbase-AI/2.0)"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/json;q=0.9,*/*;q=0.8"
            ),
            **(headers or {}),
        },
    )

    with urlopen(
        request,
        timeout=timeout,
    ) as response:
        return response.read().decode(
            "utf-8",
            "ignore",
        )


class BundesligaClubParser(HTMLParser):
    """
    Extrahiert Club-Überschriften aus Bundesliga.com.
    """

    def __init__(self):
        super().__init__()
        self.club_names = []
        self._heading_depth = 0
        self._heading_parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("h2", "h3"):
            self._heading_depth = 1
            self._heading_parts = []

    def handle_data(self, data):
        value = " ".join(data.split())

        if self._heading_depth and value:
            self._heading_parts.append(value)

    def handle_endtag(self, tag):
        if (
            self._heading_depth
            and tag in ("h2", "h3")
        ):
            name = " ".join(
                self._heading_parts
            ).strip()

            if name:
                self.club_names.append(name)

            self._heading_depth = 0
            self._heading_parts = []


class BundesligaPlayersParser(HTMLParser):
    """
    Extrahiert Spieler aus der offiziellen
    Bundesliga-Spielerübersicht.
    """

    def __init__(self):
        super().__init__()

        self.players = []
        self.current_club = None
        self.current_position = None

        self._heading_depth = 0
        self._heading_parts = []

        self._anchor_href = None
        self._anchor_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        if tag in ("h2", "h3"):
            self._heading_depth = 1
            self._heading_parts = []

        if tag == "a":
            href = attrs.get("href", "")

            if (
                href
                and "/spieler/" in href
            ):
                self._anchor_href = href
                self._anchor_parts = []

    def handle_data(self, data):
        value = " ".join(data.split())

        if self._heading_depth and value:
            self._heading_parts.append(value)

        if self._anchor_href is not None and value:
            self._anchor_parts.append(value)

        normalized = value.casefold()

        if normalized in POSITION_MAP:
            self.current_position = (
                POSITION_MAP[normalized]
            )

    def handle_endtag(self, tag):
        if (
            self._heading_depth
            and tag in ("h2", "h3")
        ):
            heading = " ".join(
                self._heading_parts
            ).strip()

            if heading:
                normalized = normalize_name(
                    heading
                )

                mapped = BUNDESLIGA_NAME_MAP.get(
                    normalized
                )

                if mapped:
                    self.current_club = mapped
                    self.current_position = None

            self._heading_depth = 0
            self._heading_parts = []

        if (
            tag == "a"
            and self._anchor_href is not None
        ):
            raw_text = " ".join(
                self._anchor_parts
            ).strip()

            if (
                raw_text
                and raw_text.casefold() != "image"
                and self.current_club
                and self.current_position
            ):
                number = None
                name = raw_text

                match = re.match(
                    r"^(\d{1,2})\s*(.+)$",
                    raw_text,
                )

                if match:
                    number = int(
                        match.group(1)
                    )
                    name = match.group(2).strip()

                name = re.sub(
                    r"(?<=[a-zäöüß])(?=[A-ZÄÖÜ])",
                    " ",
                    name,
                )

                href = urljoin(
                    BUNDESLIGA_PLAYERS_URL,
                    self._anchor_href,
                )

                slug = (
                    self._anchor_href
                    .rstrip("/")
                    .split("/")[-1]
                )

                self.players.append(
                    {
                        "club": self.current_club,
                        "position": self.current_position,
                        "id": slug or href,
                        "name": name,
                        "number": number,
                        "sourceUrl": href,
                    }
                )

            self._anchor_href = None
            self._anchor_parts = []


def get_bundesliga_squads():
    """
    Holt alle Bundesliga-Kader direkt von Bundesliga.com.
    """

    try:
        html = http_get_text(
            BUNDESLIGA_PLAYERS_URL
        )

        parser = BundesligaPlayersParser()
        parser.feed(html)

        squads = {
            team_name: []
            for team_name in BUNDESLIGA_NAME_MAP.values()
        }

        for player in parser.players:
            club = player["club"]

            if club not in squads:
                squads[club] = []

            squads[club].append(
                {
                    "id": player["id"],
                    "name": player["name"],
                    "age": None,
                    "number": player["number"],
                    "position": player["position"],
                    "photo": None,
                    "injury": None,
                    "sourceUrl": player["sourceUrl"],
                }
            )

        for team_name, players in squads.items():
            unique = {}

            for player in players:
                key = (
                    normalize_name(
                        player.get("name")
                    ),
                    player.get("position"),
                )
                unique[key] = player

            squads[team_name] = list(
                unique.values()
            )

        total_players = sum(
            len(players)
            for players in squads.values()
        )

        print(
            "Bundesliga.com: "
            f"{len(parser.players)} Spieler gefunden."
        )
        print(
            "Bundesliga.com: "
            f"{total_players} Spieler nach Bereinigung."
        )

        return squads

    except Exception as exc:
        print(
            "Bundesliga.com Kader "
            f"fehlgeschlagen: {exc}"
        )
        return {}


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
    Holt die aktuelle Clubübersicht direkt von Bundesliga.com.

    Keine API-Football-Team-Suche mehr.
    Fallback: OpenLigaDB.
    """

    try:
        html = http_get_text(
            BUNDESLIGA_CLUBS_URL
        )

        parser = BundesligaClubParser()
        parser.feed(html)

        teams = []
        seen = set()

        for raw_name in parser.club_names:
            key = normalize_name(raw_name)
            team_name = BUNDESLIGA_NAME_MAP.get(
                key,
                raw_name.strip(),
            )

            normalized_team = normalize_name(
                team_name
            )

            if normalized_team in seen:
                continue

            seen.add(normalized_team)

            teams.append(
                {
                    "teamName": team_name,
                    "source": "Bundesliga.com",
                }
            )

        if len(teams) == 18:
            print(
                "Bundesliga.com: 18 Clubs gefunden."
            )
            return teams

        print(
            "WARNUNG: Bundesliga.com lieferte "
            f"{len(teams)} Clubs statt 18."
        )

    except Exception as exc:
        print(
            "Bundesliga.com Clubübersicht "
            f"fehlgeschlagen: {exc}"
        )

    try:
        matches = openligadb_get(
            f"/getmatchdata/"
            f"{BUNDESLIGA_SHORTCUT}/"
            f"{OPENLIGADB_SEASON}"
        )

        discovered = {}

        for match in matches or []:
            for side in ("team1", "team2"):
                team = match.get(
                    side,
                    {},
                )
                name = team.get(
                    "teamName",
                    "",
                )

                if name:
                    discovered[
                        normalize_name(name)
                    ] = {
                        "teamName": name,
                        "source": "OpenLigaDB",
                    }

        teams = list(
            discovered.values()
        )

        print(
            "OpenLigaDB-Fallback: "
            f"{len(teams)} Clubs gefunden."
        )

        return teams

    except Exception as exc:
        print(
            "OpenLigaDB-Fallback "
            f"fehlgeschlagen: {exc}"
        )
        return []




BUNDESLIGA_CLUB_SQUAD_URLS = {
    "SV Elversberg": (
        "https://www.bundesliga.com/de/bundesliga/"
        "clubs/sv-elversberg"
    ),
    "Sport-Club Freiburg": (
        "https://www.bundesliga.com/de/bundesliga/"
        "clubs/sport-club-freiburg"
    ),
}


def get_club_page_squad(
    team_name,
):
    """
    Fallback für einen einzelnen Club.

    Die offizielle Bundesliga-Clubseite enthält den Kader
    ebenfalls. Wir setzen den Clubnamen hier direkt, weil
    die Überschrift auf der Clubseite nicht zwingend genauso
    strukturiert ist wie auf der zentralen Spielerübersicht.
    """

    url = BUNDESLIGA_CLUB_SQUAD_URLS.get(
        team_name
    )

    if not url:
        return []

    try:
        html = http_get_text(url)

        parser = BundesligaPlayersParser()
        parser.current_club = team_name
        parser.feed(html)

        players = []

        for player in parser.players:
            if player.get("club") != team_name:
                continue

            players.append(
                {
                    "id": player["id"],
                    "name": player["name"],
                    "age": None,
                    "number": player["number"],
                    "position": player["position"],
                    "photo": None,
                    "injury": None,
                    "sourceUrl": player["sourceUrl"],
                }
            )

        unique = {}

        for player in players:
            key = (
                normalize_name(
                    player.get("name")
                ),
                player.get("position"),
            )
            unique[key] = player

        players = list(
            unique.values()
        )

        if players:
            print(
                f"Bundesliga.com Clubseite: "
                f"{team_name}: "
                f"{len(players)} Spieler"
            )

        return players

    except Exception as exc:
        print(
            f"Bundesliga.com Clubseite "
            f"{team_name} fehlgeschlagen: {exc}"
        )
        return []


def complete_missing_bundesliga_squads(
    squads,
):
    """
    Ergänzt fehlende Kader über die offiziellen
    individuellen Clubseiten.

    Der zentrale Bundesliga-Spielerindex bleibt die
    primäre Quelle. Dieser Fallback wird nur für Clubs
    verwendet, die dort keinen Kader geliefert haben.
    """

    result = dict(squads)

    for team_name, url in (
        BUNDESLIGA_CLUB_SQUAD_URLS.items()
    ):
        current = result.get(
            team_name,
            [],
        )

        if current:
            continue

        print(
            "Kader-Fallback für "
            f"{team_name}..."
        )

        fallback_players = (
            get_club_page_squad(
                team_name
            )
        )

        if fallback_players:
            result[team_name] = (
                fallback_players
            )

    return result


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

# API-Football wird in dieser Version NICHT mehr für Teams
# oder Kader verwendet.
#
# Die Bundesliga-Teams und Kader kommen direkt von
# Bundesliga.com. OpenLigaDB liefert weiterhin den Spielplan.
#
# apiTeamId/apiTeamName bleiben in der JSON erhalten, damit
# ältere Verbraucher der Datei nicht sofort brechen. Sie werden
# hier aber nicht mehr abgefragt.

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

    print(
        "Lade Bundesliga-Kader von "
        "Bundesliga.com..."
    )

    bundesliga_squads = (
        get_bundesliga_squads()
    )

    bundesliga_squads = (
        complete_missing_bundesliga_squads(
            bundesliga_squads
        )
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

        old_entry = old_teams.get(
            team_name,
            {},
        )

        # ----------------------------------------------------
        # KADER VON BUNDESLIGA.COM
        # ----------------------------------------------------

        squad = bundesliga_squads.get(
            team_name,
            [],
        )

        # Falls die offizielle Seite kurzfristig einen
        # einzelnen Club nicht liefert, behalten wir den
        # vorhandenen Kader aus der letzten JSON.
        if not squad:
            old_squad = old_entry.get(
                "players",
                [],
            )

            if old_squad:
                squad = old_squad
                print(
                    "Bundesliga.com lieferte "
                    "keinen Kader; alter Kader "
                    "wird beibehalten."
                )

        print(
            f"Kader: "
            f"{len(squad)} Spieler"
        )

        # Alte IDs bleiben aus Kompatibilitätsgründen erhalten.
        api_team_id = old_entry.get(
            "apiTeamId"
        )
        api_team_name = old_entry.get(
            "apiTeamName"
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
                    "title": "Bundesliga.com",
                    "url": (
                        BUNDESLIGA_PLAYERS_URL
                    ),
                },
                {
                    "title": "OpenLigaDB",
                    "url": (
                        "https://www.openligadb.de/"
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

    missing_squads = []

    for name, entry in (
        data["teams"].items()
    ):
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
        "Teams mit Bundesliga-Kader: "
        f"{18 - len(missing_squads)}/18"
    )

    print(
        "API-Football-Team-IDs werden "
        "für Kader nicht verwendet."
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
