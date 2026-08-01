# OCR-incident en productie-uitrol — augustus 2026

## Aanleiding

Bij hetzelfde Wordfeud-screenshot las de lokale OCR een tegel eerst als `O` en
later als `E`, terwijl de tegel visueel drie punten toont. Een onafhankelijke
controle kon de letter evenmin betrouwbaar bevestigen. De lokale tests leken
wel correct, maar dat bewees niet dat de productie-app exact dezelfde
herkenningsroute en hetzelfde font gebruikte.

De fout zat dus niet alleen in één letterprofiel. De testopstelling en de
productieherkenning konden van elkaar afwijken, terwijl de app een onbetrouwbare
uitkomst toch als vrijwel zeker presenteerde.

## Vastgestelde oorzaken

- De oude lokale herkenning maakte templates met een systeemfont. macOS, de
  ontwikkelomgeving en de Linux-productieserver konden daardoor verschillende
  glyphs renderen.
- De vaste confidencewaarde van 98% maskeerde echte onzekerheid.
- De kleine puntwaarde op een tegel werd als tweede bevestiging gebruikt. Die
  informatie is visueel zwak en kan een verkeerde grote letter juist
  bevestigen.
- De offline regressietest controleerde vooral de ontwikkelmachine. Daarmee was
  niet bewezen dat de online Streamlit-route host-onafhankelijk was.

## Duurzame oplossing

De OCR is daarom opnieuw ingericht in `wordfeud_analyzer/vision.py`:

1. De herkenning gebruikt ingecheckte Wordfeud-glyphprofielen van 24×32 pixels
   in plaats van lokaal geïnstalleerde fonts. De profielen zijn op exacte
   bloblengte gevalideerd en zijn daarmee gelijk op macOS, Linux en productie.
2. De confidence wordt berekend uit de gemeten glyphafstand; er wordt geen
   vaste 98% meer teruggegeven.
3. De puntwaarde is alleen een zwak signaal. Bij een conflict mag alleen een
   uitzonderlijk sterke grote-glyphmatch de puntenlezing corrigeren. Een
   middelmatige of tegenstrijdige match faalt gesloten en wordt geweigerd.
4. De puntenwaarde wordt niet doorgestuurd naar de remote OCR en hoort niet in
   het bordmodel. De letterbeslissing blijft daardoor één duidelijke,
   controleerbare verantwoordelijkheid.
5. De regressietest gebruikt het echte aangeleverde screenshot en controleert
   bord, rek en onzekerheid. De test slaagt ook wanneer tijdelijk een ander
   systeemfont wordt opgegeven; dat maakt afhankelijkheid van host-fonts
   zichtbaar.

De duurzame reviewprocedure staat in
[`ocr-review-workflow.md`](ocr-review-workflow.md). Die procedure vereist
reproductie met het echte screenshot, expliciete tests van twijfelgevallen en
een onafhankelijke review. Voor deze wijziging is de afgesproken Claude-review
uitgevoerd: maximaal zes beurten, de eerste twee met high effort en daarna
medium effort. De laatste review gaf `APPROVED`.

## Validatie

De echte screenshot-regressie gaf het verwachte bord en rek terug. De gemeten
lokale confidence was 62,6%; de app presenteert zo'n resultaat dus niet meer
als zekere herkenning maar toont een waarschuwing onder 80%.

Daarna waren alle projectcontroles groen:

```text
.venv/bin/python -m pytest -q       77 passed
node --test frontend/board.test.js  12 passed
git diff --check                    clean
```

## Git-acties

De wijziging is vastgelegd als:

```text
commit b2928f9 Make local OCR deterministic across hosts
```

De commit is gepusht van `main` naar `origin/main` op
`git@github.com:shifthappens/feutsolver.git`. Tijdens het stagen liep de
gesandboxte Git-actie tegen `Operation not permitted` op voor
`.git/index.lock`; met de expliciet toegestane elevated Git-actie kon de
wijziging veilig worden gestaged en gecommit. Er is geen reset of andere
destructieve Git-actie gebruikt.

## Productie-deployment

De workflow staat bewust op `workflow_dispatch`; een push naar `main` deployt
niet automatisch. Voor de OCR-wijziging is daarom handmatig gestart:

```bash
gh workflow run deploy.yml --repo shifthappens/feutsolver --ref main
gh run watch 30721873503 --repo shifthappens/feutsolver --exit-status
```

Run `30721873503` is succesvol geëindigd voor de exacte head commit
`b2928f91efa57f18a6e7f1b21b2e6268af146864`. De workflow heeft achtereenvolgens:

- de releasecommit uitgecheckt;
- Python 3.12 ingericht en de applicatie-tests uitgevoerd;
- de beperkte deploy-key en gepinde server-hostkey gebruikt;
- uitsluitend de allowlisted applicatiebestanden via rsync gesynchroniseerd;
- de service-leesbare groepsrechten gecontroleerd.

De server bewaakt het deploypad met `feutsolver.path`; die watcher start de
restart-service nadat rsync klaar is. GitHub Actions heeft daardoor geen
sudo- of restart-secret nodig. Deze documentatiewijziging wordt nu alleen
gecommit en gepusht; de hierboven genoemde productie-deploy wordt niet opnieuw
gestart.

## Herhaalbare werkwijze voor volgende incidenten

1. Bewaar het exacte screenshot en reproduceer eerst de online fout lokaal.
2. Controleer welke backend, commit, Python-versie en templatebron werkelijk
   actief zijn.
3. Gebruik geen host-afhankelijke fonts of vaste confidencewaarden.
4. Laat twijfelgevallen falen; laat een zwakke puntwaarde nooit een grote
   glyph bevestigen.
5. Voeg een screenshot-regressie toe en test minimaal één andere host/font-
   configuratie.
6. Laat de afgesproken onafhankelijke review de uitkomst controleren.
7. Test, commit en push; start daarna alleen een handmatige deploy en volg de
   volledige Actions-run tot `success`.
