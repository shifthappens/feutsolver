# Wordfeud Analyzer

Een Nederlandse Wordfeud-analyzer met twee strikt gescheiden onderdelen:

1. `wordfeud_analyzer/vision.py` lost de geometrie lokaal en deterministisch op: waar het bord staat, welke vakken een tegel dragen en welke bonus elk vrij vak heeft. Die tegels worden uitgesneden en in een vaste volgorde in één afbeelding gezet; het vision-model krijgt precies één vraag, namelijk welke letter op elke tegel staat. Het hoeft dus nooit rijen te tellen, een raster op te vullen of een coördinaat terug te geven, waardoor positiefouten per constructie niet kunnen ontstaan.

   Tegel- en bonusherkenning ijken zichzelf per screenshot op de meest voorkomende celkleur, en bonusvakken worden op tint geclassificeerd. Daardoor werken het donkere en het lichte thema via hetzelfde codepad, en zit er nergens een vaste bordindeling in.
2. `wordfeud_analyzer/move_generator.py` gebruikt een compacte, geminimaliseerde GADDAG met anker-vakken en kruiswoordchecks. Hij genereert legale zetten en berekent score, bonussen, blanco's en de 40-punten-bingo lokaal.

## Woorden die op het bord liggen, worden geleerd

## Interactief werken

De startpagina opent met een leeg standaard-Wordfeudbord van 15×15 vakken. Je kunt het bord en het rek handmatig invullen, of een screenshot uploaden. Een succesvolle upload vervangt in één keer het bord, de effectieve bonusvakken, de rekletters, blanco-toewijzingen en de confidence-metadata. Bij een afgewezen upload blijft de huidige stand behouden.

Klik op `Solve` om maximaal zes zetten te laten berekenen. De Python-solver blijft daarbij leidend voor geldigheid, kruiswoorden en score. De eerste suggestie wordt groen als voorbeeld op het bord gelegd; kies een andere suggestie om de preview te wisselen. `Cancel` verlaat de preview zonder de stand te wijzigen. `Place` past uitsluitend een nog geldige suggestie toe en verbruikt de gebruikte rekletters precies één keer, inclusief blanco's.

Het rek mag leeg zijn, maar `Solve` blijft dan uitgeschakeld met een uitleg. Handmatig ingevoerde letters zijn gewone tegels; blanco's die uit een screenshot komen behouden hun blank-status. Tijdens een preview zijn bord, rek en state-changing save-acties vergrendeld. De verborgen invoer houdt typen en mobiele schermtoetsenborden bruikbaar; pijltjes verplaatsen de selectie, een tweede klik wisselt horizontaal/verticaal en `Backspace` gaat terug en wist.

Saves worden versie `v1` in de browser-localStorage bewaard. De eerste `Save als…` vereist een niet-lege, hoofdletterongevoelig unieke naam; daarna werkt `Save` de gekoppelde save bij en maakt `Rename` de naamswijziging direct blijvend. `Load` herstelt de volledige stand. Corruptie of een onbekende opslagversie wordt overgeslagen met een waarschuwing. Bordwijzigingen, inclusief handmatige edits en uploads, worden na 3 seconden inactiviteit automatisch opgeslagen wanneer er al een gekoppelde save bestaat; zonder bestaande save wordt niets aangemaakt. Een succesvolle `Place` werkt een gekoppelde save ook direct bij.

De bestaande screenshot-afwijzingen, confidencecontrole, geleerde woorden, woordenlijstbeheer en suggestievervanging blijven beschikbaar. Je kunt daarbij zowel een hoofdvoorstel als een kruiswoord van een huidige suggestie uitsluiten. Een nieuw bord wist de actieve save-link; een upload behoudt die link, zodat een volgende succesvolle plaatsing nog steeds automatisch naar dezelfde save kan schrijven.

Wordfeud laat een speler alleen een zet indienen die zijn eigen woordenboek accepteert. Wat er op het bord ligt is dus per definitie geldig, ook als OpenTaal het niet kent. Na iedere uitlezing worden zulke woorden toegevoegd aan `data/geleerde-woorden.txt` en meteen meegenomen in de suggesties van diezelfde beurt.

Eén uitzondering: staat er een zet klaar die nog niet gespeeld is, dan heeft Wordfeud die woorden nog niet goedgekeurd. Zo'n screenshot wordt geweigerd vóór er een model aan te pas komt, zodat er ook niets van geleerd wordt.

Het betrouwbaarste kenmerk is de knoppenbalk: zolang een zet klaarligt staat daar een gevulde blauwe **Speel**-knop in plaats van het neutrale Pas/Hussel. Dat werkt ook wanneer de tegels ongeldig liggen, want dan toont Wordfeud helemaal geen score. Ligt de zet wél geldig, dan verschijnt daarnaast het gele scorebolletje op het bord; ook dat wordt herkend, waar het ook ligt en welk getal er ook in staat.

Los daarvan geldt een stelling over het bord zelf: in een geldige stand hangen alle tegels aaneen met het middenvak. Tegels die daar niet aan vastzitten zijn onmogelijk — ook als ze een bestaand woord vormen — en leiden eveneens tot weigering. Dat vangt bovendien een screenshot af waarvan de knoppen zijn weggesneden.

OpenTaal-woorden met diacritieken worden gevouwen in plaats van weggegooid: `façade` wordt `FACADE`, `abituriënt` wordt `ABITURIENT`. Dat scheelt ruim drieduizend woorden die eerder volledig ontbraken.

De app toont naast de beste zet vijf alternatieven. Selecteer een suggestie om de preview op het interactieve bord te wisselen; alleen de nieuwe stenen zijn groen gemarkeerd.

Onder de suggesties kun je een voorgesteld woord of bijbehorend kruiswoord insturen om het permanent uit de geconfigureerde woordenlijst te verwijderen. De app bouwt de woordenlijstcache opnieuw op en vult de suggesties daarna aan met de eerstvolgende legale zet. Staat het woord ook in de lijst met geleerde bordwoorden, dan wordt die kopie eveneens verwijderd.

Na een screenshot-upload wordt niet automatisch een zet geplaatst: controleer eerst het zichtbare bord en kies daarna bewust `Solve`. Het vision-model geeft bij iedere uitlezing een eigen zekerheidspercentage mee; onder de 90% wordt het resultaat niet gebruikt en vraagt de app om een betere screenshot. Boven die grens staat het gerapporteerde percentage bij het uitgelezen bord.

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
node --test frontend/board.test.js
```

De Python-tests controleren onder andere de standaardbonusindeling, lege rekken, zettoepassing, rackverbruik, blanco's, stale solve-resultaten en atomische uploadvervanging. De Node-tests controleren de bord- en rack-reducer, richting, wissen, preview-locks en localStorage CRUD.
