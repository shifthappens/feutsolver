# Wordfeud-oplosser

Een Nederlandse Wordfeud-oplosser met twee strikt gescheiden onderdelen:

1. `wordfeud_analyzer/vision.py` lost de geometrie en letterherkenning lokaal en deterministisch op: waar het bord staat, welke vakken een tegel dragen, welke bonus elk vrij vak heeft en welke glyph op iedere tegel staat. De normale route heeft geen AI of netwerk nodig; hij gebruikt snelle glyph-templates en Tesseract als draagbare fallback. OpenRouter blijft beschikbaar als expliciete fallback via `WORDFEUD_OCR_BACKEND=auto`.

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

Eén uitzondering: staat er een zet klaar die nog niet gespeeld is, dan heeft Wordfeud die woorden nog niet goedgekeurd. Zo'n schermafbeelding wordt geweigerd vóór er OCR aan te pas komt, zodat er ook niets van geleerd wordt.

Het betrouwbaarste kenmerk is de knoppenbalk: zolang een zet klaarligt staat daar een gevulde blauwe **Speel**-knop in plaats van het neutrale Pas/Hussel. Dat werkt ook wanneer de tegels ongeldig liggen, want dan toont Wordfeud helemaal geen punten. Ligt de zet wél geldig, dan verschijnt daarnaast het gele puntenbolletje op het bord; ook dat wordt herkend, waar het ook ligt en welk getal er ook in staat.

Los daarvan geldt een stelling over het bord zelf: in een geldige stand hangen alle tegels aaneen met het middenvak. Tegels die daar niet aan vastzitten zijn onmogelijk — ook als ze een bestaand woord vormen — en leiden eveneens tot weigering. Dat vangt bovendien een schermafbeelding af waarvan de knoppen zijn weggesneden.

OpenTaal-woorden met diacritieken worden gevouwen in plaats van weggegooid: `façade` wordt `FACADE`, `abituriënt` wordt `ABITURIENT`. Dat scheelt ruim drieduizend woorden die eerder volledig ontbraken.

De app toont naast de beste zet vijf alternatieven. Selecteer een suggestie om het voorbeeld op het interactieve bord te wisselen; alleen de nieuwe stenen zijn groen gemarkeerd.

Onder de suggesties kun je één of meer voorgestelde woorden of bijbehorende kruiswoorden, kommagescheiden, insturen om ze permanent uit de geconfigureerde woordenlijst te verwijderen. De app bouwt de woordenlijstcache opnieuw op en vult de suggesties daarna aan met de eerstvolgende legale zetten. Staat een woord ook in de lijst met geleerde bordwoorden, dan wordt die kopie eveneens verwijderd.

Na het inladen van een schermafbeelding wordt niet automatisch een zet geplaatst: controleer eerst het zichtbare bord en kies daarna bewust `Geef oplossingen weer`. De lokale route rapporteert gemeten zekerheid per uitlezing, niet langer een vaste waarde. Onder 80% toont de app expliciet een waarschuwing.

De lokale letterherkenning vergelijkt de grote tegelglyph met ingecheckte, Wordfeud-specifieke pixelprofielen. Die profielen zijn deel van de broncode en zijn dus identiek op macOS, Linux en de productieserver; er is geen afhankelijkheid van een lokaal systeemlettertype. Iedere profielblob wordt bij laden op de exacte verwachte lengte gecontroleerd. De zichtbare puntwaarde blijft slechts een zwak signaal. Bij een conflict mag alleen een zeer sterke grote-glyphmatch de puntlezing verwerpen; een matige match wordt afgewezen in plaats van door twee onzekere OCR-uitkomsten te laten bevestigen. De puntwaarde blijft buiten de remote OCR-API en het bordmodel. Kan lokale OCR een glyph niet betrouwbaar lezen, dan wordt de upload geweigerd; met `auto` kan daarna optioneel één OpenRouter-uitlezing volgen.

## Server/client-scheiding

De browser bevat uitsluitend de interactieve bordweergave, invoer en presentatie van serverresultaten. De upload gaat via Streamlit naar de Python-server. Daar worden de schermafbeelding, OCR, beeldbewerking en eventuele OpenRouter-aanroep uitgevoerd door `wordfeud_analyzer/vision.py`; `move_generator.py` leest de woordenlijsten en genereert en scoort de zetten. De woordenlijstbestanden en de GADDAG worden nooit als frontend-assets of browserdata meegestuurd.

De browser ontvangt alleen de gevalideerde bordstatus en de door de server berekende suggesties om ze te tonen. Iedere bordwijziging en iedere plaatsingsaanvraag wordt opnieuw server-side gevalideerd. De lokale opslag in de browser bevat alleen opgeslagen bordstanden, nooit OCR-code, solvercode of woordenlijstinhoud. `tests/test_client_server_boundary.py` bewaakt deze grens.

## Starten

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# macOS: brew install tesseract
# Debian/Ubuntu: sudo apt-get install tesseract-ocr
streamlit run app.py
```

De standaard is lokale OCR (`WORDFEUD_OCR_BACKEND=local`), waardoor een API-sleutel niet nodig is. De lokale route is ontworpen voor Wordfeud-screenshots en blijft ruim onder twee seconden op een normale laptop. Zet voor een optionele cloudfallback `WORDFEUD_OCR_BACKEND=auto`; `openrouter` forceert de oude AI-route.

Een blijvende, lokale optie is `.streamlit/secrets.toml` (dit bestand staat in `.gitignore`):

```toml
OPENROUTER_API_KEY = "jouw-sleutel"
OPENROUTER_VISION_MODEL = "openai/gpt-4.1-mini"
WORDFEUD_OCR_BACKEND = "local"
```

De app leest eerst `OPENROUTER_API_KEY` uit de omgeving en daarna dit secrets-bestand. Deel de sleutel niet in chat, commit hem niet en plak hem alleen eventueel in het afgeschermde wachtwoordveld van je lokaal draaiende app.

Gebruik alleen bij `auto` of `openrouter` in de zijbalk `openai/gpt-4.1-mini` (standaard) of een ander beeldmodel dat het OpenAI-compatibele chat-eindpunt van OpenRouter en JSON-schema-uitvoer ondersteunt.

## Productie-uitrol

De productie-uitrol wordt uitsluitend handmatig gestart via de GitHub Actions-workflow `Deploy Wordfeud Analyzer`. De workflow heeft alleen `workflow_dispatch`: een commit of push naar `main` start dus nooit een deploy. Op de server bewaakt de ingeschakelde systemd-unit `feutsolver.path` het deploypad; na een bestandswijziging start die `feutsolver-restart.service`, die na 20 seconden `feutsolver.service` herstart. Daarom heeft GitHub Actions geen restart-secret of sudo-rechten nodig.

De ondersteunde handmatige route is de GitHub CLI. Controleer eerst dat `gh auth status` een actieve GitHub-sessie toont en start vervolgens de workflow voor de gepushte `main`-branch:

```bash
gh workflow run deploy.yml --repo shifthappens/feutsolver --ref main
```

De opdracht geeft de URL van de nieuwe run terug. Volg die run tot hij klaar is met `gh run watch <run-id> --repo shifthappens/feutsolver --exit-status`; alleen een eindstatus `success` betekent dat de uitrol geslaagd is. Deze route is op 1 augustus 2026 succesvol uitgevoerd voor commit `029300d`.

Bewaar de SSH-sleutel, hostnaam/IP-adres, doelpad en gepinde hostkey alleen als GitHub-secrets of in de lokale SSH-configuratie. Zet ze niet in Git. Gebruik op de server de service `feutsolver.service`; controleer na een uitrol dat deze `active (running)` is.

## Woordenlijst

De lokale installatie gebruikt `data/opentaal-wordlist.txt` zodra dit bestand aanwezig is; anders valt hij terug op de kleine demolijst. Zet vóór werkelijk spelgebruik de `wordlist.txt` van [OpenTaal](https://github.com/OpenTaal/opentaal-wordlist) lokaal klaar of laad hem in met:

```bash
curl -fL https://raw.githubusercontent.com/OpenTaal/opentaal-wordlist/master/wordlist.txt -o data/opentaal-wordlist.txt
```

De geleerde woorden staan los van de bronlijst in `data/geleerde-woorden.txt`. Dat bestand staat niet in Git en wordt niet meegestuurd bij een uitrol: het hoort bij de server, net als de OpenTaal-lijst zelf. De rsync-uitrol sluit het uit van overdracht, en `--delete` verwijdert uitgesloten bestanden niet, dus het overleeft een uitrol. Draait de app als een gebruiker die niet in `data/` mag schrijven, zet dan `WORDFEUD_LEARNED_WORDS_PATH` naar een pad dat wél schrijfbaar is; lukt schrijven niet, dan blijven de suggesties gewoon kloppen maar wordt er niets onthouden.

De lijst is vrij beschikbaar onder voorwaarden; neem de licentie en bronvermelding van OpenTaal over wanneer je die verspreidt. De tool houdt uitsluitend kleine, alfabetische Nederlandse woorden van 2–15 letters over: nummers, leestekens, afkortingen en eigennamen worden uitgesloten. De eerste opbouw van de GADDAG kost lokaal circa een halve minuut voor de volledige lijst; binnen dezelfde draaiende Streamlit-app wordt hij gecachet.

## Privacy en betrouwbaarheid

- Bij de standaard lokale route verlaat de schermafbeelding de server niet. Alleen met `auto` na een lokale OCR-fout of met `openrouter` gaat het beeld naar het gekozen beeldmodel.
- Zet de sleutel in een omgevingsvariabele of Streamlit secrets, nooit in Git. `.env` staat in `.gitignore`.
- De app gebruikt een bezet bord pas wanneer de lokale geometrie en glyph-uitlezing volledig zijn. Blijf het getoonde bord vergelijken met je schermafbeelding voordat je een zet speelt: een verkeerd gelezen bonusvak geeft vanzelfsprekend een verkeerd puntenaantal.

## Testen

```bash
python -m pytest -q
node --test frontend/board.test.js
```

De Python-tests controleren onder andere de standaardbonusindeling, lege rekken, zettoepassing, rekverbruik, blanco's, verouderde oplossingsresultaten en atomische vervanging na het inladen. De JavaScript-tests controleren de bord- en rekbewerker, richting, wissen, voorbeeldvergrendeling en de opslagfuncties voor de browseropslag.
