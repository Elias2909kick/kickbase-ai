import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, parse_qs, unquote, urlencode
from urllib.parse import unquote

# ============================================================
# KONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"
# Optional: IDs des aktuellen Kickbase-Kaders für intensive Web-Recherche.
# Die Datei ist bewusst optional, weil GitHub Actions keinen Zugriff auf browser-localStorage hat.
ACTIVE_ROSTER_FILE = BASE_DIR / "kickbase-roster.json"

OPENLIGADB_URL = "https://api.openligadb.de"
BUNDESLIGA_SHORTCUT = "bl1"
CURRENT_SEASON = "2026/27"
OPENLIGADB_SEASON = 2026

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
# OFFIZIELLE PERSONAL-/STATUSQUELLEN – GENERISCH
# ============================================================
#
# Ziel:
# - für JEDEN Kaderspieler dieselbe Logik
# - nur offizielle Vereinsdomains als Statusquelle
# - Sitemap / News-Seiten zuerst, Suchmaschine nur als Link-Fallback
# - keine spielerspezifischen Hardcodes
#
# Bereits bestätigte Verletzungen aus der vorhandenen JSON werden solange
# beibehalten, bis eine neuere belastbare Rückkehr-/Fit-Meldung gefunden wird.

OFFICIAL_CLUB_DOMAINS = {
    "FC Augsburg": "fcaugsburg.de",
    "1. FC Union Berlin": "fc-union-berlin.de",
    "SV Werder Bremen": "werder.de",
    "Borussia Dortmund": "bvb.de",
    "SV 07 Elversberg": "sv07elversberg.de",
    "Eintracht Frankfurt": "eintracht.de",
    "SC Freiburg": "scfreiburg.com",
    "Hamburger SV": "hsv.de",
    "TSG Hoffenheim": "tsg-hoffenheim.de",
    "1. FC Köln": "fc.de",
    "RB Leipzig": "rbleipzig.com",
    "Bayer 04 Leverkusen": "bayer04.de",
    "1. FSV Mainz 05": "mainz05.de",
    "Borussia Mönchengladbach": "borussia.de",
    "FC Bayern München": "fcbayern.com",
    "SC Paderborn 07": "scp07.de",
    "FC Schalke 04": "schalke04.de",
    "VfB Stuttgart": "vfb.de",
}

# Bekannte offizielle Seiten dienen nur als zusätzliche Seeds.
# Keine dieser Seiten ist spielerspezifisch im Auswertungsalgorithmus.
OFFICIAL_STATUS_SEEDS = {
    "SV Werder Bremen": [
        "https://www.werder.de/news/maenner/2026-2027/personal-update-260826",
        "https://www.werder.de/news/maenner/2026-2027/personal-lueneburg-20082026",
    ],
}

STATUS_URL_KEYWORDS = (
    "personal",
    "verletz",
    "reha",
    "training",
    "aufstellung",
    "kader",
    "vorbericht",
    "presse",
    "spieltag",
    "team",
    "news",
)

SEARCH_URL = "https://html.duckduckgo.com/html/"

# V13 FAST MODE
# Ziel: kompletter Kader-Refresh in wenigen Minuten statt >15 Minuten.
STATUS_SEARCH_TIMEOUT = 5
STATUS_PAGE_TIMEOUT = 6
STATUS_MAX_SEARCH_QUERIES = 1
STATUS_MAX_CANDIDATE_URLS = 4
STATUS_MAX_PAGES_PER_PLAYER = 3
STATUS_GLOBAL_BUDGET_SECONDS = 120

# V14: eine einzige Bundesliga-Spieltagseite statt Websuche pro Spieler.
BUNDESLIGA_MATCHDAY_STATUS_TEMPLATE = (
    "https://www.bundesliga.com/de/bundesliga/news/"
    "voraussichtliche-aufstellungen-spieltag-verletzungen-"
    "sperren-ubersicht-{matchday}-24397"
)

_MATCHDAY_STATUS_CACHE = {}
_TEAMCHECK_CACHE = {}

# V19: Ligaweite Bundesliga-Statistikseiten werden pro Kategorie nur EINMAL
# geladen und anschließend für alle aktiven Kaderspieler wiederverwendet.
_BUNDESLIGA_STATS_TEXT_CACHE = {}

BUNDESLIGA_STATS_SEASON = "2026-2027"

BUNDESLIGA_PLAYER_STAT_CATEGORIES = {
    "goals": {
        "slug": "tore",
        "heading": "Tore",
        "kind": "count",
    },
    "assists": {
        "slug": "vorlagen",
        "heading": "Vorlagen",
        "kind": "count",
    },
    "shots": {
        "slug": "torschuesse",
        "heading": "Torschüsse",
        "kind": "count",
    },
    "woodwork": {
        "slug": "pfosten-oder-lattentreffer",
        "heading": "Pfosten- oder Lattentreffer",
        "kind": "count",
    },
    "penalties": {
        "slug": "elfmeter",
        "heading": "Elfmeter",
        "kind": "count",
    },
    "penaltiesScored": {
        "slug": "verwandelte-elfmeter",
        "heading": "Verwandelte Elfmeter",
        "kind": "count",
    },
    "passAccuracy": {
        "slug": "passquote",
        "heading": "Passquote",
        "kind": "rate",
    },
    "duelsWon": {
        "slug": "gewonnene-zweikaempfe",
        "heading": "Gewonnene Zweikämpfe",
        "kind": "count",
    },
    "aerialDuelsWon": {
        "slug": "gewonnene-kopfballduelle",
        "heading": "Gewonnene Kopfballduelle",
        "kind": "count",
    },
    "crosses": {
        "slug": "flanken-aus-dem-spiel",
        "heading": "Flanken aus dem Spiel",
        "kind": "count",
    },
    "yellowCards": {
        "slug": "gelbe-karten",
        "heading": "Gelbe Karten",
        "kind": "count",
    },
    "cards": {
        "slug": "karten",
        "heading": "Karten",
        "kind": "count",
    },
    "fouls": {
        "slug": "fouls-am-gegner",
        "heading": "Fouls am Gegner",
        "kind": "count",
    },
    "saves": {
        "slug": "gehaltene-torschuesse",
        "heading": "Gehaltene Torschüsse",
        "kind": "count",
    },
}

BUNDESLIGA_CLUB_NEWS_SLUGS = {
    "FC Bayern München": "fc-bayern-muenchen",
    "VfB Stuttgart": "vfb-stuttgart",
    "RB Leipzig": "rb-leipzig",
    "Borussia Mönchengladbach": "borussia-moenchengladbach",
    "1. FSV Mainz 05": "1-fsv-mainz-05",
    "SC Paderborn 07": "sc-paderborn-07",
    "1. FC Union Berlin": "1-fc-union-berlin",
    "Eintracht Frankfurt": "eintracht-frankfurt",
    "1. FC Köln": "1-fc-koeln",
    "TSG Hoffenheim": "tsg-hoffenheim",
    "SV 07 Elversberg": "sv-elversberg",
    "Bayer 04 Leverkusen": "bayer-04-leverkusen",
    "Borussia Dortmund": "borussia-dortmund",
    "Hamburger SV": "hamburger-sv",
    "SC Freiburg": "sc-freiburg",
    "SV Werder Bremen": "sv-werder-bremen",
    "FC Augsburg": "fc-augsburg",
    "FC Schalke 04": "fc-schalke-04",
}
_STATUS_RESEARCH_STARTED_AT = None

LEGACY_NO_INJURY_TEXTS = {
    "keine verletzungsmeldung auf dem profil gefunden",
    "keine verletzungsmeldung gefunden",
}

LEGACY_NO_SUSPENSION_TEXTS = {
    "keine sperrmeldung auf dem profil gefunden",
    "keine sperrmeldung gefunden",
}

INJURY_DIAGNOSES = (
    ("kreuzbandriss", "Kreuzbandriss"),
    ("riss des vorderen kreuzbandes", "Kreuzbandriss"),
    ("muskelfaserriss", "Muskelfaserriss"),
    ("muskelbündelriss", "Muskelbündelriss"),
    ("muskelverletzung", "Muskelverletzung"),
    ("knieverletzung", "Knieverletzung"),
    ("sprunggelenksverletzung", "Sprunggelenksverletzung"),
    ("schulterverletzung", "Schulterverletzung"),
    ("bänderriss", "Bänderriss"),
    ("baenderriss", "Bänderriss"),
    ("fraktur", "Fraktur"),
)

CURRENT_INJURY_TERMS = (
    "verletzt",
    "verletzung",
    "fällt aus",
    "faellt aus",
    "fehlt weiterhin",
    "nicht zur verfügung",
    "nicht zur verfuegung",
    "in der reha",
    "in reha",
    "rehabilitation",
)

CURRENT_SUSPENSION_TERMS = (
    "gesperrt",
    "sperre",
    "rotsperre",
    "gelbsperre",
    "5. gelbe karte",
    "fünfte gelbe karte",
    "fuenfte gelbe karte",
)

RECOVERY_TERMS = (
    "wieder im mannschaftstraining",
    "zurück im mannschaftstraining",
    "zurueck im mannschaftstraining",
    "wieder einsatzbereit",
    "voll belastbar",
)

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

    Keine externe Team-ID-Suche mehr.
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
# ÖFFENTLICHE PLAYER-INTELLIGENCE V10
# ============================================================

# Bewusst ohne kostenpflichtige Fußball-API-Abhängigkeit.
#
# Die Architektur verwendet:
# - Bundesliga.com: aktuelle Bundesliga-Kader und Spieler-Stammdaten
# - OpenLigaDB: Spielplan, nächster Gegner, Heim/Auswärts
# - öffentliche Bundesliga-Statistikseite als dokumentierte Zusatzquelle
#
# Echte Kickbase-Ø-Punkte werden nicht erfunden. Ebenso werden
# Verletzungen, Sperren, Startelf und Form nicht aus einem anderen
# Wert abgeleitet, wenn keine belastbare öffentliche Information
# vorliegt. Stattdessen bleibt das Feld sauber auf
# "Noch nicht recherchiert".

BUNDESLIGA_STATS_URL = (
    "https://www.bundesliga.com/de/bundesliga/"
    "statistiken/spieler/2026-2027"
)


def get_public_player_intelligence_source():
    """
    Prüft die offizielle Bundesliga-Statistikseite als öffentliche
    Zusatzquelle. Die Seite wird nicht als Kickbase-Datenquelle
    missbraucht und es werden keine Werte erfunden.
    """
    try:
        html = http_get_text(BUNDESLIGA_STATS_URL)
        if html and len(html) > 500:
            print(
                "Bundesliga.com Statistikseite: erreichbar."
            )
            return {
                "available": True,
                "url": BUNDESLIGA_STATS_URL,
            }
    except Exception as exc:
        print(
            "Bundesliga.com Statistikseite nicht erreichbar: "
            f"{exc}"
        )

    return {
        "available": False,
        "url": BUNDESLIGA_STATS_URL,
    }


def build_public_recommendation(
    next_match,
    old_player=None,
):
    """
    Konservative Empfehlung ohne erfundene Spielerwerte.

    Bereits vorhandene belastbare Empfehlung aus der alten JSON
    wird erhalten. Andernfalls wird nur die sichere Spieltags-
    Information verwendet.
    """
    old_player = old_player or {}

    old_recommendation = old_player.get(
        "recommendation"
    )

    if old_recommendation and old_recommendation != "Noch nicht recherchiert":
        return old_recommendation

    if next_match:
        return "Nächstes Spiel vorhanden"

    return "Noch nicht ausreichend Daten"


# ============================================================
# ALTE JSON LADEN
# ============================================================

def load_old_data():
    """
    Liest die alte JSON-Datei.

    Die komplette alte JSON wird geladen, damit bereits
    vorhandene Intelligence-Werte nicht unnötig verloren gehen.
    """

    if not INTELLIGENCE_FILE.exists():
        return {}

    try:

        with INTELLIGENCE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(file)

        return data

    except Exception as exc:

        print(
            "Alte player-intelligence.json "
            "konnte nicht geladen werden: "
            f"{exc}"
        )

        return {}


# ============================================================
# PLAYER-INTELLIGENCE: ÖFFENTLICHE DATEN
# ============================================================

# Keine kostenpflichtige Fußball-API-Abhängigkeit.
# Die Spieler-Stammdaten kommen von Bundesliga.com, der Spielplan
# von OpenLigaDB. Öffentliche Bundesliga-Statistiken werden nur
# als zusätzliche Quelle geprüft; fehlende Werte bleiben leer bzw.
# "Noch nicht recherchiert" und werden nicht erfunden.

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
        "starting": "Noch nicht recherchiert",
        "form": "Noch nicht recherchiert",
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



def parse_number_after_label(text, label):
    """
    Liest einfache Bundesliga.com-Spielerprofil-Werte aus dem
    sichtbaren Text. Beispiel: 'Einsätze 2'.
    """
    pattern = rf"{re.escape(label)}\s+(\d+(?:[.,]\d+)?)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return None

    value = match.group(1).replace(",", ".")
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def parse_number_after_labels(text, labels):
    """Probiert mehrere Sprachvarianten derselben Statistik."""
    for label in labels:
        value = parse_number_after_label(text, label)
        if value is not None:
            return value
    return None



def parse_float_after_labels(text, labels):
    """Liest einen numerischen Wert hinter einem von mehreren Labels."""
    for label in labels:
        pattern = rf"{re.escape(label)}\s+(\d+(?:[.,]\d+)?)"
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            raw = match.group(1).replace(",", ".")
            try:
                value = float(raw)
                return int(value) if value.is_integer() else value
            except ValueError:
                pass
    return None



def _bundesliga_stat_url(slug):
    return (
        "https://www.bundesliga.com/de/bundesliga/statistiken/"
        f"spieler/{slug}/{BUNDESLIGA_STATS_SEASON}"
    )


def _get_bundesliga_stat_text(metric_key):
    """
    Lädt eine Bundesliga-Rankingseite einmal und cached den geglätteten Text.
    Bei nicht erreichbarer Seite wird None gecached, damit nicht wiederholt
    angefragt wird.
    """
    if metric_key in _BUNDESLIGA_STATS_TEXT_CACHE:
        return _BUNDESLIGA_STATS_TEXT_CACHE[metric_key]

    config = BUNDESLIGA_PLAYER_STAT_CATEGORIES.get(metric_key)
    if not config:
        _BUNDESLIGA_STATS_TEXT_CACHE[metric_key] = (None, None)
        return None, None

    url = _bundesliga_stat_url(config["slug"])

    try:
        html = http_get_text(url, timeout=8)
        page_text = _html_to_visible_text(html)
        _BUNDESLIGA_STATS_TEXT_CACHE[metric_key] = (page_text, url)
        print(
            f"STATS-CACHE {metric_key}: geladen "
            f"({len(page_text)} Zeichen) | {url}"
        )
        return page_text, url
    except Exception as exc:
        print(f"STATS-CACHE FEHLER {metric_key}: {exc}")
        _BUNDESLIGA_STATS_TEXT_CACHE[metric_key] = (None, url)
        return None, url


def _normalize_player_lookup_name(value):
    value = normalize_name(value or "")
    return re.sub(r"\s+", " ", value).strip()


def _extract_metric_from_ranking_text(page_text, heading, player_name):
    """
    Extrahiert den Wert eines Spielers aus dem sichtbaren Rankingtext.

    Die Bundesliga-Seiten rendern serverseitig Einträge in der Form:
      Rang Spielername Wert

    Der Ausschnitt wird auf den Bereich der gewählten Kategorie begrenzt,
    um Seitenspalten mit anderen Rankings nicht versehentlich zu verwenden.
    """
    if not page_text or not player_name:
        return None

    # Hauptkategorie ab ihrer Überschrift bis zum nächsten typischen Ranking-
    # oder Navigationsmarker eingrenzen. Ein großzügiger Ausschnitt reicht,
    # weil die aktuelle Saison zu Beginn nur wenige Nicht-Null-Werte enthält.
    heading_match = re.search(
        re.escape(heading),
        page_text,
        flags=re.IGNORECASE,
    )
    if not heading_match:
        return None

    start = heading_match.start()
    segment = page_text[start:start + 7000]

    # Exakten Namen bevorzugen.
    name_patterns = [
        re.escape(str(player_name).strip()),
    ]

    # Unicode-/Akzent-robuster Fallback über Nachnamen.
    parts = str(player_name).strip().split()
    if parts:
        surname = parts[-1]
        if surname and surname != player_name:
            name_patterns.append(re.escape(surname))

    for name_pattern in name_patterns:
        for match in re.finditer(
            rf"\b{name_pattern}\b",
            segment,
            flags=re.IGNORECASE,
        ):
            tail = segment[match.end():match.end() + 60]

            # Wert direkt nach dem Namen. Dezimalzahlen und Komma zulassen.
            value_match = re.search(
                r"^\s*(\d+(?:[.,]\d+)?)\b",
                tail,
            )
            if value_match:
                raw = value_match.group(1).replace(",", ".")
                try:
                    value = float(raw)
                    return int(value) if value.is_integer() else value
                except ValueError:
                    continue

    return None


def collect_bundesliga_rankings_for_player(player):
    """
    V19: Alle ligaweiten Kategorien für EINEN Spieler aus bereits gecachten
    Statistikseiten auslesen.

    Zählstatistiken:
      Wenn die Seite erfolgreich geladen wurde und der Spieler nicht in der
      Nicht-Null-Rangliste erscheint, wird 0 verwendet.

    Quoten:
      Fehlender Eintrag bleibt None; eine Quote darf niemals als 0 erfunden
      werden.
    """
    player_name = str(player.get("name") or "").strip()

    values = {}
    sources = {}
    pages_available = 0

    for metric_key, config in BUNDESLIGA_PLAYER_STAT_CATEGORIES.items():
        page_text, url = _get_bundesliga_stat_text(metric_key)

        if not page_text:
            values[metric_key] = None
            continue

        pages_available += 1
        value = _extract_metric_from_ranking_text(
            page_text,
            config["heading"],
            player_name,
        )

        if value is None and config["kind"] == "count":
            # Bundesliga-Rankings führen nur Spieler mit >0 in der sichtbaren
            # Wertung. Ist die Seite erreichbar, ist "nicht gelistet" für eine
            # reine Zählstatistik ein belastbares 0.
            value = 0

        values[metric_key] = value
        if value is not None:
            sources[metric_key] = url

    # "cards" ist Gesamtzahl Karten, aber redCards lässt sich daraus nicht
    # sauber ableiten, weil Gelb-Rot separat enthalten sein kann. Deshalb
    # redCards bewusst NICHT schätzen.
    values["redCards"] = None

    print(
        f"STATS-PLAYER {player_name}: "
        f"{sum(v is not None for v in values.values())} Werte | "
        f"{pages_available} Rankingseiten verfügbar"
    )

    return values, sources



def build_kickbase_ai_projection(player, performance, data_coverage):
    """
    V21: Positionsabhängiger Public-Data-Prognoseindex.

    - keine Kickbase-API
    - fehlende Statistiken werden nicht als 0 gewertet
    - Verletzung/Sperre sind harte Ausschlussregeln
    - Startelfsignal + historische Starts/Minuten -> erwartete Spielzeit
    - positionsabhängige Gewichtung der vorhandenen Leistungsdaten
    """
    coverage = int(data_coverage.get("coveragePercent") or 0)

    starting = str(player.get("starting") or "").lower()
    injury = str(player.get("injury") or "").lower()
    suspension = str(player.get("suspension") or "").lower()
    home_away = str(player.get("homeAway") or "").lower()
    position = str(player.get("position") or "").lower()

    if "verletzt" in injury or "gesperrt" in suspension:
        return {
            "expectedPoints": None,
            "rangeMin": None,
            "rangeMax": None,
            "confidence": coverage,
            "recommendation": "Nicht aufstellen",
            "reason": "Verletzung oder Sperre",
            "expectedMinutes": 0,
            "positionModel": position or "unbekannt",
            "model": "v21-position-minutes-public-data",
        }

    appearances = performance.get("appearances")
    starts = performance.get("starts")
    minutes = performance.get("minutes")

    # 1) Startelfsignal -> Basis-Spielzeit
    if "sehr wahrscheinlich" in starting:
        expected_minutes = 82.0
        start_probability = 0.93
    elif "wahrscheinlich" in starting:
        expected_minutes = 74.0
        start_probability = 0.82
    elif "eher bank" in starting:
        expected_minutes = 34.0
        start_probability = 0.38
    elif "nicht" in starting and "recherchiert" not in starting:
        expected_minutes = 20.0
        start_probability = 0.20
    else:
        expected_minutes = 50.0
        start_probability = 0.55

    # Historische Einsatzdaten stabilisieren die Schätzung, falls vorhanden.
    hist_minutes = None
    if minutes is not None and appearances:
        try:
            hist_minutes = float(minutes) / max(float(appearances), 1.0)
            expected_minutes = 0.72 * expected_minutes + 0.28 * hist_minutes
        except (TypeError, ValueError):
            pass

    if starts is not None and appearances:
        try:
            historical_start_share = float(starts) / max(float(appearances), 1.0)
            start_probability = 0.78 * start_probability + 0.22 * historical_start_share
        except (TypeError, ValueError):
            pass

    expected_minutes = max(0.0, min(expected_minutes, 90.0))
    minute_factor = expected_minutes / 90.0

    def per90(value):
        if value is None:
            return None
        try:
            if minutes:
                return float(value) * 90.0 / max(float(minutes), 1.0)
            if appearances:
                return float(value) / max(float(appearances), 1.0)
        except (TypeError, ValueError):
            return None
        return None

    # 2) Positionsmodell
    if "torwart" in position or position in ("tw", "gk"):
        pos_group = "TW"
    elif "abwehr" in position or position in ("ab", "abw", "df"):
        pos_group = "ABW"
    elif "mittelfeld" in position or position in ("mf", "mid"):
        pos_group = "MF"
    elif "angriff" in position or position in ("ang", "fw", "st"):
        pos_group = "ANG"
    else:
        pos_group = "ALL"

    # Werte sind bewusst ein transparenter eigener Index und keine behauptete
    # Reproduktion der proprietären Kickbase-Punkteformel.
    weights = {
        "TW": {
            "goals": 14.0, "assists": 12.0, "shots": 0.5, "shotsOnTarget": 0.8,
            "duelsWon": 0.7, "aerialDuelsWon": 0.8, "crosses": 0.1,
            "saves": 5.0, "cleanSheets": 14.0, "goalsAgainst": -4.5,
            "fouls": -1.0, "yellowCards": -3.0,
        },
        "ABW": {
            "goals": 24.0, "assists": 14.0, "shots": 1.0, "shotsOnTarget": 1.8,
            "duelsWon": 1.0, "aerialDuelsWon": 1.2, "crosses": 0.45,
            "saves": 0.0, "cleanSheets": 9.0, "goalsAgainst": -2.2,
            "fouls": -0.8, "yellowCards": -3.0,
        },
        "MF": {
            "goals": 22.0, "assists": 18.0, "shots": 1.3, "shotsOnTarget": 2.0,
            "duelsWon": 0.75, "aerialDuelsWon": 0.65, "crosses": 0.65,
            "saves": 0.0, "cleanSheets": 2.0, "goalsAgainst": -0.5,
            "fouls": -0.7, "yellowCards": -3.0,
        },
        "ANG": {
            "goals": 26.0, "assists": 17.0, "shots": 1.5, "shotsOnTarget": 2.6,
            "duelsWon": 0.55, "aerialDuelsWon": 0.7, "crosses": 0.35,
            "saves": 0.0, "cleanSheets": 0.0, "goalsAgainst": 0.0,
            "fouls": -0.6, "yellowCards": -3.0,
        },
        "ALL": {
            "goals": 22.0, "assists": 16.0, "shots": 1.2, "shotsOnTarget": 2.0,
            "duelsWon": 0.7, "aerialDuelsWon": 0.7, "crosses": 0.45,
            "saves": 2.0, "cleanSheets": 4.0, "goalsAgainst": -1.0,
            "fouls": -0.7, "yellowCards": -3.0,
        },
    }[pos_group]

    # 3) Baseline aus Einsatzwahrscheinlichkeit. Dadurch wird ein sicherer
    # Starter klar von einem Bankspieler getrennt.
    score = 8.0 + 28.0 * start_probability + 10.0 * minute_factor

    component_values = {}
    for key, weight in weights.items():
        value = per90(performance.get(key))
        if value is None or weight == 0:
            continue
        contribution = value * weight * minute_factor
        # Einzelne öffentliche Rankings dürfen die Prognose nicht dominieren.
        contribution = max(-12.0, min(contribution, 22.0))
        component_values[key] = contribution

    # Passquote nur als kleiner Effizienzindikator; ohne Passvolumen/-zone
    # darf sie keinen großen Einfluss bekommen.
    pass_accuracy = performance.get("passAccuracy")
    if pass_accuracy is not None:
        try:
            pa = float(pass_accuracy)
            component_values["passAccuracy"] = max(
                -3.0, min((pa - 75.0) * 0.10 * minute_factor, 3.0)
            )
        except (TypeError, ValueError):
            pass

    if component_values:
        # Summe mit Dämpfung statt Mittelwert: starke Spieler profitieren von
        # mehreren positiven Kategorien, ohne dass fehlende Kategorien = 0 sind.
        raw_components = sum(component_values.values())
        score += raw_components * 0.72

    if "heim" in home_away:
        score += 2.0
    elif "auswärts" in home_away:
        score -= 1.0

    # Coverage beeinflusst primär die Sicherheit, nicht die Spielerqualität.
    # Nur bei sehr dünner Datenlage ziehen wir Extremwerte leicht zur Mitte.
    coverage_factor = max(0.55, min(coverage / 70.0, 1.0))
    score = 25.0 + (score - 25.0) * coverage_factor

    expected = int(round(max(0.0, min(score, 120.0))))

    uncertainty = int(round(
        24.0
        - min(coverage, 100) * 0.12
        + (1.0 - start_probability) * 8.0
    ))
    uncertainty = max(8, min(28, uncertainty))

    if expected_minutes < 25:
        recommendation = "Riskant"
    elif expected_minutes < 50:
        recommendation = "Eher Bank / Joker"
    elif expected >= 45 and start_probability >= 0.78:
        recommendation = "Sehr gute Option"
    elif start_probability >= 0.75:
        recommendation = "Starten"
    elif expected_minutes >= 55:
        recommendation = "Beobachten"
    else:
        recommendation = "Riskant"

    reason = (
        f"{pos_group}-Modell | erwartete Spielzeit {int(round(expected_minutes))} Min. | "
        f"{len(component_values)} Performance-Komponenten | Datenabdeckung {coverage}%"
    )

    return {
        "expectedPoints": expected,
        "rangeMin": max(0, expected - uncertainty),
        "rangeMax": expected + uncertainty,
        "confidence": coverage,
        "recommendation": recommendation,
        "reason": reason,
        "expectedMinutes": int(round(expected_minutes)),
        "startProbability": int(round(start_probability * 100)),
        "positionModel": pos_group,
        "componentsUsed": sorted(component_values.keys()),
        "model": "v21-position-minutes-public-data",
    }


def build_performance_layer(values, old_performance=None):
    """
    V18: Transparente Statistikschicht für den späteren Kickbase-AI Score.
    Es werden ausschließlich tatsächlich gefundene Werte gespeichert.
    Kein Wert wird aus anderen Statistiken geschätzt.
    """
    old_performance = old_performance or {}

    metric_names = (
        "appearances",
        "starts",
        "minutes",
        "goals",
        "assists",
        "shots",
        "shotsOnTarget",
        "woodwork",
        "penalties",
        "penaltiesScored",
        "passAccuracy",
        "duelsWon",
        "aerialDuelsWon",
        "crosses",
        "fouls",
        "yellowCards",
        "redCards",
        "saves",
        "cleanSheets",
        "goalsAgainst",
    )

    performance = {}
    for key in metric_names:
        new_value = values.get(key)
        performance[key] = (
            new_value
            if new_value is not None
            else old_performance.get(key)
        )

    available = [key for key in metric_names if performance.get(key) is not None]
    missing = [key for key in metric_names if performance.get(key) is None]
    coverage = round((len(available) / len(metric_names)) * 100) if metric_names else 0

    return performance, {
        "availableMetrics": available,
        "missingMetrics": missing,
        "coveragePercent": coverage,
        "scoreReady": coverage >= 70,
        "note": (
            "Nur öffentlich gefundene Werte; keine Kickbase-Daten und keine "
            "geschätzten Einzelaktionen."
        ),
    }


def _is_goalkeeper(player):
    position = normalize_name(player.get("position"))
    return position in {"torhuter", "torhueter", "tw", "goalkeeper"}


def _localized_profile_urls(source_url):
    """Alternative offizielle Bundesliga.com-Sprachpfade für dasselbe Profil."""
    replacements = (
        ("/de/spieler/", "/pt/jogador/"),
        ("/de/spieler/", "/en/player/"),
        ("/de/spieler/", "/es/jugador/"),
        ("/de/spieler/", "/fr/joueur/"),
    )
    urls = []
    for old, new in replacements:
        if old in source_url:
            candidate = source_url.replace(old, new, 1)
            if candidate not in urls:
                urls.append(candidate)
    return urls




def _official_host_matches(url, domain):
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False

    domain = (domain or "").lower()
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def _extract_xml_locs(xml_text):
    if not xml_text:
        return []

    return [
        match.strip()
        for match in re.findall(
            r"<loc>\s*(.*?)\s*</loc>",
            xml_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match.strip()
    ]


def _candidate_sitemap_urls(domain):
    base = f"https://{domain}"
    return [
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
        f"{base}/sitemap-index.xml",
        f"{base}/news-sitemap.xml",
        f"{base}/sitemap-news.xml",
    ]


def _url_looks_status_relevant(url):
    lower = (url or "").lower()
    year = str(datetime.now(timezone.utc).year)

    keyword_hit = any(keyword in lower for keyword in STATUS_URL_KEYWORDS)
    current_year = year in lower
    previous_year = str(int(year) - 1) in lower

    return keyword_hit and (current_year or previous_year or "/news/" in lower)


def _decode_duckduckgo_url(href):
    if not href:
        return None

    href = href.replace("&amp;", "&")
    if href.startswith("//"):
        href = "https:" + href

    if "duckduckgo.com/l/" in href or href.startswith("/l/"):
        try:
            parsed = urlparse(
                href if href.startswith("http")
                else "https://duckduckgo.com" + href
            )
            uddg = parse_qs(parsed.query).get("uddg", [None])[0]
            return unquote(uddg) if uddg else None
        except Exception:
            return None

    return href if href.startswith("http") else None


def _discover_urls_via_sitemaps(domain, max_urls=35):
    """
    Holt Status-/News-URLs ausschließlich von der offiziellen Vereinsdomain.
    Sitemap-Indizes werden maximal eine Ebene rekursiv verfolgt.
    """
    result = []
    seen = set()
    sitemap_queue = _candidate_sitemap_urls(domain)
    processed_sitemaps = set()

    while sitemap_queue and len(result) < max_urls:
        sitemap_url = sitemap_queue.pop(0)

        if sitemap_url in processed_sitemaps:
            continue

        processed_sitemaps.add(sitemap_url)

        try:
            body = http_get_text(sitemap_url, timeout=12)
        except Exception:
            continue

        locs = _extract_xml_locs(body)

        for loc in locs:
            if not _official_host_matches(loc, domain):
                continue

            lower = loc.lower()

            if (
                "sitemap" in lower
                and loc not in processed_sitemaps
                and len(processed_sitemaps) < 12
            ):
                sitemap_queue.append(loc)
                continue

            if (
                loc not in seen
                and _url_looks_status_relevant(loc)
            ):
                seen.add(loc)
                result.append(loc)

                if len(result) >= max_urls:
                    break

    return result


def _discover_urls_via_search(player_name, domain, max_urls=8):
    """
    Fallback nur zur Link-Findung. Inhalt wird anschließend ausschließlich
    von der offiziellen Vereinsdomain geladen.
    """
    year = datetime.now(timezone.utc).year
    queries = (
        f'site:{domain} "{player_name}" Verletzung {year}',
        f'site:{domain} "{player_name}" Reha {year}',
        f'site:{domain} "{player_name}" Personal {year}',
        f'site:{domain} "{player_name}" gesperrt {year}',
    )

    found = []
    seen = set()

    for query in queries:
        try:
            url = SEARCH_URL + "?" + urlencode({"q": query})
            html = http_get_text(
                url,
                headers={"Accept-Language": "de-DE,de;q=0.9"},
                timeout=15,
            )
        except Exception:
            continue

        hrefs = re.findall(
            r'<a[^>]+href=["\']([^"\']+)["\']',
            html,
            flags=re.IGNORECASE,
        )

        for href in hrefs:
            target = _decode_duckduckgo_url(href)

            if (
                not target
                or target in seen
                or not _official_host_matches(target, domain)
            ):
                continue

            seen.add(target)
            found.append(target)

            if len(found) >= max_urls:
                return found

    return found


def _search_query_urls(query, allowed_domains=None, max_urls=12):
    """
    Sucht gezielt nach einem Spieler. Die Suchmaschine dient ausschließlich
    zur URL-Ermittlung; die Inhalte werden danach von den Zielseiten geladen.
    """
    found = []
    seen = set()

    try:
        url = SEARCH_URL + "?" + urlencode({"q": query})
        html = http_get_text(
            url,
            headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.7"},
            timeout=STATUS_SEARCH_TIMEOUT,
        )
    except Exception as exc:
        print(f"SEARCH-DEBUG: Suche fehlgeschlagen: {query} -> {exc}")
        return []

    hrefs = re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )

    for href in hrefs:
        target = _decode_duckduckgo_url(href)
        if not target or target in seen:
            continue

        try:
            host = (urlparse(target).hostname or "").lower()
        except Exception:
            continue

        if allowed_domains:
            ok = any(
                host == domain or host.endswith("." + domain)
                for domain in allowed_domains
            )
            if not ok:
                continue

        seen.add(target)
        found.append(target)

        if len(found) >= max_urls:
            break

    return found



def _matchday_from_next_match(next_match):
    if not isinstance(next_match, dict):
        return None

    match = next_match.get("match") or {}
    group = match.get("group") or {}

    candidates = (
        group.get("groupOrderID"),
        group.get("groupOrderId"),
        match.get("groupOrderID"),
        match.get("groupOrderId"),
    )

    for value in candidates:
        try:
            number = int(value)
            if 1 <= number <= 34:
                return number
        except (TypeError, ValueError):
            continue

    return None


def _get_matchday_status_text(matchday):
    """
    Lädt die offizielle Bundesliga-Spieltagübersicht genau EINMAL pro Lauf.
    Die Seite enthält voraussichtliche Aufstellungen sowie 'Es fehlen'
    mit Verletzungen/Sperren für alle Vereine.
    """
    if not matchday:
        return None, None

    if matchday in _MATCHDAY_STATUS_CACHE:
        return _MATCHDAY_STATUS_CACHE[matchday]

    url = BUNDESLIGA_MATCHDAY_STATUS_TEMPLATE.format(matchday=matchday)

    try:
        html = http_get_text(url, timeout=10)
        page_text = _html_to_visible_text(html)
        result = (page_text, url)
        print(
            f"MATCHDAY-STATUS: Spieltag {matchday} geladen "
            f"({len(page_text)} Zeichen)."
        )
    except Exception as exc:
        print(
            f"MATCHDAY-STATUS FEHLER: Spieltag {matchday} "
            f"konnte nicht geladen werden: {exc}"
        )
        result = (None, url)

    _MATCHDAY_STATUS_CACHE[matchday] = result
    return result


def _extract_team_match_section(page_text, club, opponent):
    """
    Schneidet möglichst nur die Partie des gesuchten Vereins aus der
    Ligaübersicht heraus.
    """
    if not page_text:
        return ""

    patterns = []
    if club and opponent:
        patterns.extend([
            rf"{re.escape(club)}\s*-\s*{re.escape(opponent)}",
            rf"{re.escape(opponent)}\s*-\s*{re.escape(club)}",
        ])

    start = None
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE)
        if match:
            start = match.start()
            break

    if start is None:
        # Fallback: erster Clubtreffer
        match = re.search(re.escape(club or ""), page_text, flags=re.IGNORECASE)
        start = match.start() if match else 0

    # 3500 Zeichen reichen typischerweise für beide Teams einer Partie,
    # ohne große Teile des nächsten Spiels mitzunehmen.
    return page_text[start:start + 3500]


def _classify_absence_reason(reason):
    lower = (reason or "").lower()

    suspension_terms = (
        "sperre", "gesperrt", "rotsperre", "gelbsperre",
        "gelb-rot", "rote karte", "gelbe karte",
    )
    non_injury_terms = (
        "nicht berücksichtigt",
        "nicht beruecksichtigt",
        "belastungssteuerung",
        "rotation",
    )

    if any(term in lower for term in suspension_terms):
        return "suspension"

    if any(term in lower for term in non_injury_terms):
        return "other"

    return "injury"



def _name_matches_in_lineup(lineup_text, player_name):
    if not lineup_text or not player_name:
        return False
    parts = str(player_name).split()
    surname = parts[-1] if parts else player_name
    patterns = [re.escape(player_name)]
    if surname and surname != player_name:
        patterns.append(re.escape(surname))
    return any(
        re.search(rf"\b{pattern}\b", lineup_text, flags=re.IGNORECASE)
        for pattern in patterns
    )


def _extract_team_lineup(section, club):
    """
    Extrahiert die voraussichtliche Elf aus geglättetem Seitentext.
    Funktioniert ohne Zeilenumbrüche.
    """
    if not section or not club:
        return ""

    aliases = {
        "FC Bayern München": ("FCB", "Bayern", "FC Bayern München"),
        "VfB Stuttgart": ("VFB", "VfB Stuttgart", "Stuttgart"),
        "RB Leipzig": ("RBL", "RB Leipzig", "Leipzig"),
        "Borussia Mönchengladbach": ("BMG", "Borussia Mönchengladbach", "Gladbach"),
        "1. FSV Mainz 05": ("M05", "1. FSV Mainz 05", "Mainz"),
        "SC Paderborn 07": ("SCP", "SC Paderborn 07", "Paderborn"),
        "1. FC Union Berlin": ("FCU", "1. FC Union Berlin", "Union Berlin"),
        "Eintracht Frankfurt": ("SGE", "Eintracht Frankfurt", "Frankfurt"),
        "1. FC Köln": ("KOE", "1. FC Köln", "Köln"),
        "TSG Hoffenheim": ("TSG", "TSG Hoffenheim", "Hoffenheim"),
        "SV 07 Elversberg": ("SVE", "SV 07 Elversberg", "Elversberg"),
        "Bayer 04 Leverkusen": ("B04", "Bayer 04 Leverkusen", "Leverkusen"),
        "Borussia Dortmund": ("BVB", "Borussia Dortmund", "Dortmund"),
        "Hamburger SV": ("HSV", "Hamburger SV"),
        "SC Freiburg": ("SCF", "SC Freiburg", "Freiburg"),
        "SV Werder Bremen": ("SVW", "SV Werder Bremen", "Werder Bremen"),
        "FC Augsburg": ("FCA", "FC Augsburg", "Augsburg"),
        "FC Schalke 04": ("S04", "FC Schalke 04", "Schalke"),
    }

    labels = aliases.get(club, (club,))
    start_candidates = []

    for label in labels:
        for pat in (
            rf"Voraussichtliche\s+Aufstellung(?:en)?\s+{re.escape(label)}\s*[:\-]?\s*",
            rf"Predicted\s+line-?up\s*[:\-]?\s*{re.escape(label)}\s*",
            rf"\b{re.escape(label)}\s*:\s*",
        ):
            match = re.search(pat, section, flags=re.IGNORECASE)
            if match:
                start_candidates.append(match.end())

    if not start_candidates:
        return ""

    start = min(start_candidates)
    tail = section[start:]

    end_patterns = [
        r"\bEs\s+fehlen\s*:",
        r"\bVoraussichtliche\s+Aufstellung(?:en)?\b",
        r"\bPredicted\s+line-?up\b",
    ]

    end = len(tail)
    for end_pattern in end_patterns:
        match = re.search(end_pattern, tail, flags=re.IGNORECASE)
        if match and match.start() > 10:
            end = min(end, match.start())

    return re.sub(r"\s+", " ", tail[:end]).strip()



def _slugify_for_search(value):
    value = str(value or "").lower()
    value = (
        value.replace("ä", "a")
        .replace("ö", "o")
        .replace("ü", "u")
        .replace("ß", "ss")
    )
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def _discover_bundesliga_teamcheck_url(club):
    """
    V17: Findet den Teamcheck ausschließlich über die offizielle
    Bundesliga-Club-Newsseite. Keine Google-/Suchmaschinen-Abhängigkeit.
    """
    if not club:
        return None

    cached = _TEAMCHECK_CACHE.get(club)
    if cached and "url" in cached:
        return cached.get("url")

    slug = BUNDESLIGA_CLUB_NEWS_SLUGS.get(club)
    if not slug:
        _TEAMCHECK_CACHE[club] = {"url": None, "text": None}
        print(f"TEAMCHECK-DISCOVERY {club}: kein Bundesliga-Club-Slug hinterlegt.")
        return None

    news_url = f"https://www.bundesliga.com/de/bundesliga/clubs/{slug}/news"

    try:
        html = http_get_text(news_url, timeout=10)
    except Exception as exc:
        _TEAMCHECK_CACHE[club] = {"url": None, "text": None}
        print(f"TEAMCHECK-DISCOVERY {club}: Newsseite nicht ladbar -> {exc}")
        return None

    # Direkte absolute und relative Teamcheck-Links akzeptieren.
    hrefs = re.findall(
        r'href=["\']([^"\']*teamcheck-saisonvorschau-2026-27[^"\']*)["\']',
        html,
        flags=re.IGNORECASE,
    )

    candidates = []
    for href in hrefs:
        href = href.replace("&amp;", "&").strip()

        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.bundesliga.com" + href
        elif not href.startswith("http"):
            continue

        # Nur Bundesliga-Domain und aktuelle Saison.
        if (
            "bundesliga.com/" not in href.lower()
            or "teamcheck-saisonvorschau-2026-27" not in href.lower()
        ):
            continue

        href = href.split("?")[0].split("#")[0]
        if href not in candidates:
            candidates.append(href)

    # Falls mehrere Teamchecks auf der Newsseite auftauchen:
    # Clubname/Slug im Link bevorzugen.
    club_tokens = [
        token
        for token in re.sub(r"[^a-z0-9]+", "-", slug.lower()).split("-")
        if len(token) >= 3 and token not in {"club"}
    ]

    best_url = None
    best_score = -1

    for url in candidates:
        lower = url.lower()
        score = sum(token in lower for token in club_tokens)
        if score > best_score:
            best_score = score
            best_url = url

    _TEAMCHECK_CACHE[club] = {
        "url": best_url,
        "text": None,
        "newsUrl": news_url,
    }

    print(
        f"TEAMCHECK-DISCOVERY {club}: "
        f"{len(candidates)} Kandidat(en) -> {best_url or 'kein Treffer'}"
    )

    return best_url


def _get_teamcheck_text(club):
    cached = _TEAMCHECK_CACHE.get(club)
    if cached and cached.get("text"):
        return cached["text"], cached.get("url")

    url = _discover_bundesliga_teamcheck_url(club)
    if not url:
        return None, None

    try:
        html = http_get_text(url, timeout=10)
        page_text = _html_to_visible_text(html)
    except Exception as exc:
        print(f"TEAMCHECK-FEHLER {club}: {exc}")
        return None, url

    _TEAMCHECK_CACHE[club] = {"url": url, "text": page_text}
    print(f"TEAMCHECK {club}: geladen ({len(page_text)} Zeichen).")
    return page_text, url


def _player_in_teamcheck_starting_xi(player_name, page_text):
    if not player_name or not page_text:
        return False

    marker = re.search(
        r"Voraussichtliche\s+Aufstellung\s+am\s+\d+\.\s*Spieltag\s*:",
        page_text,
        flags=re.IGNORECASE,
    )
    if not marker:
        return False

    tail = page_text[marker.end():marker.end() + 1800]

    # Stoppe an einem typischen Artikel-/Autorenmarker, sofern vorhanden.
    stop = re.search(
        r"\b(?:Weitere Videos|Empfohlener redaktioneller Inhalt|Anzeige)\b",
        tail,
        flags=re.IGNORECASE,
    )
    if stop:
        tail = tail[:stop.start()]

    return _name_matches_in_lineup(tail, player_name)


def _derive_lineup_probability(player, section):
    player_name = str(player.get("name") or "").strip()
    club = str(player.get("club") or "").strip()
    lineup = _extract_team_lineup(section, club)

    # Matchday-Aufstellung eindeutig erkannt.
    if _name_matches_in_lineup(lineup, player_name):
        return "Sehr wahrscheinlich"

    # Explizite Ausfallliste hat Vorrang.
    parts = player_name.split()
    surname = parts[-1] if parts else player_name
    if re.search(
        rf"\b{re.escape(surname)}\b\s*\(",
        section or "",
        flags=re.IGNORECASE,
    ):
        return "Nein"

    # Spieler erscheint im Aufstellungsbereich, aber Parser kann die
    # exakte Teamzeile nicht sicher isolieren.
    first_missing = re.search(
        r"\bEs\s+fehlen\s*:",
        section or "",
        flags=re.IGNORECASE,
    )
    pre_missing = (
        (section or "")[:first_missing.start()]
        if first_missing
        else (section or "")
    )

    if _name_matches_in_lineup(pre_missing, player_name):
        return "Wahrscheinlich"

    # V17: offizieller Bundesliga-Teamcheck über Club-Newsseite.
    teamcheck_text, teamcheck_url = _get_teamcheck_text(club)

    if teamcheck_text:
        if _player_in_teamcheck_starting_xi(player_name, teamcheck_text):
            print(
                f"TEAMCHECK-STARTELF {player_name}: JA | {teamcheck_url}"
            )
            return "Wahrscheinlich"

        print(
            f"TEAMCHECK-STARTELF {player_name}: NEIN | {teamcheck_url}"
        )
        return "Eher Bank / offen"

    # Keine belastbare Aufstellungsquelle -> keine negative Behauptung.
    print(
        f"TEAMCHECK-STARTELF {player_name}: "
        "keine belastbare Teamcheck-Quelle gefunden."
    )
    return "Noch nicht recherchiert"


def _derive_form_from_stats(player):
    """
    Form bleibt bewusst konservativ und nutzt nur bereits vorhandene
    strukturierte Match-/Spielerwerte. Keine erfundenen Kickbase-Punkte.
    """
    appearances = player.get("appearances")
    starts = player.get("starts")
    goals = player.get("goals")
    assists = player.get("assists")

    nums = []
    for value in (appearances, starts, goals, assists):
        try:
            nums.append(int(value) if value is not None else None)
        except (TypeError, ValueError):
            nums.append(None)

    appearances, starts, goals, assists = nums

    if appearances is None:
        return "Noch keine belastbare Formbasis"

    if appearances == 0:
        return "Noch kein Saisoneinsatz"

    parts = [f"{appearances} Einsätze"]
    if starts is not None:
        parts.append(f"{starts} Startelf")
    if goals is not None:
        parts.append(f"{goals} Tore")
    if assists is not None:
        parts.append(f"{assists} Vorlagen")
    return " · ".join(parts)


def _normalize_yellow_cards(player):
    for key in ("yellowCards", "yellow_cards", "yellowcards"):
        value = player.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return 0

def get_matchday_status_for_player(player):
    """
    V14: Status aus einer einzigen offiziellen Bundesliga-Seite.

    Wenn ein Spieler unter 'Es fehlen' mit Grund aufgeführt wird:
      - Sperrgrund -> Gesperrt
      - sonst      -> Verletzt + Grund

    Wenn die aktuelle Spieltagseite erfolgreich geladen wurde und der
    Spieler NICHT unter 'Es fehlen' steht:
      - Verletzung -> Fit
      - Sperre     -> Keine Sperre
    """
    player_name = str(player.get("name") or "").strip()
    club = str(player.get("club") or "").strip()
    opponent = str(player.get("_opponent") or "").strip()
    matchday = player.get("_matchday")

    page_text, source_url = _get_matchday_status_text(matchday)

    if not page_text:
        return {
            "checked": False,
            "evidence": "unknown",
            "injured": False,
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "suspended": False,
            "sourceUrl": None,
        }

    section = _extract_team_match_section(
        page_text,
        club,
        opponent,
    )

    lineup_probability = _derive_lineup_probability(player, section)

    parts = player_name.split()
    surname = parts[-1] if parts else player_name

    # Bundesliga führt in der Aufstellung meist nur den Nachnamen.
    name_patterns = []
    if player_name:
        name_patterns.append(re.escape(player_name))
    if surname and surname != player_name:
        name_patterns.append(re.escape(surname))

    reason = None

    for name_pattern in name_patterns:
        # Abwesenheitslisten haben zuverlässig die Form:
        # Spielername (Grund)
        matches = list(re.finditer(
            rf"\b{name_pattern}\b\s*\(([^)]{{2,180}})\)",
            section,
            flags=re.IGNORECASE,
        ))
        if matches:
            reason = re.sub(
                r"\s+",
                " ",
                matches[0].group(1),
            ).strip()
            break

    if reason:
        category = _classify_absence_reason(reason)

        if category == "suspension":
            print(
                f"MATCHDAY-STATUS {player_name}: "
                f"Gesperrt | Grund={reason}"
            )
            return {
                "checked": True,
                "evidence": "suspended",
                "injured": False,
                "injuryDiagnosis": None,
                "injuryExpectedAbsence": None,
                "suspended": True,
                "sourceUrl": source_url,
                "lineupProbability": lineup_probability,
            }

        if category == "injury":
            print(
                f"MATCHDAY-STATUS {player_name}: "
                f"Verletzt | Grund={reason}"
            )
            return {
                "checked": True,
                "evidence": "injured",
                "injured": True,
                "injuryDiagnosis": reason,
                "injuryExpectedAbsence": None,
                "suspended": False,
                "sourceUrl": source_url,
                "lineupProbability": lineup_probability,
            }

        # Nicht berücksichtigt / Rotation etc. ist keine Verletzung.
        print(
            f"MATCHDAY-STATUS {player_name}: "
            f"kein Verletzungs-/Sperrgrund | Hinweis={reason}"
        )
        return {
            "checked": True,
            "evidence": "fit",
            "injured": False,
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "suspended": False,
            "sourceUrl": source_url,
            "lineupProbability": lineup_probability,
        }

    # Seite erfolgreich geprüft und Spieler steht nicht in der Ausfallliste.
    print(
        f"MATCHDAY-STATUS {player_name}: "
        "nicht unter 'Es fehlen' -> Fit / Keine Sperre"
    )
    return {
        "checked": True,
        "evidence": "fit",
        "injured": False,
        "injuryDiagnosis": None,
        "injuryExpectedAbsence": None,
        "suspended": False,
        "sourceUrl": source_url,
        "lineupProbability": lineup_probability,
    }


def discover_official_status_urls(player):
    """
    V13 FAST:
    - keine Sitemap-Crawls
    - maximal EINE gezielte Suche pro Spieler
    - höchstens wenige offizielle Kandidaten
    - bekannte offizielle Club-Seeds bleiben kostenlos nutzbar

    Dadurch vermeiden wir die 19-Minuten-Laufzeit aus V12.
    """
    club = str(player.get("club") or "").strip()
    player_name = str(player.get("name") or "").strip()
    domain = OFFICIAL_CLUB_DOMAINS.get(club)

    if not domain or not player_name:
        return []

    urls = []
    seen = set()

    def add(url):
        if (
            url
            and url not in seen
            and _official_host_matches(url, domain)
        ):
            seen.add(url)
            urls.append(url)

    # Kostenlose bekannte Seeds zuerst
    for url in OFFICIAL_STATUS_SEEDS.get(club, []):
        add(url)

    # Nur EINE kombinierte Suchanfrage
    query = (
        f'site:{domain} "{player_name}" '
        f'(Verletzung OR verletzt OR Reha OR Training OR gesperrt OR Personal)'
    )

    for url in _search_query_urls(
        query,
        allowed_domains=[domain],
        max_urls=STATUS_MAX_CANDIDATE_URLS,
    ):
        add(url)

    print(
        f"STATUS-DISCOVERY {player_name}: "
        f"{len(urls)} offizielle Kandidaten für {club}."
    )

    return urls[:STATUS_MAX_CANDIDATE_URLS]


def _html_to_visible_text(html):
    if not html:
        return ""

    value = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(r"<[^>]+>", " ", value)
    value = (
        value.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"\s+", " ", value).strip()


def _player_text_windows(text, player_name, radius=320):
    """
    Liefert nur Textbereiche direkt um den Spielernamen.
    Dadurch wird vermieden, dass die Verletzung/Sperre eines
    Teamkollegen dem falschen Spieler zugeordnet wird.
    """
    if not text or not player_name:
        return []

    name_pattern = re.escape(player_name)
    windows = []

    for match in re.finditer(name_pattern, text, flags=re.IGNORECASE):
        start = max(0, match.start() - radius)
        end = min(len(text), match.end() + radius)
        windows.append(text[start:end])

    # Fallback auf Nachnamen, wenn der vollständige Name in der Meldung
    # anders geschrieben ist.
    if not windows:
        parts = str(player_name).strip().split()
        if parts:
            surname = parts[-1]
            for match in re.finditer(
                re.escape(surname),
                text,
                flags=re.IGNORECASE,
            ):
                start = max(0, match.start() - radius)
                end = min(len(text), match.end() + radius)
                windows.append(text[start:end])

    return windows


def _diagnosis_from_text(text):
    lower = (text or "").lower()

    # Konkreten Kreuzbandbefund inklusive Knie-Seite bevorzugen.
    if (
        "riss des vorderen kreuzbandes" in lower
        or "kreuzbandriss" in lower
    ):
        side = None
        if "linken knie" in lower or "linkes knie" in lower:
            side = "linkes Knie"
        elif "rechten knie" in lower or "rechtes knie" in lower:
            side = "rechtes Knie"

        return (
            f"Kreuzbandriss ({side})"
            if side
            else "Kreuzbandriss"
        )

    for token, label in INJURY_DIAGNOSES:
        if token in lower:
            return label

    return None


def _absence_from_text(text):
    lower = (text or "").lower()

    if "weiterhin in der reha" in lower or "noch in der reha" in lower:
        return "weiterhin Reha; Rückkehr nicht öffentlich terminiert"

    if "in der reha" in lower or "in reha" in lower:
        return "Reha; Rückkehr nicht öffentlich terminiert"

    patterns = (
        r"\bvoraussichtlich\s+für\s+([^.;]{2,80})",
        r"\bvoraussichtlich\s+bis\s+([^.;]{2,80})",
        r"\bausfallzeit\s*[:\-]\s*([^.;]{2,80})",
        r"\bfällt\s+für\s+([^.;]{2,80})\s+aus",
        r"\bfehlt\s+für\s+([^.;]{2,80})",
    )

    for pattern in patterns:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;-")
            if value:
                return value[:80]

    return None


def _format_injury(injured, diagnosis=None, absence=None):
    if not injured:
        return "Fit"

    parts = ["Verletzt"]

    if diagnosis:
        parts.append(diagnosis)
    else:
        parts.append("Diagnose nicht öffentlich verfügbar")

    if absence:
        parts.append(absence)
    else:
        parts.append("Ausfallzeit nicht öffentlich verfügbar")

    return " · ".join(parts)



def _normalize_injury_label(value):
    """Entfernt alte UI-Texte aus früheren Script-Versionen."""
    if value is None:
        return value

    raw = str(value).strip()
    if raw.lower() in LEGACY_NO_INJURY_TEXTS:
        return "Status nicht eindeutig verfügbar"

    return raw


def _normalize_suspension_label(value):
    """Sperre wird nur noch als Gesperrt / Keine Sperre / unbekannt gespeichert."""
    if value is None:
        return value

    raw = str(value).strip()
    lower = raw.lower()

    if lower in LEGACY_NO_SUSPENSION_TEXTS:
        return "Keine Sperre"

    if lower in {"gesperrt", "keine sperre", "noch nicht recherchiert"}:
        return raw

    if "gesperrt" in lower or "sperre" in lower:
        return "Gesperrt"

    return raw


def _window_has_recovery_for_player(window):
    """
    Recovery zählt nur, wenn die Rückkehr-/Fit-Formulierung im direkten
    Spielerfenster steht. So neutralisiert die Rückkehr eines Teamkollegen
    nicht die Verletzung des gesuchten Spielers.
    """
    lower = (window or "").lower()
    return any(term in lower for term in RECOVERY_TERMS)


def get_official_status_for_player(player):
    """
    Generische Statusrecherche für jeden aktiven Kaderspieler.

    Ergebnis-Evidenz:
      injured   -> aktuelle offizielle Verletzungsmeldung
      recovered -> explizite Rückkehr-/Fit-Meldung
      suspended -> aktuelle Sperrmeldung
      unknown   -> keine belastbare Statusaussage gefunden

    Keine spielerspezifischen Sonderfälle.
    """
    global _STATUS_RESEARCH_STARTED_AT

    if _STATUS_RESEARCH_STARTED_AT is None:
        _STATUS_RESEARCH_STARTED_AT = time.monotonic()

    club = str(player.get("club") or "").strip()
    player_name = str(player.get("name") or "").strip()

    elapsed = time.monotonic() - _STATUS_RESEARCH_STARTED_AT
    if elapsed >= STATUS_GLOBAL_BUDGET_SECONDS:
        print(
            f"STATUS {player_name}: Fast-Mode-Zeitbudget erreicht; "
            "vorhandene Daten werden beibehalten."
        )
        return {
            "checked": False,
            "evidence": "budget_exhausted",
            "injured": False,
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "suspended": False,
            "sourceUrl": None,
        }

    urls = discover_official_status_urls(player)

    result = {
        "checked": False,
        "evidence": "unknown",
        "injured": False,
        "injuryDiagnosis": None,
        "injuryExpectedAbsence": None,
        "suspended": False,
        "sourceUrl": None,
    }

    if not urls:
        print(
            f"STATUS {player_name}: keine offiziellen Status-URLs "
            f"für {club} gefunden."
        )
        return result

    for debug_url in urls[:5]:
        print(f"STATUS-URL {player_name}: {debug_url}")

    # Nur eine begrenzte Zahl Seiten laden, damit 17/18 Spieler
    # den Workflow nicht unnötig verlangsamen.
    pages_checked = 0

    for url in urls:
        if pages_checked >= STATUS_MAX_PAGES_PER_PLAYER:
            break

        try:
            html = http_get_text(url, timeout=STATUS_PAGE_TIMEOUT)
            page_text = _html_to_visible_text(html)
            pages_checked += 1
            result["checked"] = True
        except Exception:
            continue

        windows = _player_text_windows(
            page_text,
            player_name,
            radius=420,
        )

        if not windows:
            continue

        for window in windows:
            lower = window.lower()

            diagnosis = _diagnosis_from_text(window)
            absence = _absence_from_text(window)

            injured = (
                diagnosis is not None
                or any(term in lower for term in CURRENT_INJURY_TERMS)
            )

            suspended = any(
                term in lower
                for term in CURRENT_SUSPENSION_TERMS
            )

            recovered = _window_has_recovery_for_player(window)

            # Verletzung ist die stärkste Evidenz, wenn Diagnose/
            # Ausfallbegriff unmittelbar beim Spielernamen steht.
            if injured and not recovered:
                result.update({
                    "evidence": "injured",
                    "injured": True,
                    "injuryDiagnosis": diagnosis,
                    "injuryExpectedAbsence": absence,
                    "sourceUrl": url,
                })

                print(
                    f"STATUS {player_name}: verletzt=True | "
                    f"Diagnose={diagnosis or '-'} | "
                    f"Ausfall={absence or '-'} | "
                    f"Quelle={url}"
                )
                return result

            if suspended:
                result.update({
                    "evidence": "suspended",
                    "suspended": True,
                    "sourceUrl": url,
                })

                print(
                    f"STATUS {player_name}: gesperrt=True | Quelle={url}"
                )
                return result

            if recovered:
                result.update({
                    "evidence": "recovered",
                    "sourceUrl": url,
                })

                print(
                    f"STATUS {player_name}: Rückkehr/Fit bestätigt | "
                    f"Quelle={url}"
                )
                return result

    print(
        f"STATUS {player_name}: keine eindeutige Statusmeldung "
        f"({pages_checked} offizielle Seiten geprüft)."
    )

    return result


def extract_player_profile_intelligence(player):
    """
    Recherchiert einen einzelnen Spieler direkt über sein
    öffentliches Bundesliga.com-Spielerprofil.

    Keine externe Fußball-API.
    Die Funktion liefert nur Werte, die auf dem öffentlichen
    Profil tatsächlich gefunden wurden.
    """
    source_url = player.get("sourceUrl")

    if not source_url:
        return {
            "available": False,
            "sourceUrl": None,
            "appearances": None,
            "goals": None,
            "assists": None,
            "goalsAgainst": None,
            "yellowCards": None,
            "form": "Noch nicht recherchiert",
            "starting": "Noch nicht recherchiert",
            "injury": "Noch nicht recherchiert",
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "injurySourceUrl": None,
            "injuryEvidence": "unknown",
            "suspension": "Noch nicht recherchiert",
            "suspensionEvidence": "unknown",
            "lastMatch": None,
        }

    try:
        html = http_get_text(source_url, timeout=20)

        # HTML grob in sichtbaren Text umwandeln.
        text_only = re.sub(r"<script\b[^>]*>.*?</script>", " ", html,
                           flags=re.IGNORECASE | re.DOTALL)
        text_only = re.sub(r"<style\b[^>]*>.*?</style>", " ", text_only,
                           flags=re.IGNORECASE | re.DOTALL)
        text_only = re.sub(r"<[^>]+>", " ", text_only)
        text_only = re.sub(r"\s+", " ", text_only).strip()

        appearances = parse_number_after_label(text_only, "Einsätze")
        goals = parse_number_after_label(text_only, "Tore")
        assists = parse_number_after_label(text_only, "Vorlagen")
        yellow_cards = parse_number_after_label(text_only, "Gelbe Karten")

        # V18: zusätzliche öffentliche Leistungswerte. Nur übernehmen,
        # wenn das Spielerprofil den Wert tatsächlich ausweist.
        starts = parse_float_after_labels(text_only, ("Startelf", "Startelfeinsätze"))
        minutes = parse_float_after_labels(text_only, ("Spielminuten", "Minuten"))
        shots = parse_float_after_labels(text_only, ("Torschüsse", "Schüsse"))
        shots_on_target = parse_float_after_labels(
            text_only, ("Torschüsse aufs Tor", "Schüsse aufs Tor")
        )
        woodwork = parse_float_after_labels(text_only, ("Aluminiumtreffer", "Pfostentreffer"))
        penalties = parse_float_after_labels(text_only, ("Elfmeter",))
        penalties_scored = parse_float_after_labels(
            text_only, ("Verwandelte Elfmeter", "Elfmetertore")
        )
        pass_accuracy = parse_float_after_labels(
            text_only, ("Passquote", "Passgenauigkeit")
        )
        duels_won = parse_float_after_labels(
            text_only, ("Gewonnene Zweikämpfe", "Zweikämpfe gewonnen")
        )
        aerial_duels_won = parse_float_after_labels(
            text_only, ("Gewonnene Kopfballduelle", "Kopfballduelle gewonnen")
        )
        crosses = parse_float_after_labels(text_only, ("Flanken",))
        fouls = parse_float_after_labels(text_only, ("Fouls",))
        red_cards = parse_float_after_labels(text_only, ("Rote Karten",))
        saves = parse_float_after_labels(
            text_only, ("Gehaltene Torschüsse", "Paraden")
        )
        clean_sheets = parse_float_after_labels(
            text_only, ("Weiße Weste", "Zu null", "Clean Sheets")
        )

        goals_against = None
        if _is_goalkeeper(player):
            goals_against = parse_number_after_labels(
                text_only,
                (
                    "Gegentore",
                    "Goals conceded",
                    "Goals against",
                    "Gols contra",
                    "Goles encajados",
                    "Buts encaisses",
                ),
            )

            # Die deutsche Profilseite zeigt diese Torwartstatistik
            # nicht immer als "Gegentore". Wir bleiben bei Bundesliga.com
            # und probieren dafür offizielle Sprachvarianten derselben Seite.
            if goals_against is None:
                for localized_url in _localized_profile_urls(source_url):
                    try:
                        localized_html = http_get_text(
                            localized_url,
                            timeout=20,
                        )
                        localized_text = re.sub(
                            r"<script\b[^>]*>.*?</script>",
                            " ",
                            localized_html,
                            flags=re.IGNORECASE | re.DOTALL,
                        )
                        localized_text = re.sub(
                            r"<style\b[^>]*>.*?</style>",
                            " ",
                            localized_text,
                            flags=re.IGNORECASE | re.DOTALL,
                        )
                        localized_text = re.sub(r"<[^>]+>", " ", localized_text)
                        localized_text = re.sub(r"\s+", " ", localized_text).strip()
                        goals_against = parse_number_after_labels(
                            localized_text,
                            (
                                "Gegentore",
                                "Goals conceded",
                                "Goals against",
                                "Gols contra",
                                "Goles encajados",
                                "Buts encaisses",
                            ),
                        )
                        if goals_against is not None:
                            break
                    except Exception:
                        continue

        # Letztes Spiel nur als Kontext; keine Startelf daraus ableiten.
        last_match = None
        match = re.search(
            r"Letztes Spiel\s+(.{0,180}?)(?:News|Mitspieler|Kompletter Kader|$)",
            text_only,
            flags=re.IGNORECASE,
        )
        if match:
            last_match = re.sub(r"\s+", " ", match.group(1)).strip()

        # Form wird transparent als aktuelle Saisonstatistik
        # dargestellt und nicht als erfundener Rating-Wert.
        form_parts = []
        if appearances is not None:
            form_parts.append(f"{appearances} Einsätze")

        if _is_goalkeeper(player):
            if goals_against is not None:
                form_parts.append(f"{goals_against} Gegentore")
        else:
            if goals is not None:
                form_parts.append(f"{goals} Tore")
            if assists is not None:
                form_parts.append(f"{assists} Vorlagen")

        form = " · ".join(form_parts) if form_parts else "Keine Saisonstatistik gefunden"

        # Bundesliga.com-Spielerprofile liefern nicht zuverlässig
        # eine Startelfquote. Deshalb keine künstliche Prozentzahl.
        starting = "Öffentlich nicht verfügbar"

        # --------------------------------------------------------
        # AKTUELLER VERLETZUNGS-/SPERRSTATUS
        # --------------------------------------------------------
        # Historische News und allgemeine Erwähnungen von Verletzungen
        # dürfen keinen aktuellen Status erzeugen. Deshalb akzeptieren
        # wir nur eindeutig aktuelle Formulierungen.

        current_profile_text = re.split(
            r"\bLetztes Spiel\b",
            text_only,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        lower = current_profile_text.lower()

        injury_patterns = (
            r"\baktuell\s+verletzt\b",
            r"\bderzeit\s+verletzt\b",
            r"\baktuell\s+angeschlagen\b",
            r"\bderzeit\s+angeschlagen\b",
            r"\baktuell\s+krank\b",
            r"\bderzeit\s+krank\b",
            r"\baktuell\s+erkrankt\b",
            r"\bderzeit\s+erkrankt\b",
        )

        suspension_patterns = (
            r"\baktuell\s+gesperrt\b",
            r"\bderzeit\s+gesperrt\b",
            r"\baktuelle\s+sperre\b",
            r"\bderzeitige\s+sperre\b",
        )

        injury_hit = next(
            (pattern for pattern in injury_patterns if re.search(pattern, lower)),
            None,
        )
        suspension_hit = next(
            (pattern for pattern in suspension_patterns if re.search(pattern, lower)),
            None,
        )

        # V14: zuerst die ligaweite offizielle Spieltagübersicht.
        # Das ist ein einziger HTTP-Abruf für alle Kaderspieler.
        official_status = get_matchday_status_for_player(player)

        official_evidence = official_status.get("evidence", "unknown")
        starting = official_status.get(
            "lineupProbability",
            "Öffentlich nicht verfügbar",
        )
        form = _derive_form_from_stats(player)
        yellow_cards = _normalize_yellow_cards(player)

        official_injured = bool(official_status.get("injured"))
        official_suspended = bool(official_status.get("suspended"))

        injured = bool(injury_hit) or official_injured
        suspended = bool(suspension_hit) or official_suspended

        injury_diagnosis = official_status.get("injuryDiagnosis")
        injury_expected_absence = official_status.get("injuryExpectedAbsence")
        injury_source_url = official_status.get("sourceUrl")

        if injured:
            injury = _format_injury(
                True,
                injury_diagnosis,
                injury_expected_absence,
            )
            injury_evidence = "injured"
        elif official_evidence in {"recovered", "fit"}:
            injury = "Fit"
            injury_evidence = official_evidence
        else:
            # Wichtig: "nichts gefunden" ist nicht automatisch "Fit".
            injury = "Status nicht eindeutig verfügbar"
            injury_evidence = "unknown"

        if suspended:
            suspension = "Gesperrt"
            suspension_evidence = "suspended"
        elif official_status.get("checked"):
            suspension = "Keine Sperre"
            suspension_evidence = "checked_no_suspension"
        else:
            suspension = "Noch nicht recherchiert"
            suspension_evidence = "unknown"

        return {
            "available": True,
            "sourceUrl": source_url,
            "appearances": appearances,
            "starts": starts,
            "minutes": minutes,
            "goals": goals,
            "assists": assists,
            "shots": shots,
            "shotsOnTarget": shots_on_target,
            "woodwork": woodwork,
            "penalties": penalties,
            "penaltiesScored": penalties_scored,
            "passAccuracy": pass_accuracy,
            "duelsWon": duels_won,
            "aerialDuelsWon": aerial_duels_won,
            "crosses": crosses,
            "fouls": fouls,
            "yellowCards": yellow_cards,
            "redCards": red_cards,
            "saves": saves,
            "cleanSheets": clean_sheets,
            "goalsAgainst": goals_against,
            "form": form,
            "starting": starting,
            "injury": injury,
            "injuryDiagnosis": injury_diagnosis,
            "injuryExpectedAbsence": injury_expected_absence,
            "injurySourceUrl": injury_source_url,
            "injuryEvidence": injury_evidence,
            "suspension": suspension,
            "suspensionEvidence": suspension_evidence,
            "yellowCards": yellow_cards,
            "lastMatch": last_match,
        }

    except Exception as exc:
        print(
            f"Bundesliga.com-Spielerprofil fehlgeschlagen "
            f"({player.get('name', 'unbekannt')}): {exc}"
        )

        return {
            "available": False,
            "sourceUrl": source_url,
            "appearances": None,
            "goals": None,
            "assists": None,
            "goalsAgainst": None,
            "yellowCards": None,
            "form": "Noch nicht recherchiert",
            "starting": "Noch nicht recherchiert",
            "injury": "Noch nicht recherchiert",
            "injuryDiagnosis": None,
            "injuryExpectedAbsence": None,
            "injurySourceUrl": None,
            "injuryEvidence": "unknown",
            "suspension": "Noch nicht recherchiert",
            "suspensionEvidence": "unknown",
            "lastMatch": None,
        }


def load_active_roster_ids():
    """
    Liest den persönlichen Kader aus kickbase-roster.json.

    Die Workflow-Eingaben sind bei unserem aktuellen Kader-Input
    kurze Kickbase-Namens-Tokens (z.B. "friedrich") und keine
    Bundesliga.com-Spieler-Slugs. Die Tokens werden deshalb später
    gegen Bundesliga.com-ID, vollständigen Namen und eindeutige
    Nachnamen abgeglichen.
    """
    if not ACTIVE_ROSTER_FILE.exists():
        print("Kein kickbase-roster.json vorhanden.")
        print("Player-Intelligence-Recherche für persönlichen Kader: 0 Spieler.")
        return []

    try:
        raw = json.loads(ACTIVE_ROSTER_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"kickbase-roster.json konnte nicht gelesen werden: {exc}")
        return []

    players = raw.get("players", []) if isinstance(raw, dict) else raw
    if not isinstance(players, list):
        print("kickbase-roster.json hat kein gültiges players-Array.")
        return []

    tokens = []
    seen = set()

    for item in players:
        if isinstance(item, str):
            token = item.strip()
        elif isinstance(item, dict):
            token = str(
                item.get("id")
                or item.get("name")
                or item.get("player")
                or ""
            ).strip()
        else:
            continue

        if token:
            normalized = normalize_name(token)
            if normalized and normalized not in seen:
                seen.add(normalized)
                tokens.append(token)

    if len(tokens) > 18:
        raise RuntimeError(
            f"Der persönliche Kader enthält {len(tokens)} Spieler. "
            "Maximal 18 Spieler erlaubt."
        )

    print(
        f"Aktueller persönlicher Kader für Player Intelligence: "
        f"{len(tokens)}/18 Spieler (kickbase-roster.json)."
    )
    print("Kader-Tokens: " + ", ".join(tokens))
    return tokens


def resolve_active_player_ids(bundesliga_squads, roster_tokens):
    """
    Löst die Kader-Tokens robust gegen Bundesliga.com-Spieler auf.

    Match-Reihenfolge:
    1. exakte Bundesliga.com-ID
    2. exakter voller Name
    3. eindeutiger Nachname
    4. eindeutiger Namensbestandteil / zusammengesetzter Name

    Dabei werden Umlaute, Akzente, Bindestriche und Groß-/Kleinschreibung
    normalisiert. API-Football wird hierfür nicht verwendet.
    """
    def norm(value):
        value = str(value or "").strip().lower()
        value = unicodedata.normalize("NFKD", value)
        value = "".join(ch for ch in value if not unicodedata.combining(ch))
        value = value.replace("ß", "ss")
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return " ".join(value.split())

    all_players = []
    for squad in bundesliga_squads.values():
        all_players.extend(squad)

    roster_norms = {norm(token) for token in roster_tokens if norm(token)}

    # Bekannte Kickbase-Kurz-IDs, deren Schreibweise nicht 1:1 dem
    # Bundesliga.com-Namen entspricht. Diese Zuordnung ist bewusst eng
    # begrenzt und dient nur der eindeutigen Auflösung des persönlichen
    # Kaders; es wird keine externe Fußball-API benötigt.
    explicit_aliases = {
        "friedrich": ["marvin friedrich"],
        "koemuer": ["mert koemuer", "mert kömür"],
        "hoeler": ["lucas hoeler", "lucas höler"],
    }

    # Alle möglichen eindeutigen Namensbestandteile vorbereiten.
    surname_counts = {}
    token_counts = {}

    player_meta = []
    for player in all_players:
        player_id = str(player.get("id", "")).strip()
        player_name = str(player.get("name", "")).strip()
        name_norm = norm(player_name)
        parts = name_norm.split()

        surname = parts[-1] if parts else ""
        if surname:
            surname_counts[surname] = surname_counts.get(surname, 0) + 1

        for part in set(parts):
            if len(part) >= 4:
                token_counts[part] = token_counts.get(part, 0) + 1

        player_meta.append((player, player_id, name_norm, parts, surname))

    active_ids = set()
    matches = []
    unresolved = []

    for roster_token in roster_tokens:
        token_norm = norm(roster_token)
        if not token_norm:
            unresolved.append(roster_token)
            continue

        candidates = []

        # 1) Exakte ID / voller Name
        for player, player_id, name_norm, parts, surname in player_meta:
            if token_norm == norm(player_id) or token_norm == name_norm:
                candidates.append(player)

        # 1b) Explizite, eindeutige Kickbase-Kurz-ID.
        # Nur verwenden, wenn genau ein Bundesliga.com-Spieler passt.
        if not candidates and token_norm in explicit_aliases:
            alias_names = {norm(name) for name in explicit_aliases[token_norm]}
            alias_candidates = [
                player
                for player, player_id, name_norm, parts, surname
                in player_meta
                if name_norm in alias_names
            ]
            if len(alias_candidates) == 1:
                candidates = alias_candidates

        # 2) Eindeutiger Nachname
        if not candidates and surname_counts.get(token_norm, 0) == 1:
            candidates = [
                player for player, player_id, name_norm, parts, surname
                in player_meta
                if surname == token_norm
            ]

        # 3) Eindeutiger Namensbestandteil
        if not candidates and len(token_norm) >= 4:
            candidates = [
                player for player, player_id, name_norm, parts, surname
                in player_meta
                if token_norm in parts and token_counts.get(token_norm, 0) == 1
            ]

        # 4) Zusammengesetzte/leicht abweichende Schreibweise:
        #    Token muss vollständig in genau einem Namen vorkommen.
        if not candidates and len(token_norm) >= 5:
            fuzzy = [
                player for player, player_id, name_norm, parts, surname
                in player_meta
                if token_norm in name_norm
            ]
            if len(fuzzy) == 1:
                candidates = fuzzy

        if len(candidates) == 1:
            player = candidates[0]
            player_id = str(player.get("id", "")).strip()
            if player_id:
                active_ids.add(player_id)
                matches.append(
                    f"{roster_token} -> "
                    f"{player.get('name', 'Unbekannt')} [{player_id}]"
                )
        else:
            unresolved.append(roster_token)

    print(
        "Aktive Spieler für intensive Recherche: "
        f"{len(active_ids)}/{len(roster_tokens)} Kader-Tokens zugeordnet."
    )

    if matches:
        print("Zugeordnete Spieler: " + "; ".join(matches))

    if unresolved:
        print("Nicht eindeutig zugeordnet: " + ", ".join(unresolved))

    return active_ids


def _extract_roster_ids(players):
    if not isinstance(players, list):
        return []

    ids = []
    seen = set()

    for item in players:
        if isinstance(item, str):
            player_id = item.strip()
        elif isinstance(item, dict):
            player_id = str(item.get("id", "")).strip()
        else:
            continue

        if player_id and player_id not in seen:
            seen.add(player_id)
            ids.append(player_id)

    if len(ids) > 18:
        raise RuntimeError(
            f"Der persönliche Kader enthält {len(ids)} Spieler. "
            "Maximal 18 Spieler erlaubt."
        )

    return ids


def build_player_intelligence(
    player,
    club_name,
    next_match,
    old_player=None,
    public_source=None,
    research_player=False,
):
    """
    Baut einen Spieler-Eintrag aus vorhandenen Daten und optionaler
    Einzelspieler-Recherche.

    V10-Änderung:
    - Standardmäßig KEIN Abruf des einzelnen Spielerprofils.
    - Nur IDs aus kickbase-roster.json werden intensiv recherchiert.
    - Vorhandene Intelligence bleibt für alle anderen Spieler erhalten.
    - Keine Kickbase-Daten werden erfunden.
    """
    player_id = player.get("id")
    name = player.get("name", "Unbekannter Spieler")
    old_player = old_player or {}
    public_source = public_source or {}

    if next_match:
        opponent = next_match.get("opponent")
        home_away = next_match.get("homeAway")
    else:
        opponent = None
        home_away = None

    if research_player:
        research_input = dict(player)
        research_input["club"] = club_name
        research_input["_opponent"] = (
            next_match.get("opponent")
            if isinstance(next_match, dict)
            else None
        )
        research_input["_matchday"] = _matchday_from_next_match(next_match)
        public_player = extract_player_profile_intelligence(research_input)

        print(
            f"PLAYER-STATUS {name} [{player_id}]: "
            f"injury={public_player.get('injury')} | "
            f"injuryEvidence={public_player.get('injuryEvidence')} | "
            f"suspension={public_player.get('suspension')} | "
            f"suspensionEvidence={public_player.get('suspensionEvidence')} | "
            f"injurySourceUrl={public_player.get('injurySourceUrl')}"
        )

        average = old_player.get("average")
        starting = public_player.get("starting", old_player.get("starting", "Noch nicht recherchiert"))
        form = public_player.get("form", old_player.get("form", "Noch nicht recherchiert"))
        injury = public_player.get("injury", old_player.get("injury", "Noch nicht recherchiert"))
        injury_diagnosis = public_player.get(
            "injuryDiagnosis",
            old_player.get("injuryDiagnosis"),
        )
        injury_expected_absence = public_player.get(
            "injuryExpectedAbsence",
            old_player.get("injuryExpectedAbsence"),
        )
        injury_source_url = public_player.get(
            "injurySourceUrl",
            old_player.get("injurySourceUrl"),
        )
        injury_evidence = public_player.get("injuryEvidence", "unknown")

        suspension = public_player.get(
            "suspension",
            old_player.get("suspension", "Noch nicht recherchiert"),
        )
        suspension_evidence = public_player.get(
            "suspensionEvidence",
            "unknown",
        )

        injury = _normalize_injury_label(injury)
        suspension = _normalize_suspension_label(suspension)

        # --------------------------------------------------------
        # GENERISCHES STATUS-GEDÄCHTNIS
        # --------------------------------------------------------
        # Eine bereits offiziell bestätigte Verletzung darf durch einen
        # späteren erfolglosen Parser-Lauf nicht verschwinden.
        # Sie endet erst bei neuer Verletzungsevidenz oder expliziter
        # Rückkehr-/Fit-Evidenz.
        old_injury = _normalize_injury_label(
            old_player.get("injury")
        )
        old_injury_confirmed = (
            isinstance(old_injury, str)
            and old_injury.startswith("Verletzt")
        )

        if (
            injury_evidence in {"unknown", "budget_exhausted"}
            and old_injury_confirmed
        ):
            injury = old_injury
            injury_diagnosis = old_player.get("injuryDiagnosis")
            injury_expected_absence = old_player.get(
                "injuryExpectedAbsence"
            )
            injury_source_url = old_player.get("injurySourceUrl")
            injury_evidence = old_player.get(
                "injuryEvidence",
                "carried_confirmed_injury",
            )

        if (
            suspension_evidence in {"unknown", "budget_exhausted"}
            and old_player.get("suspension") in {"Gesperrt", "Keine Sperre"}
        ):
            suspension = old_player.get("suspension")
            suspension_evidence = old_player.get(
                "suspensionEvidence",
                "carried_status",
            )

        appearances = public_player.get("appearances", old_player.get("appearances"))
        goals = public_player.get("goals", old_player.get("goals"))
        assists = public_player.get("assists", old_player.get("assists"))
        goals_against = public_player.get("goalsAgainst", old_player.get("goalsAgainst"))
        yellow_cards = public_player.get("yellowCards", old_player.get("yellowCards"))
        last_match = public_player.get("lastMatch", old_player.get("lastMatch"))
        profile_available = public_player.get("available", False)
        profile_url = public_player.get("sourceUrl") or player.get("sourceUrl")
    else:
        # Keine Netzwerkanfrage: vorhandene Daten werden vollständig erhalten.
        average = old_player.get("average")
        starting = old_player.get("starting", "Noch nicht recherchiert")
        form = old_player.get("form", "Noch nicht recherchiert")
        injury = old_player.get("injury", "Noch nicht recherchiert")
        injury_diagnosis = old_player.get("injuryDiagnosis")
        injury_expected_absence = old_player.get("injuryExpectedAbsence")
        injury_source_url = old_player.get("injurySourceUrl")
        injury_evidence = old_player.get("injuryEvidence", "unknown")

        suspension = old_player.get("suspension", "Noch nicht recherchiert")
        suspension_evidence = old_player.get(
            "suspensionEvidence",
            "unknown",
        )

        injury = _normalize_injury_label(injury)
        suspension = _normalize_suspension_label(suspension)

        appearances = old_player.get("appearances")
        goals = old_player.get("goals")
        assists = old_player.get("assists")
        goals_against = old_player.get("goalsAgainst")
        yellow_cards = old_player.get("yellowCards")
        last_match = old_player.get("lastMatch")
        profile_available = bool(old_player.get("dataStatus", {}).get("playerProfileSource") == "erreichbar")
        profile_url = old_player.get("sourceUrl") or player.get("sourceUrl")

    # V19: Performance-Layer.
    # Spielerprofil + ligaweite Bundesliga-Rankings werden zusammengeführt.
    old_performance = old_player.get("performance") or {}
    performance_sources = dict(old_player.get("performanceSources") or {})

    if research_player:
        ranking_values, ranking_sources = collect_bundesliga_rankings_for_player(
            research_input
        )

        perf_values = {
            "appearances": public_player.get("appearances"),
            "starts": public_player.get("starts"),
            "minutes": public_player.get("minutes"),
            "goals": public_player.get("goals"),
            "assists": public_player.get("assists"),
            "shots": public_player.get("shots"),
            "shotsOnTarget": public_player.get("shotsOnTarget"),
            "woodwork": public_player.get("woodwork"),
            "penalties": public_player.get("penalties"),
            "penaltiesScored": public_player.get("penaltiesScored"),
            "passAccuracy": public_player.get("passAccuracy"),
            "duelsWon": public_player.get("duelsWon"),
            "aerialDuelsWon": public_player.get("aerialDuelsWon"),
            "crosses": public_player.get("crosses"),
            "fouls": public_player.get("fouls"),
            "yellowCards": public_player.get("yellowCards"),
            "redCards": public_player.get("redCards"),
            "saves": public_player.get("saves"),
            "cleanSheets": public_player.get("cleanSheets"),
            "goalsAgainst": public_player.get("goalsAgainst"),
        }

        # Ranking-Werte ergänzen/überschreiben nur, wenn sie tatsächlich
        # verfügbar sind. So bleiben Profilwerte für nicht abgedeckte Felder.
        for metric_key, value in ranking_values.items():
            if value is not None:
                perf_values[metric_key] = value

        performance_sources.update(ranking_sources)

    else:
        perf_values = dict(old_performance)

    performance, data_coverage = build_performance_layer(
        perf_values,
        old_performance=old_performance,
    )

    if research_player:
        projection_context = dict(old_player)
        projection_context.update({
            "starting": starting,
            "injury": injury,
            "suspension": suspension,
            "homeAway": home_away,
        })
        kickbase_ai_projection = build_kickbase_ai_projection(
            projection_context, performance, data_coverage
        )
        print(
            f"AI-PROJECTION {name}: "
            f"{kickbase_ai_projection.get('expectedPoints')} "
            f"[{kickbase_ai_projection.get('rangeMin')}-"
            f"{kickbase_ai_projection.get('rangeMax')}] | "
            f"{kickbase_ai_projection.get('recommendation')} | "
            f"Confidence {kickbase_ai_projection.get('confidence')}%"
        )
    else:
        kickbase_ai_projection = old_player.get("kickbaseAiProjection")

    if research_player:
        print(
            f"PERFORMANCE {name}: Coverage {data_coverage['coveragePercent']}% | "
            f"vorhanden={len(data_coverage['availableMetrics'])} | "
            f"fehlend={len(data_coverage['missingMetrics'])}"
        )

    # Dynamische Spieltagsdaten immer neu berechnen.
    old_recommendation = old_player.get("recommendation")
    if old_recommendation and old_recommendation != "Noch nicht recherchiert":
        recommendation = old_recommendation
    elif next_match:
        recommendation = "Nächstes Spiel vorhanden"
    else:
        recommendation = "Noch nicht ausreichend Daten"

    return {
        "id": player_id,
        "name": name,
        "club": club_name,
        "position": player.get("position"),
        "number": player.get("number"),
        "sourceUrl": player.get("sourceUrl"),
        "average": average,
        "starting": starting,
        "form": form,
        "footballRating": (
            kickbase_ai_projection.get("expectedPoints")
            if kickbase_ai_projection
            else old_player.get("footballRating")
        ),
        "kickbaseAiScore": (
            kickbase_ai_projection.get("expectedPoints")
            if kickbase_ai_projection
            else None
        ),
        "kickbaseAiProjection": kickbase_ai_projection,
        "performance": performance,
        "performanceSources": performance_sources,
        "dataCoverage": data_coverage,
        "appearances": appearances,
        "starts": old_player.get("starts"),
        "minutes": old_player.get("minutes"),
        "goals": goals,
        "assists": assists,
        "goalsAgainst": goals_against,
        "yellowCards": yellow_cards,
        "lastMatch": last_match,
        "opponent": opponent,
        "homeAway": home_away,
        "injury": injury,
        "injuryDiagnosis": injury_diagnosis,
        "injuryExpectedAbsence": injury_expected_absence,
        "injurySourceUrl": injury_source_url,
        "injuryEvidence": injury_evidence,
        "suspension": suspension,
        "suspensionEvidence": suspension_evidence,
        "recommendation": recommendation,
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sources": [
            {"name": "Bundesliga.com", "url": BUNDESLIGA_PLAYERS_URL},
            {"name": "Bundesliga.com Statistik", "url": BUNDESLIGA_STATS_URL},
            {"name": "Bundesliga.com Spielerprofil", "url": profile_url},
            *(
                [{"name": "Offizielle Vereinsmeldung", "url": injury_source_url}]
                if injury_source_url
                else []
            ),
            {"name": "OpenLigaDB", "url": "https://www.openligadb.de/"},
        ],
        "dataStatus": {
            "kickbaseAverage": "vorhanden" if average is not None else "nicht verfügbar",
            "publicStatsSource": "erreichbar" if public_source.get("available") else "nicht erreichbar",
            "playerProfileSource": "erreichbar" if profile_available else "nicht recherchiert",
            "playerValues": "nicht erfunden",
            "researchMode": "intensiv" if research_player else "bestehende_daten_beibehalten",
        },
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
    print("Prüfe öffentliche Bundesliga-Statistikquelle...")
    public_source = get_public_player_intelligence_source()

    old_data = load_old_data()
    old_teams = old_data.get("teams", {}) if isinstance(old_data, dict) else {}
    old_players = old_data.get("players", {}) if isinstance(old_data, dict) else {}
    active_roster_ids = load_active_roster_ids()
    active_player_ids = resolve_active_player_ids(
        bundesliga_squads,
        active_roster_ids,
    )
    print(
        "V21-AI-Prognose-Modus: Position + erwartete Spielzeit + Performance + Status + Unsicherheit nur für den aufgelösten "
        f"aktiven Kader ({len(active_player_ids)} Spieler); "
        "alle übrigen Spieler behalten ihre vorhandenen Werte."
    )

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

            old_player = old_players.get(
                player_id,
                {},
            )

            data["players"][player_id] = (
                build_player_intelligence(
                    player,
                    team_name,
                    next_match,
                    old_player=old_player,
                    public_source=public_source,
                    research_player=(player_id in active_player_ids),
                )
            )

            # V10: Kein Sleep für normale Spieler, weil dort kein
            # Einzelspieler-Profil abgerufen wird. Bei aktivem Kader
            # wird zwischen öffentlichen Profilabrufen kurz pausiert.
            if player_id in active_player_ids:
                time.sleep(0.25)

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

    data["architecture"] = {
        "kickbaseSource": "nicht verwendet",
        "teamsAndPlayers": "Bundesliga.com",
        "fixtures": "OpenLigaDB",
        "playerStats": "bestehende Werte + optionale Einzelspieler-Web-Recherche für aktiven Kader",
        "externalFootballApi": False,
        "activeRosterSource": "kickbase-roster.json (optional)",
    }

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
        "Keine externe Fußball-API für Player-Intelligence verwendet."
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
