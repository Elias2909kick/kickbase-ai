import json
import os
from datetime import datetime, timezone
from pathlib import Path

from openai import OpenAI


BASE_DIR = Path(__file__).resolve().parent.parent
INTELLIGENCE_FILE = BASE_DIR / "player-intelligence.json"

MODEL = "gpt-5.6-luna"


def load_intelligence():
    with INTELLIGENCE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_intelligence(data):
    with INTELLIGENCE_FILE.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def research_player(client, player_name, club, position):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    prompt = f"""
Du bist die Player-Intelligence-Recherche für Kickbase AI.

Recherchiere aktuelle öffentlich zugängliche Informationen über diesen
Fußballspieler:

Name: {player_name}
Verein: {club}
Position: {position}

Aktuelles Datum: {today}

Nutze ausschließlich öffentlich zugängliche Quellen.
VERWENDE KEINE Kickbase-Daten und keine Kickbase-API.

Recherchiere insbesondere:

1. starting
   Wie wahrscheinlich ist ein Startelfeinsatz im nächsten Spiel?

2. form
   Wie ist die aktuelle sportliche Form einzuschätzen?

3. opponent
   Wer ist der nächste Gegner?

4. homeAway
   Findet das nächste Spiel zuhause oder auswärts statt?

5. injury
   Gibt es eine aktuelle Verletzung oder eine glaubwürdige Meldung
   über eine mögliche Verletzung?

6. suspension
   Gibt es eine aktuelle Sperre oder droht eine Sperre?

7. recommendation
   Gib eine Empfehlung:
   "Starten", "Beobachten", "Bank" oder "Verkaufen".

8. average
   Nur eintragen, wenn ein belastbarer aktueller Durchschnittswert
   aus einer öffentlich zugänglichen Quelle gefunden wird.
   Andernfalls null.

WICHTIG:
- Keine Informationen erfinden.
- Wenn eine Information nicht zuverlässig feststellbar ist: null.
- Bei widersprüchlichen Quellen die Unsicherheit berücksichtigen.
- Die Empfehlung muss aus den recherchierten Informationen abgeleitet werden.
- Quellen müssen tatsächlich für die Recherche verwendet worden sein.
- Bevorzuge offizielle Vereins-/Ligaquellen und etablierte Sportmedien.
- Kickbase darf nicht als Quelle verwendet werden.

Gib ausschließlich das geforderte JSON zurück.
"""

    response = client.responses.create(
        model=MODEL,
        tools=[
            {
                "type": "web_search",
                "search_context_size": "medium",
            }
        ],
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "player_intelligence",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "average": {
                            "type": ["string", "null"]
                        },
                        "starting": {
                            "type": ["string", "null"]
                        },
                        "form": {
                            "type": ["string", "null"]
                        },
                        "opponent": {
                            "type": ["string", "null"]
                        },
                        "homeAway": {
                            "type": ["string", "null"]
                        },
                        "injury": {
                            "type": ["string", "null"]
                        },
                        "suspension": {
                            "type": ["string", "null"]
                        },
                        "recommendation": {
                            "type": ["string", "null"]
                        },
                        "sources": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {
                                        "type": "string"
                                    },
                                    "url": {
                                        "type": "string"
                                    },
                                    "date": {
                                        "type": ["string", "null"]
                                    }
                                },
                                "required": [
                                    "title",
                                    "url",
                                    "date"
                                ],
                                "additionalProperties": False
                            }
                        },
                        "lastUpdated": {
                            "type": "string"
                        }
                    },
                    "required": [
                        "average",
                        "starting",
                        "form",
                        "opponent",
                        "homeAway",
                        "injury",
                        "suspension",
                        "recommendation",
                        "sources",
                        "lastUpdated"
                    ],
                    "additionalProperties": False
                }
            }
        }
    )

    return json.loads(response.output_text)


def main():
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY wurde in GitHub Actions nicht gefunden."
        )

    client = OpenAI(api_key=api_key)

    data = load_intelligence()

    result = research_player(
        client=client,
        player_name="Kristof",
        club="Elversberg",
        position="TW",
    )

    data["kristof"] = result

    save_intelligence(data)

    print("Player Intelligence für Kristof aktualisiert.")


if __name__ == "__main__":
    main()
