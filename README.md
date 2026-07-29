# Wordfeud Analyzer

Een Nederlandse Wordfeud-analyzer met twee strikt gescheiden onderdelen:

1. `wordfeud_analyzer/vision.py` stuurt uitsluitend vergrote uitsneden van bord en rack naar een vision-model en valideert de 15×15 JSON-uitvoer met Pydantic. De zichtbare bonusvakken worden daarnaast lokaal op hun Wordfeud-kleur per cel herkend; er is geen vaste bordindeling.
2. `wordfeud_analyzer/move_generator.py` gebruikt een compacte, geminimaliseerde GADDAG met anker-vakken en kruiswoordchecks. Hij genereert legale zetten en berekent score, bonussen, blanco's en de 40-punten-bingo lokaal.

De app toont naast de beste zet vijf alternatieven. Iedere suggestie krijgt een eigen bordweergave; alleen de nieuwe stenen zijn groen gemarkeerd.

Eén klik volstaat: na de vision-extractie rekent de app direct door en toont hij de top 6. Er is geen JSON-controlestap meer. Het vision-model geeft bij iedere uitlezing een eigen zekerheidspercentage mee; onder de 90% wordt het resultaat niet gebruikt en vraagt de app om een betere screenshot. Boven die grens staat het gerapporteerde percentage bij het uitgelezen bord. Het algoritme gebruikt uitsluitend de uitgelezen coördinaten: er zit geen standaardbord-layout in de scoreberekening.

## Starten

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENROUTER_API_KEY='...'
streamlit run app.py
```

Een blijvende, lokale optie is `.streamlit/secrets.toml` (dit bestand staat in `.gitignore`):

```toml
OPENROUTER_API_KEY = "jouw-sleutel"
OPENROUTER_VISION_MODEL = "google/gemini-2.5-flash"
```

De app leest eerst `OPENROUTER_API_KEY` uit de omgeving en daarna dit secrets-bestand. Deel de sleutel niet in chat, commit hem niet en plak hem alleen eventueel in het afgeschermde wachtwoordveld van je lokaal draaiende app.

Gebruik in de zijbalk `google/gemini-2.5-flash` (standaard) of een ander vision-model dat OpenRouter's OpenAI-compatibele chat endpoint en JSON-schema-uitvoer ondersteunt.

## Woordenlijst

De lokale installatie gebruikt `data/opentaal-wordlist.txt` zodra dit bestand aanwezig is; anders valt hij terug op de kleine demo-lijst. Upload vóór werkelijk spelgebruik de `wordlist.txt` van [OpenTaal](https://github.com/OpenTaal/opentaal-wordlist) of download hem lokaal met:

```bash
curl -fL https://raw.githubusercontent.com/OpenTaal/opentaal-wordlist/master/wordlist.txt -o data/opentaal-wordlist.txt
```

De lijst is vrij beschikbaar onder voorwaarden; neem de licentie en bronvermelding van OpenTaal over wanneer je die verspreidt. De tool houdt uitsluitend kleine, alfabetische Nederlandse woorden van 2–15 letters over: nummers, leestekens, afkortingen en eigennamen worden uitgesloten. De eerste opbouw van de GADDAG kost lokaal circa een halve minuut voor de volledige lijst; binnen dezelfde draaiende Streamlit-app wordt hij gecachet.

## Privacy en betrouwbaarheid

- De screenshot gaat alleen naar het gekozen vision-model. Bord, woordvalidatie en scores gaan niet naar een model.
- Zet de sleutel in een omgevingsvariabele of Streamlit secrets, nooit in Git. `.env` staat in `.gitignore`.
- De app vertrouwt op de uitlezing van het vision-model zodra dat zelf minstens 90% zeker is. Blijf het getoonde bord vergelijken met je screenshot voordat je een zet speelt: een verkeerd gelezen bonusvak geeft vanzelfsprekend een verkeerde score.

## Testen

```bash
python -m pytest -q
```
