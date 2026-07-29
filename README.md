# Wordfeud Analyzer

Een Nederlandse Wordfeud-analyzer met twee strikt gescheiden onderdelen:

1. `wordfeud_analyzer/vision.py` lost de geometrie lokaal en deterministisch op: waar het bord staat, welke vakken een tegel dragen en welke bonus elk vrij vak heeft. Die tegels worden uitgesneden en in een vaste volgorde in één afbeelding gezet; het vision-model krijgt precies één vraag, namelijk welke letter op elke tegel staat. Het hoeft dus nooit rijen te tellen, een raster op te vullen of een coördinaat terug te geven, waardoor positiefouten per constructie niet kunnen ontstaan.

   Tegel- en bonusherkenning ijken zichzelf per screenshot op de meest voorkomende celkleur, en bonusvakken worden op tint geclassificeerd. Daardoor werken het donkere en het lichte thema via hetzelfde codepad, en zit er nergens een vaste bordindeling in.
2. `wordfeud_analyzer/move_generator.py` gebruikt een compacte, geminimaliseerde GADDAG met anker-vakken en kruiswoordchecks. Hij genereert legale zetten en berekent score, bonussen, blanco's en de 40-punten-bingo lokaal.

## Woorden die op het bord liggen, worden geleerd

Wordfeud laat een speler alleen een zet indienen die zijn eigen woordenboek accepteert. Wat er op het bord ligt is dus per definitie geldig, ook als OpenTaal het niet kent. Na iedere uitlezing worden zulke woorden toegevoegd aan `data/geleerde-woorden.txt` en meteen meegenomen in de suggesties van diezelfde beurt.

Eén uitzondering: staat er een zet klaar die nog niet gespeeld is, dan heeft Wordfeud die woorden nog niet goedgekeurd. Zo'n screenshot is te herkennen aan het gele scorebolletje op het bord en wordt geweigerd — vóór er een model aan te pas komt, zodat er ook niets van geleerd wordt.

OpenTaal-woorden met diacritieken worden gevouwen in plaats van weggegooid: `façade` wordt `FACADE`, `abituriënt` wordt `ABITURIENT`. Dat scheelt ruim drieduizend woorden die eerder volledig ontbraken.

De app toont naast de beste zet vijf alternatieven. Iedere suggestie krijgt een eigen bordweergave; alleen de nieuwe stenen zijn groen gemarkeerd.

Eén klik volstaat: na de vision-extractie rekent de app direct door en toont hij de top 6. Er is geen JSON-controlestap meer. Het vision-model geeft bij iedere uitlezing een eigen zekerheidspercentage mee; onder de 90% wordt het resultaat niet gebruikt en vraagt de app om een betere screenshot. Boven die grens staat het gerapporteerde percentage bij het uitgelezen bord.

Omdat wij de tegels zelf uitsnijden en op volgorde zetten, blijft er nog één foutmodus over: het model geeft een ander aantal letters terug dan er tegels zijn. Dat is één controle, en de retry noemt het verwachte aantal. Losse tegels zonder buur kan Wordfeud niet produceren; die worden als herkenningsfout gemeld vóór er een model aan te pas komt.

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

De geleerde woorden staan los van de bronlijst in `data/geleerde-woorden.txt`. Dat bestand staat niet in Git en wordt niet meegedeployed: het hoort bij de server, net als de OpenTaal-lijst zelf. De deploy-rsync sluit het uit van overdracht, en `--delete` verwijdert uitgesloten bestanden niet, dus het overleeft een deploy. Draait de app als een gebruiker die niet in `data/` mag schrijven, zet dan `WORDFEUD_LEARNED_WORDS_PATH` naar een pad dat wél schrijfbaar is; lukt schrijven niet, dan blijven de suggesties gewoon kloppen maar wordt er niets onthouden.

De lijst is vrij beschikbaar onder voorwaarden; neem de licentie en bronvermelding van OpenTaal over wanneer je die verspreidt. De tool houdt uitsluitend kleine, alfabetische Nederlandse woorden van 2–15 letters over: nummers, leestekens, afkortingen en eigennamen worden uitgesloten. De eerste opbouw van de GADDAG kost lokaal circa een halve minuut voor de volledige lijst; binnen dezelfde draaiende Streamlit-app wordt hij gecachet.

## Privacy en betrouwbaarheid

- De screenshot gaat alleen naar het gekozen vision-model. Bord, woordvalidatie en scores gaan niet naar een model.
- Zet de sleutel in een omgevingsvariabele of Streamlit secrets, nooit in Git. `.env` staat in `.gitignore`.
- De app vertrouwt op de uitlezing van het vision-model zodra dat zelf minstens 90% zeker is. Blijf het getoonde bord vergelijken met je screenshot voordat je een zet speelt: een verkeerd gelezen bonusvak geeft vanzelfsprekend een verkeerde score.

## Testen

```bash
python -m pytest -q
```
