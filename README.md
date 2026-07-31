# Wordfeud-oplosser

Een Nederlandse Wordfeud-oplosser met twee strikt gescheiden onderdelen:

1. `wordfeud_analyzer/vision.py` lost de geometrie lokaal en deterministisch op: waar het bord staat, welke vakken een tegel dragen en welke bonus elk vrij vak heeft. Iedere uitgesneden tegel krijgt een stabiel nummer dat rechtstreeks aan zijn lokaal gevonden bordvak gekoppeld is. Het beeldmodel leest de grote letter en de kleine puntwaarde als ondersteunende metadata. Een bezet bord wordt onafhankelijk in normale én omgekeerde tegelvolgorde gelezen; verschillen krijgen een derde, vergrote beslissende lezing. Daardoor kan een overgeslagen glyph niet meer alle volgende letters ongemerkt naar een ander vak schuiven, terwijl een verkeerd gelezen piepkleine puntwaarde een correcte letter niet afkeurt.

   Tegel- en bonusherkenning ijken zichzelf per schermafbeelding op de meest voorkomende celkleur, en bonusvakken worden op tint geclassificeerd. Daardoor werken het donkere en het lichte thema via hetzelfde codepad, en zit er nergens een vaste bordindeling in.
2. `wordfeud_analyzer/move_generator.py` gebruikt een compacte, geminimaliseerde GADDAG met anker-vakken en kruiswoordchecks. Hij genereert legale zetten en berekent punten, bonussen, blanco's en de 40-punten-bingo lokaal.

## Woorden die op het bord liggen, worden geleerd

## Interactief werken

De startpagina opent met een leeg standaard-Wordfeudbord van 15×15 vakken. Je kunt het bord en het rek handmatig invullen, of een schermafbeelding inladen. Een succesvolle inlezing vervangt in één keer het bord, de effectieve bonusvakken, de rekletters, blanco-toewijzingen en de zekerheidsmetadata. Bij een afgewezen inlezing blijft de huidige stand behouden.

Klik op `Geef oplossingen weer` om maximaal zes zetten te laten berekenen. De Python-oplosser blijft daarbij leidend voor geldigheid, kruiswoorden en punten. De eerste suggestie wordt groen als voorbeeld op het bord gelegd; kies een andere suggestie om het voorbeeld te wisselen. `Annuleren` verlaat het voorbeeld zonder de stand te wijzigen. `Zet plaatsen` past uitsluitend een nog geldige suggestie toe en verbruikt de gebruikte rekletters precies één keer, inclusief blanco's.

Het rek mag leeg zijn, maar `Geef oplossingen weer` blijft dan uitgeschakeld met een uitleg. Handmatig ingevoerde letters zijn gewone tegels; blanco's die uit een schermafbeelding komen behouden hun blanco-status. Tijdens een voorbeeld zijn bord, rek en handelingen die de opslag wijzigen vergrendeld. De verborgen invoer houdt typen en mobiele schermtoetsenborden bruikbaar; pijltjes verplaatsen de selectie, een tweede klik wisselt horizontaal/verticaal en de terugtoets gaat terug en wist.

Opgeslagen spellen worden versie `v1` in de browseropslag (`localStorage`) bewaard. De eerste `Opslaan als…` vereist een niet-lege, hoofdletterongevoelig unieke naam; daarna werkt `Opslaan` het gekoppelde spel bij en maakt `Naam wijzigen` de naamswijziging direct blijvend. `Laden` herstelt de volledige stand. Corruptie of een onbekende opslagversie wordt overgeslagen met een waarschuwing. Handmatige bordwijzigingen worden na 3 seconden inactiviteit automatisch opgeslagen wanneer er al een gekoppeld spel bestaat; zonder bestaand spel wordt niets aangemaakt. Een succesvolle `Zet plaatsen` werkt een gekoppeld spel ook direct bij. Het inladen van een schermafbeelding sluit de actieve spelopslag eerst, zodat de nieuwe stand niet per ongeluk in het oude spel wordt opgeslagen.

De bestaande afwijzingen van schermafbeeldingen, zekerheidscontrole, geleerde woorden, woordenlijstbeheer en suggestievervanging blijven beschikbaar. Je kunt daarbij zowel een hoofdvoorstel als een kruiswoord van een huidige suggestie uitsluiten. Een nieuw bord en het opnieuw inladen van een schermafbeelding wissen de actieve spelopslag; de nieuwe stand wordt daardoor als onopgeslagen bord behandeld.

Wordfeud laat een speler alleen een zet indienen die zijn eigen woordenboek accepteert. Wat er op het bord ligt is dus per definitie geldig, ook als OpenTaal het niet kent. Na iedere uitlezing worden zulke woorden toegevoegd aan `data/geleerde-woorden.txt` en meteen meegenomen in de suggesties van diezelfde beurt.

Eén uitzondering: staat er een zet klaar die nog niet gespeeld is, dan heeft Wordfeud die woorden nog niet goedgekeurd. Zo'n schermafbeelding wordt geweigerd vóór er een model aan te pas komt, zodat er ook niets van geleerd wordt.

Het betrouwbaarste kenmerk is de knoppenbalk: zolang een zet klaarligt staat daar een gevulde blauwe **Speel**-knop in plaats van het neutrale Pas/Hussel. Dat werkt ook wanneer de tegels ongeldig liggen, want dan toont Wordfeud helemaal geen punten. Ligt de zet wél geldig, dan verschijnt daarnaast het gele puntenbolletje op het bord; ook dat wordt herkend, waar het ook ligt en welk getal er ook in staat.

Los daarvan geldt een stelling over het bord zelf: in een geldige stand hangen alle tegels aaneen met het middenvak. Tegels die daar niet aan vastzitten zijn onmogelijk — ook als ze een bestaand woord vormen — en leiden eveneens tot weigering. Dat vangt bovendien een schermafbeelding af waarvan de knoppen zijn weggesneden.

OpenTaal-woorden met diacritieken worden gevouwen in plaats van weggegooid: `façade` wordt `FACADE`, `abituriënt` wordt `ABITURIENT`. Dat scheelt ruim drieduizend woorden die eerder volledig ontbraken.

De app toont naast de beste zet vijf alternatieven. Selecteer een suggestie om het voorbeeld op het interactieve bord te wisselen; alleen de nieuwe stenen zijn groen gemarkeerd.

Onder de suggesties kun je een voorgesteld woord of bijbehorend kruiswoord insturen om het permanent uit de geconfigureerde woordenlijst te verwijderen. De app bouwt de woordenlijstcache opnieuw op en vult de suggesties daarna aan met de eerstvolgende legale zet. Staat het woord ook in de lijst met geleerde bordwoorden, dan wordt die kopie eveneens verwijderd.

Na het inladen van een schermafbeelding wordt niet automatisch een zet geplaatst: controleer eerst het zichtbare bord en kies daarna bewust `Geef oplossingen weer`. Het beeldmodel geeft bij iedere uitlezing een eigen zekerheidspercentage mee; onder de 90% wordt het resultaat niet gebruikt en vraagt de app om een betere schermafbeelding. Boven die grens staat het gerapporteerde percentage bij het uitgelezen bord.

De uitlezing controleert drie harde eigenschappen voordat een bord wordt vervangen: alle lokaal gevonden tegel-ID's zijn exact eenmaal aanwezig, de normale en omgekeerde letterlezing zijn gelijk en iedere gebruikte lezing haalt de zekerheidsgrens. De kleine puntwaarde blijft ondersteunende OCR-informatie, maar is door zijn formaat geen afwijzingsgrond. Bij een letterverschil leest het model alleen de betwiste tegels opnieuw op groter formaat en geldt een tweederdemeerderheid. Blijft er onenigheid, dan wordt de upload geweigerd in plaats van een mogelijk verschoven bord te tonen. Losse tegels zonder buur kan Wordfeud niet produceren; die worden als herkenningsfout gemeld vóór er een model aan te pas komt.

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

Gebruik in de zijbalk `google/gemini-2.5-flash` (standaard) of een ander beeldmodel dat het OpenAI-compatibele chat-eindpunt van OpenRouter en JSON-schema-uitvoer ondersteunt.

## Woordenlijst

De lokale installatie gebruikt `data/opentaal-wordlist.txt` zodra dit bestand aanwezig is; anders valt hij terug op de kleine demolijst. Zet vóór werkelijk spelgebruik de `wordlist.txt` van [OpenTaal](https://github.com/OpenTaal/opentaal-wordlist) lokaal klaar of laad hem in met:

```bash
curl -fL https://raw.githubusercontent.com/OpenTaal/opentaal-wordlist/master/wordlist.txt -o data/opentaal-wordlist.txt
```

De geleerde woorden staan los van de bronlijst in `data/geleerde-woorden.txt`. Dat bestand staat niet in Git en wordt niet meegestuurd bij een uitrol: het hoort bij de server, net als de OpenTaal-lijst zelf. De rsync-uitrol sluit het uit van overdracht, en `--delete` verwijdert uitgesloten bestanden niet, dus het overleeft een uitrol. Draait de app als een gebruiker die niet in `data/` mag schrijven, zet dan `WORDFEUD_LEARNED_WORDS_PATH` naar een pad dat wél schrijfbaar is; lukt schrijven niet, dan blijven de suggesties gewoon kloppen maar wordt er niets onthouden.

De lijst is vrij beschikbaar onder voorwaarden; neem de licentie en bronvermelding van OpenTaal over wanneer je die verspreidt. De tool houdt uitsluitend kleine, alfabetische Nederlandse woorden van 2–15 letters over: nummers, leestekens, afkortingen en eigennamen worden uitgesloten. De eerste opbouw van de GADDAG kost lokaal circa een halve minuut voor de volledige lijst; binnen dezelfde draaiende Streamlit-app wordt hij gecachet.

## Privacy en betrouwbaarheid

- De schermafbeelding gaat alleen naar het gekozen beeldmodel. Bord, woordvalidatie en scores gaan niet naar een model.
- Zet de sleutel in een omgevingsvariabele of Streamlit secrets, nooit in Git. `.env` staat in `.gitignore`.
- De app gebruikt een bezet bord pas wanneer twee onafhankelijke tegelvolgordes overeenkomen (of een derde lezing het verschil beslist) en iedere gebruikte lezing minstens 90% zekerheid meldt. Blijf het getoonde bord vergelijken met je schermafbeelding voordat je een zet speelt: een verkeerd gelezen bonusvak geeft vanzelfsprekend een verkeerd puntenaantal.

## Testen

```bash
python -m pytest -q
node --test frontend/board.test.js
```

De Python-tests controleren onder andere de standaardbonusindeling, lege rekken, zettoepassing, rekverbruik, blanco's, verouderde oplossingsresultaten en atomische vervanging na het inladen. De JavaScript-tests controleren de bord- en rekbewerker, richting, wissen, voorbeeldvergrendeling en de opslagfuncties voor de browseropslag.
