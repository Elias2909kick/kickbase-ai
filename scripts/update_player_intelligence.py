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
API_FOOTBALL_LEAGUE_ID = 78  # Bundesliga

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
    # Bundesliga.com / OpenLigaDB verwenden hier teils
    # unterschiedliche Schreibweisen. Alle Varianten werden
    # auf den Namen des jeweiligen Bundesliga-Spielplans
    # vereinheitlicht.
    "sv elversberg": "SV 07 Elversberg",
    "sv 07 elversberg": "SV 07 Elversberg",

    "eintracht frankfurt": "Eintracht Frankfurt",

    "sport club freiburg": "SC Freiburg",
    "sc freiburg": "SC Freiburg",
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
    Robuster Parser für Bundesliga.com.

    Wichtig: Die Clubnamen werden NICHT mehr nur anhand von
    h2/h3 erkannt. Bundesliga.com kann die Überschriften je
    nach Seite unterschiedlich ausliefern. Deshalb erkennen
    wir Clubnamen und Positionsüberschriften anhand ihres
    Textinhalts.
    """

    def __init__(self):
        super().__init__()

        self.players = []
        self.current_club = None
        self.current_position = None

        self._anchor_href = None
        self._anchor_parts = []

        self._tag_stack = []

        # Sowohl alle bekannten Aliasnamen (Dictionary-Keys)
        # als auch die kanonischen Namen (Dictionary-Values)
        # müssen erkannt werden. Genau das war der Fehler bei
        # SV Elversberg und Sport-Club Freiburg:
        #
        # Bundesliga.com: "SV Elversberg"
        # intern:         "SV 07 Elversberg"
        #
        # Bundesliga.com: "Sport-Club Freiburg"
        # intern:         "SC Freiburg"
        self.club_lookup = {}

        for alias, canonical in (
            BUNDESLIGA_NAME_MAP.items()
        ):
            self.club_lookup[
                normalize_name(alias)
            ] = canonical

            self.club_lookup[
                normalize_name(canonical)
            ] = canonical

        self.position_lookup = {
            normalize_name(
                value
            ): value
            for value in POSITION_MAP.values()
        }

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)

        self._tag_stack.append(tag)

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

        if not value:
            return

        normalized = normalize_name(value)

        # --------------------------------------------------------
        # Club erkennen – unabhängig von h2/h3/div/span usw.
        # --------------------------------------------------------
        mapped_club = self.club_lookup.get(
            normalized
        )

        if mapped_club:
            self.current_club = mapped_club
            self.current_position = None

        # --------------------------------------------------------
        # Position erkennen
        # --------------------------------------------------------
        mapped_position = self.position_lookup.get(
            normalized
        )

        if mapped_position:
            self.current_position = mapped_position

        # --------------------------------------------------------
        # Spieler-Link sammeln
        # --------------------------------------------------------
        if self._anchor_href is not None:
            self._anchor_parts.append(value)

    def handle_endtag(self, tag):
        if (
            tag == "a"
            and self._anchor_href is not None
        ):
            raw_text = " ".join(
                self._anchor_parts
            ).strip()

            if (
                raw_text
                and self.current_club
                and self.current_position
            ):
                number = None
                name = raw_text

                # Bundesliga.com kann Nummer und Name ohne
                # Trennzeichen ausliefern, z.B. "1FinnDahmen".
                match = re.match(
                    r"^(\d{1,2})(.+)$",
                    raw_text,
                )

                if match:
                    number = int(
                        match.group(1)
                    )
                    name = match.group(2).strip()

                # Eventuelle zusammengeklebte Namen etwas
                # lesbarer machen.
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

        if self._tag_stack:
            # HTMLParser kann bei fehlerhaftem HTML verschachtelte
            # Tags sehen; deshalb nur den letzten Tag entfernen.
            if self._tag_stack[-1] == tag:
                self._tag_stack.pop()
            elif tag in self._tag_stack:
                self._tag_stack.remove(tag)




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

        for team_name in BUNDESLIGA_NAME_MAP.values():
            count = len(
                squads.get(
                    team_name,
                    [],
                )
            )
            print(
                f"  - {team_name}: "
                f"{count} Spieler"
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
# API-FOOTBALL: SPIELER-INTELLIGENCE
# ============================================================

def api_football_get(endpoint, params=None):
    """
    Führt einen API-Football-Request mit Rate-Limit und Fehlerbehandlung aus.

    Wichtig:
    - Kein Zugriff auf Kickbase.
    - API-Football wird nur für öffentliche Fußball-/Spielerdaten verwendet.
    - Bei fehlendem API-Key wird sauber übersprungen.
    """

    global _last_api_call

    if not API_FOOTBALL_KEY:
        return None

    elapsed = time.time() - _last_api_call
    if elapsed < API_MIN_INTERVAL:
        time.sleep(API_MIN_INTERVAL - elapsed)

    query = urlencode(params or {})
    url = f"{API_FOOTBALL_URL}/{endpoint.lstrip('/')}"
    if query:
        url = f"{url}?{query}"

    try:
        result = http_get_json(
            url,
            headers={
                "x-apisports-key": API_FOOTBALL_KEY,
            },
        )
        _last_api_call = time.time()
        return result

    except (HTTPError, URLError, TimeoutError) as exc:
        _last_api_call = time.time()
        print(
            "API-Football Request fehlgeschlagen: "
            f"{endpoint}: {exc}"
        )
        return None
    except Exception as exc:
        _last_api_call = time.time()
        print(
            "API-Football unerwarteter Fehler: "
            f"{endpoint}: {exc}"
        )
        return None


def get_api_football_players():
    """
    Lädt die Bundesliga-Spieler inkl. Saisonstatistiken.

    Der /players-Endpunkt liefert Profil + Saisonstatistiken und ist
    paginiert. Wir laden deshalb alle Seiten der Bundesliga-Saison.

    Das Ergebnis wird nach normalisiertem Spielernamen indiziert.
    """

    if not API_FOOTBALL_KEY:
        print(
            "API_FOOTBALL_KEY fehlt. "
            "Player-Statistiken werden übersprungen."
        )
        return {}

    first = api_football_get(
        "players",
        {
            "league": API_FOOTBALL_LEAGUE_ID,
            "season": OPENLIGADB_SEASON,
            "page": 1,
        },
    )

    if not first:
        return {}

    response = first.get("response") or []
    paging = first.get("paging") or {}
    total_pages = int(paging.get("total") or 1)

    all_players = list(response)

    print(
        "API-Football: "
        f"Seite 1/{total_pages}, "
        f"{len(response)} Spieler."
    )

    for page in range(2, total_pages + 1):
        payload = api_football_get(
            "players",
            {
                "league": API_FOOTBALL_LEAGUE_ID,
                "season": OPENLIGADB_SEASON,
                "page": page,
            },
        )

        if not payload:
            print(
                f"API-Football: Seite {page} konnte "
                "nicht geladen werden."
            )
            continue

        page_players = payload.get("response") or []
        all_players.extend(page_players)

        print(
            "API-Football: "
            f"Seite {page}/{total_pages}, "
            f"{len(page_players)} Spieler."
        )

    index = {}

    for entry in all_players:
        player = entry.get("player") or {}
        name = player.get("name") or ""

        if not name:
            continue

        key = normalize_name(name)
        index[key] = entry

    print(
        "API-Football: "
        f"{len(index)} Spieler indexiert."
    )

    return index


def get_api_football_injuries():
    """
    Lädt aktuelle Verletzungen/Sperren der Bundesliga.

    Die API liefert bei /injuries sowohl type=Injury als auch
    type=Suspension. Deshalb können wir beide UI-Felder getrennt
    befüllen, ohne Kickbase direkt abzufragen.
    """

    if not API_FOOTBALL_KEY:
        return {}

    first = api_football_get(
        "injuries",
        {
            "league": API_FOOTBALL_LEAGUE_ID,
            "season": OPENLIGADB_SEASON,
            "page": 1,
        },
    )

    if not first:
        return {}

    all_rows = list(first.get("response") or [])
    paging = first.get("paging") or {}
    total_pages = int(paging.get("total") or 1)

    for page in range(2, total_pages + 1):
        payload = api_football_get(
            "injuries",
            {
                "league": API_FOOTBALL_LEAGUE_ID,
                "season": OPENLIGADB_SEASON,
                "page": page,
            },
        )

        if payload:
            all_rows.extend(payload.get("response") or [])

    index = {}

    for row in all_rows:
        player = row.get("player") or {}
        team = row.get("team") or {}

        name = player.get("name") or ""
        if not name:
            continue

        key = normalize_name(name)
        item = {
            "type": row.get("type"),
            "reason": row.get("reason"),
            "team": team.get("name"),
        }

        # Falls ein Spieler mehrere Einträge hat, behalten wir
        # Verletzung und Sperre getrennt.
        existing = index.setdefault(
            key,
            {
                "injury": None,
                "suspension": None,
            },
        )

        row_type = normalize_name(row.get("type"))

        if "suspension" in row_type:
            existing["suspension"] = item
        else:
            existing["injury"] = item

    print(
        "API-Football: "
        f"{len(index)} Spieler mit Verletzungs-/Sperrinfos."
    )

    return index


def find_api_player(api_players, player_name, club_name):
    """Findet den API-Football-Spieler zuerst exakt, dann über Namensteile."""

    if not api_players:
        return None

    exact = api_players.get(
        normalize_name(player_name)
    )

    if exact:
        return exact

    normalized = normalize_name(player_name)
    if not normalized:
        return None

    # Fallback für Schreibweisen wie Initialen oder Bindestriche.
    candidates = []
    for key, entry in api_players.items():
        api_name = (entry.get("player") or {}).get("name", "")
        if names_match(player_name, api_name):
            candidates.append(entry)

    if not candidates:
        return None

    # Bei Namensgleichheit den Verein bevorzugen.
    for entry in candidates:
        for stat in entry.get("statistics") or []:
            team = stat.get("team") or {}
            if names_match(club_name, team.get("name", "")):
                return entry

    return candidates[0]


def choose_api_stat_block(api_entry, club_name):
    """Wählt den Bundesliga-Statistikblock des aktuellen Vereins."""

    if not api_entry:
        return None

    blocks = api_entry.get("statistics") or []
    if not blocks:
        return None

    matching = []

    for block in blocks:
        league = block.get("league") or {}
        team = block.get("team") or {}

        if (
            league.get("id") == API_FOOTBALL_LEAGUE_ID
            and names_match(club_name, team.get("name", ""))
        ):
            matching.append(block)

    if matching:
        return max(
            matching,
            key=lambda item: (
                item.get("games") or {}
            ).get("minutes") or 0,
        )

    bundesliga_blocks = [
        block
        for block in blocks
        if (block.get("league") or {}).get("id")
        == API_FOOTBALL_LEAGUE_ID
    ]

    if bundesliga_blocks:
        return max(
            bundesliga_blocks,
            key=lambda item: (
                item.get("games") or {}
            ).get("minutes") or 0,
        )

    return None


def parse_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_recommendation(
    player,
    next_match,
    stat_block,
    injury_info,
    suspension_info,
):
    """
    Ein transparenter erster Empfehlungs-Score.

    Noch bewusst kein Kickbase-Punkte-Score: echte Kickbase-
    Durchschnittspunkte stehen uns ohne Kickbase-Datenquelle nicht
    zur Verfügung und werden deshalb nicht erfunden.
    """

    if suspension_info:
        return "Nicht aufstellen – gesperrt"

    if injury_info:
        return "Vorsicht – verletzt"

    if not stat_block:
        return (
            "Nächstes Spiel vorhanden"
            if next_match
            else "Noch nicht ausreichend Daten"
        )

    games = stat_block.get("games") or {}
    appearances = games.get("appearences") or 0
    starts = games.get("lineups") or 0
    minutes = games.get("minutes") or 0
    rating = parse_float(games.get("rating"))

    start_rate = (
        starts / appearances
        if appearances
        else 0
    )

    if rating is not None and start_rate >= 0.65:
        return "Gute Option"

    if rating is not None and start_rate >= 0.35:
        return "Beobachten"

    if appearances == 0:
        return "Noch keine Saison-Einsätze"

    if minutes < 180:
        return "Rotationsrisiko"

    return "Beobachten"


# ============================================================
# INTELLIGENCE
# ============================================================

def build_intelligence(
    squad,
    next_match,
):
    """
    Baut weiterhin die Team-Intelligence.

    Die eigentlichen Spieler werden separat unter
    data["players"] gespeichert. Dadurch kann das Frontend
    später direkt auf einen einzelnen Kickbase-Spieler zugreifen.
    """

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
            "Noch nicht recherchiert"
        ),
        "suspension": (
            "Noch nicht recherchiert"
        ),
        "recommendation": recommendation,
        "players": squad,
    }


def build_player_intelligence(
    player,
    club_name,
    next_match,
    api_entry=None,
    injury_entry=None,
):
    """
    Baut einen einzelnen Spieler-Eintrag.

    Quellen:
    - Bundesliga.com: Spieler, Verein, Position, stabile Zuordnung
    - OpenLigaDB: nächster Gegner + Heim/Auswärts
    - API-Football: Saisonrating, Einsätze, Startelf, Minuten,
      Verletzungs-/Sperrstatus

    Wichtig:
    Echte Kickbase-Ø-Punkte werden NICHT erfunden. Dafür bräuchten
    wir eine zulässige Kickbase-Datenquelle. Das Feld "average"
    bleibt deshalb None, bis wir eine solche Quelle haben.
    """

    player_id = player.get("id")
    name = player.get(
        "name",
        "Unbekannter Spieler",
    )

    stat_block = choose_api_stat_block(
        api_entry,
        club_name,
    )

    player_profile = (
        (api_entry or {}).get("player") or {}
    )

    current_injury = None
    current_suspension = None

    if injury_entry:
        current_injury = injury_entry.get("injury")
        current_suspension = injury_entry.get("suspension")

    if player_profile.get("injured"):
        if not current_injury:
            current_injury = {
                "type": "Injury",
                "reason": "Aktuell verletzt",
            }

    if next_match:
        opponent = next_match.get("opponent")
        home_away = next_match.get("homeAway")
    else:
        opponent = None
        home_away = None

    starting = None
    form = "Noch nicht recherchiert"
    football_rating = None
    appearances = None
    starts = None
    minutes = None
    goals = None
    assists = None

    if stat_block:
        games = stat_block.get("games") or {}
        goals_data = stat_block.get("goals") or {}

        appearances = games.get("appearences")
        starts = games.get("lineups")
        minutes = games.get("minutes")
        goals = goals_data.get("total")
        assists = goals_data.get("assists")

        rating = parse_float(
            games.get("rating")
        )

        football_rating = rating

        if appearances:
            start_rate = (
                (starts or 0) / appearances
            )
            starting = round(
                start_rate * 100
            )

        if rating is not None:
            form = f"{rating:.2f}/10"

    injury_text = (
        current_injury.get("reason")
        if current_injury
        else "Keine aktuelle Verletzung"
    )

    suspension_text = (
        current_suspension.get("reason")
        if current_suspension
        else "Keine aktuelle Sperre"
    )

    recommendation = build_recommendation(
        player,
        next_match,
        stat_block,
        current_injury,
        current_suspension,
    )

    return {
        "id": player_id,
        "name": name,
        "club": club_name,
        "position": player.get("position"),
        "number": player.get("number"),
        "sourceUrl": player.get("sourceUrl"),

        # Bewusst None: keine erfundenen Kickbase-Punkte.
        "average": None,

        # Neue öffentliche Fußball-Daten.
        "starting": (
            f"{starting}%"
            if starting is not None
            else "Noch nicht recherchiert"
        ),
        "form": form,
        "footballRating": football_rating,
        "appearances": appearances,
        "starts": starts,
        "minutes": minutes,
        "goals": goals,
        "assists": assists,

        "opponent": opponent,
        "homeAway": home_away,
        "injury": injury_text,
        "suspension": suspension_text,
        "recommendation": recommendation,

        "lastUpdated": datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%d"),

        "sources": [
            {
                "name": "Bundesliga.com",
                "url": BUNDESLIGA_PLAYERS_URL,
            },
            {
                "name": "OpenLigaDB",
                "url": "https://www.openligadb.de/",
            },
            {
                "name": "API-Football",
                "url": API_FOOTBALL_URL,
            },
        ],
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

    print()
    print("Lade öffentliche Player-Statistiken...")
    api_players = get_api_football_players()

    print("Lade aktuelle Verletzungen und Sperren...")
    api_injuries = get_api_football_injuries()

    old_teams = load_old_data()

    # Neue, saubere Datenbank.
    data = {
        "teams": {},
        "players": {},
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
        # SPIELER-DATEN
        # ----------------------------------------------------
        player_count_before = len(
            data["players"]
        )

        for player in squad:
            player_id = player.get("id")

            if not player_id:
                continue

            api_entry = find_api_player(
                api_players,
                player.get("name", ""),
                team_name,
            )

            injury_entry = api_injuries.get(
                normalize_name(
                    player.get("name", "")
                )
            )

            data["players"][player_id] = (
                build_player_intelligence(
                    player,
                    team_name,
                    next_match,
                    api_entry=api_entry,
                    injury_entry=injury_entry,
                )
            )

        player_count_after = len(
            data["players"]
        )

        print(
            "Spieler-Intelligence: "
            f"{player_count_after - player_count_before} "
            "Spieler hinzugefügt."
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

            "playerCount": len(squad),

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

    data["playerCount"] = len(
        data["players"]
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
        "Spieler gespeichert: "
        f"{len(data['players'])}"
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

        print()
        print(
            "HINWEIS: Wenn hier ein Team fehlt, "
            "ist die Zuordnung auf Bundesliga.com "
            "fehlgeschlagen."
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
        f"{updated_teams} Teams aktualisiert."
    )

    print(
        f"{len(data['players'])} Spieler "
        "gespeichert."
    )


if __name__ == "__main__":
    main()
