import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser
from urllib.parse import urljoin

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
            "yellowCards": None,
            "form": "Noch nicht recherchiert",
            "starting": "Noch nicht recherchiert",
            "injury": "Noch nicht recherchiert",
            "suspension": "Noch nicht recherchiert",
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
        if goals is not None:
            form_parts.append(f"{goals} Tore")
        if assists is not None:
            form_parts.append(f"{assists} Vorlagen")

        form = " · ".join(form_parts) if form_parts else "Keine Saisonstatistik gefunden"

        # Bundesliga.com-Spielerprofile liefern nicht zuverlässig
        # eine Startelfquote. Deshalb keine künstliche Prozentzahl.
        starting = "Öffentlich nicht verfügbar"

        # Sehr konservative Recherche: nur explizite Hinweise auf
        # Verletzung/Krankheit/Sperre im öffentlichen Profiltext.
        lower = text_only.lower()

        injury_terms = (
            "verletzt", "verletzung", "angeschlagen",
            "krank", "erkrankt", "muskelverletzung",
        )
        suspension_terms = (
            "gesperrt", "sperre", "rotgesperrt",
            "gelb-rot", "gelbrote karte",
        )

        injury = "Keine Verletzungsmeldung auf dem Profil gefunden"
        suspension = "Keine Sperrmeldung auf dem Profil gefunden"

        injury_hit = next((term for term in injury_terms if term in lower), None)
        suspension_hit = next((term for term in suspension_terms if term in lower), None)

        if injury_hit:
            injury = f"Hinweis auf: {injury_hit}"
        if suspension_hit:
            suspension = f"Hinweis auf: {suspension_hit}"

        return {
            "available": True,
            "sourceUrl": source_url,
            "appearances": appearances,
            "goals": goals,
            "assists": assists,
            "yellowCards": yellow_cards,
            "form": form,
            "starting": starting,
            "injury": injury,
            "suspension": suspension,
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
            "yellowCards": None,
            "form": "Noch nicht recherchiert",
            "starting": "Noch nicht recherchiert",
            "injury": "Noch nicht recherchiert",
            "suspension": "Noch nicht recherchiert",
            "lastMatch": None,
        }


def load_active_roster_ids():
    """
    Liefert die Spieler-IDs des persönlichen Kaders.

    Primär wird der aktuelle workflow_dispatch-Input aus der
    Umgebungsvariable ROSTER_INPUT verwendet. Das ist absichtlich
    robuster als sich darauf zu verlassen, dass die temporäre
    kickbase-roster.json korrekt gelesen wird.

    Fallback:
    - kickbase-roster.json, falls vorhanden.

    Ein leerer Kader bedeutet immer 0 Recherche-Spieler und niemals
    "alle Bundesliga-Spieler".
    """

    raw = os.environ.get("ROSTER_INPUT", "").strip()

    # workflow_dispatch: direkter Kader-Input
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"ROSTER_INPUT enthält ungültiges JSON: {exc}")
            return []

        players = parsed.get("players", []) if isinstance(parsed, dict) else parsed
        ids = _extract_roster_ids(players)

        print(
            f"Aktueller persönlicher Kader für Player Intelligence: "
            f"{len(ids)}/18 Spieler (direkter Workflow-Input)."
        )
        return ids

    # Fallback für einen lokal bereitgestellten Kader.
    if ACTIVE_ROSTER_FILE.exists():
        try:
            raw_file = json.loads(
                ACTIVE_ROSTER_FILE.read_text(encoding="utf-8")
            )
        except Exception as exc:
            print(f"kickbase-roster.json konnte nicht gelesen werden: {exc}")
            return []

        players = (
            raw_file.get("players", [])
            if isinstance(raw_file, dict)
            else raw_file
        )
        ids = _extract_roster_ids(players)

        print(
            f"Aktueller persönlicher Kader für Player Intelligence: "
            f"{len(ids)}/18 Spieler (kickbase-roster.json)."
        )
        return ids

    print("Kein persönlicher Kader übergeben.")
    print("Player-Intelligence-Recherche für persönlichen Kader: 0 Spieler.")
    return []


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
        public_player = extract_player_profile_intelligence(player)

        average = old_player.get("average")
        starting = public_player.get("starting", old_player.get("starting", "Noch nicht recherchiert"))
        form = public_player.get("form", old_player.get("form", "Noch nicht recherchiert"))
        injury = public_player.get("injury", old_player.get("injury", "Noch nicht recherchiert"))
        suspension = public_player.get("suspension", old_player.get("suspension", "Noch nicht recherchiert"))
        appearances = public_player.get("appearances", old_player.get("appearances"))
        goals = public_player.get("goals", old_player.get("goals"))
        assists = public_player.get("assists", old_player.get("assists"))
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
        suspension = old_player.get("suspension", "Noch nicht recherchiert")
        appearances = old_player.get("appearances")
        goals = old_player.get("goals")
        assists = old_player.get("assists")
        yellow_cards = old_player.get("yellowCards")
        last_match = old_player.get("lastMatch")
        profile_available = bool(old_player.get("dataStatus", {}).get("playerProfileSource") == "erreichbar")
        profile_url = old_player.get("sourceUrl") or player.get("sourceUrl")

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
        "footballRating": old_player.get("footballRating"),
        "appearances": appearances,
        "starts": old_player.get("starts"),
        "minutes": old_player.get("minutes"),
        "goals": goals,
        "assists": assists,
        "yellowCards": yellow_cards,
        "lastMatch": last_match,
        "opponent": opponent,
        "homeAway": home_away,
        "injury": injury,
        "suspension": suspension,
        "recommendation": recommendation,
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "sources": [
            {"name": "Bundesliga.com", "url": BUNDESLIGA_PLAYERS_URL},
            {"name": "Bundesliga.com Statistik", "url": BUNDESLIGA_STATS_URL},
            {"name": "Bundesliga.com Spielerprofil", "url": profile_url},
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
    print(
        "V10-Modus: Einzelspieler-Recherche nur für den aktiven "
        f"Kader ({len(active_roster_ids)} IDs); alle übrigen Spieler behalten ihre vorhandenen Werte."
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
                    research_player=(player_id in active_roster_ids),
                )
            )

            # V10: Kein Sleep für normale Spieler, weil dort kein
            # Einzelspieler-Profil abgerufen wird. Bei aktivem Kader
            # wird zwischen öffentlichen Profilabrufen kurz pausiert.
            if player_id in active_roster_ids:
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
