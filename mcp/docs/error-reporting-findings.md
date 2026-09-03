# Fehlermeldungen der MCP-Tools kamen nicht beim Aufrufer an

Befund vom 2026-09-01, entstanden nebenbei beim Umbau der Data Table
"AgentScript Pages by Channel". **Erledigt am selben Tag.** Dieses Dokument
beschreibt die tatsächliche Ursache, die Behebung und die Regel, die daraus
folgt — die erste Fassung enthielt eine falsche Ursachenannahme, siehe unten.

## Die Ursache

Das MCP-Framework unterscheidet zwei Arten von Fehlschlägen, und der Unterschied
entscheidet, ob eine Meldung sichtbar wird:

| Geworfen wird | Was der Aufrufer sieht |
| --- | --- |
| `ToolError` | die eigene Meldung, `is_error=True`, Log auf INFO ohne Traceback |
| alles andere | nur `Error executing tool <name>`, Log auf ERROR mit Traceback |

Das Toolkit warf durchgehend `ValueError` und `RuntimeError`. Beide fallen in
die zweite Zeile: das Framework wertet sie als Absturz und hält den Text
zurück, weil ein Absturz nichts Vorhersehbares mitzuteilen hat. Genau die
Begründungen, die für diesen Moment geschrieben wurden, gingen dadurch verloren.

Nicht betroffen waren Schema-Validierungsfehler: ein fehlendes Pflichtargument
kam immer sauber an (`1 validation error for export_flowArguments`, beobachtet
bei `export_flow` ohne `flow_type`), weil pydantic eigenständig meldet, bevor
der Tool-Körper überhaupt läuft.

### Die falsche Annahme der ersten Fassung

Ursprünglich stand hier, der Text werde "unterwegs verworfen", also vom
Transport. Daraus folgte der Vorschlag, Fehler als `return {"error": ...}`
zurückzugeben statt zu werfen — ein Umgehen des Problems statt einer Behebung.
Das war falsch und ist zurückgebaut. Die Texte lassen sich durchreichen; es
brauchte nur die richtige Exception-Klasse.

## Was behoben wurde

| Umfang | Änderung |
| --- | --- |
| 50 `raise`-Stellen in 11 Dateien | `ValueError`/`RuntimeError` → `ToolError` |
| 6 `return {"error": ...}` in `data_tables.py` | zurück auf `raise ToolError` |
| 5 Exception-Klassen | Basisklasse `RuntimeError` → `ToolError` |

Die fünf Klassen sind `ApiError` (`client.py`), `ConfigError` (`config.py`),
`ArchyError` (`archy.py`), `WorkspaceError` (`workspace.py`) und
`AmbiguousReference` (`resolve.py`).

**`ApiError` war der schwerste Fall.** Sie wird bei jedem fehlgeschlagenen
Genesys-Aufruf im gesamten Toolkit geworfen — jeder 403, jeder 400 mit einer
präzisen Begründung von Genesys, jedes abgelehnte Schema kam als blankes
`Error executing tool` an. Das war die eigentliche Quelle der stummen
Fehlschläge; die kaputte `create_data_table` war nur der Anlass, unter dem sie
auffiel.

Live verifiziert nach dem Neustart:

```
GET /api/v2/flows/datatables/<gelöschte-id>
→ failed with 404: flows.datatables.table.not.found - Not found

get_queue(queue="Diese Queue Gibt Es Nicht XYZ")
→ No queue found matching 'Diese Queue Gibt Es Nicht XYZ'.
```

Beide Aufrufe lieferten vorher nur `Error executing tool`.

### Nebenwirkung der umgehängten Basisklassen

`archy.py`, `config.py` und `workspace.py` hingen bisher nicht am MCP-Paket,
jetzt schon. Innerhalb der venv ist das folgenlos, `audit_flows.py` als
einziges Skript außerhalb läuft weiter. Sollten diese Module einmal wirklich
eigenständig verwendbar sein, wäre das der Punkt, an dem eine eigene
Basisklasse einzuziehen wäre statt direkt an `ToolError` zu hängen.

Im Repo gibt es kein `except RuntimeError`, und die einzige Nutzung außerhalb
des Pakets fängt mit `except archy.ArchyError` auf die konkrete Klasse. Der
Wechsel der Basisklasse ist daher nirgends spürbar.

## Die Regel

Für einen Fehlschlag, den der Code kommen sieht, `ToolError` werfen — für
Bedienfehler ebenso wie für verletzte Nachbedingungen. Beide brauchen ihren
Text, und beide sollen als Fehler erkennbar bleiben, statt als Ergebnis-Dict
mit `error`-Feld durchzugehen, das ein Aufrufer für Erfolg halten kann.

Ausnahme: **innerhalb eines pydantic-Validators bleibt `ValueError` richtig**,
weil pydantic den erwartet und selbst als Argumentfehler meldet. Im Toolkit gibt
es derzeit keine solchen Validatoren.

Eine fehlende Import-Zeile fällt hier nicht beim Modulimport auf, sondern erst,
wenn der Fehlerpfad läuft — also in genau dem Zweig, den niemand testet. Genau
das ist beim Umbau in `data_tables.py` passiert und wurde nur durch eine
statische Prüfung gefunden, ob jeder geworfene Name auch aufgelöst werden kann.

## Was die fehlenden Meldungen verdeckt hatten

Drei Vorfälle an einem Tag, alle durch dieselbe Ursache verlängert.

**Der Schlüsselspalten-Bug.** `upsert_data_table_row` schlug bei *jedem* Aufruf
fehl, auch beim wirkungslosen Neuschreiben einer unveränderten Zeile. Die
Schlüsselspalte wurde aus `schema["title"]` gelesen, das ist aber der
Tabellenname. Ein Ein-Zeilen-Bug, der über einen Umweg gefunden werden musste:
dieselbe Operation per `genesys_api_call`, dann den Quellcode lesen.

**Der Schema-Bug.** Beim Anlegen der Tabelle "Org to SC" scheiterte
`create_data_table` zweimal blank. Das erzeugte Schema hatte kein `required`,
in dem jede reale Tabelle der Org ihre Schlüsselspalte führt. Ein Kommentar im
Code nahm zudem an, Genesys schreibe das Schema beim Anlegen um — das stimmt
nicht, Genesys speichert, was es bekommt. Der Bug lag seit dem ersten Commit
der Datei drin und blieb unentdeckt, weil die Ablehnung der API nie sichtbar
wurde.

Behoben, indem der Body jetzt die nachweislich akzeptierte Form erzeugt:
Schlüsselproperty immer `key` mit dem übergebenen Spaltennamen als `title`,
`required: ["key"]`, Längengrenzen auf allen String-Spalten, Schema-Titel
gleich Tabellenname, `additionalProperties: false`, plus eine Prüfung auf die
Kollision, wenn eine Nicht-Schlüsselspalte selbst `key` heißt.

**Die falsche Aussage über Berechtigungen.** Beim Aufräumen scheiterte
`genesys_api_call` mit `DELETE` zweimal blank, einmal auf einem Routing-Skill,
einmal auf einer Wegwerf-Tabelle. Die naheliegende Diagnose lautete "der
Toolkit-Rolle fehlt eine Berechtigung" — und die war falsch. `raw.py` verlangt
für `DELETE` ein `confirm_delete_path`, das den Pfad exakt wiederholt; fehlt
es, lehnt das Tool mit genau dieser Erklärung ab.

Das war der teuerste der drei Fälle, weil er nicht nur Zeit gekostet, sondern
zu einer falschen Aussage über die Org geführt hat. Ein Argument, das
ausschließlich einen Unfall verhindern soll, ist wertlos, wenn seine Begründung
nicht ankommt.

## Commits

| Commit | Inhalt |
| --- | --- |
| `23bbc2c` | Schlüsselspalte aus `schema["required"]` lesen statt aus `title`. Der eigentliche Bug — `upsert_data_table_row` war für **jede** Tabelle funktionslos, `get_data_table_schema` meldete den Tabellennamen als Schlüsselspalte. |
| `193d97c` | Prüfungen in `upsert_data_table_row` geben `{"error": ...}` zurück statt zu werfen. *Später zurückgebaut — siehe die falsche Annahme oben.* |
| `8d9a531` | Dasselbe in `create_data_table`, plus eine Prüfung auf Spalten ohne `name` (vorher ein `KeyError` aus einer List Comprehension). *Der `{"error": ...}`-Teil später zurückgebaut, die Prüfung selbst blieb.* |

## Testen

Änderungen am MCP-Code brauchen einen echten Neustart des Serverprozesses —
die laufende Instanz hält den alten Code im Speicher.

Beobachtet am 2026-09-01: **Disable/Enable allein hat den Prozess nicht
ersetzt** (Prozessliste zeigte keinen neuen Prozess, das alte Verhalten blieb).
Wirksam waren der **"Reload"-Button** unter *Customize → MCPs* und ein
vollständiger Cursor-Neustart (Cmd+Q). Kontrolle im Zweifel über die
Prozess-Startzeit:

```bash
ps -eo pid,lstart,command | rg genesys-builder-mcp
```

Ist die Startzeit älter als der Commit, läuft noch der alte Code. Zwei
gleichzeitige Prozesspaare sind hier normal, kein Symptom.

Reproduktion nach dem Neustart, verändert nichts in der Org:

```
get_queue(queue="Diese Queue Gibt Es Nicht XYZ")
```

Erwartet: `No queue found matching '...'`. Kommt stattdessen ein blankes
`Error executing tool`, läuft noch der alte Prozess.
