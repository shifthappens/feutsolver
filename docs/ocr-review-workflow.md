# OCR-wijzigingen reviewen

Deze workflow bewaakt dat een lokale OCR-resultaat op een ontwikkelmachine niet
ten onrechte als productieresultaat geldt.

1. Reproduceer eerst met de originele screenshot en leg het verwachte bord en rek
   expliciet vast. Een herkende letter is geen bewijs wanneer de tegelpunten ermee
   botsen.
2. Meet de OCR op minimaal twee omgevingen of configuraties. Geen herkenningspad
   mag van geïnstalleerde fonts, fontvolgorde of een vaste zekerheidswaarde afhangen.
3. Bewaar geanonimiseerde, genormaliseerde glyphprofielen als regressiefixtures;
   valideer hun vorm en lengte bij laden. Voeg een tweede profiel toe als dezelfde
   Wordfeud-letter legitiem op een andere plek anders wordt gerenderd.
4. Behandel een klein puntsuperscript als aanvullende, zwakke informatie. Accepteer
   een conflict alleen bij een zeer sterke primaire glyphmatch; anders fail closed
   met een begrijpelijke melding. Combineer geen twee OCR-antwoorden zonder
   gekalibreerde betrouwbaarheid tot een schijnbaar onafhankelijke bevestiging.
5. Voeg een regressietest toe voor zowel het foutpaar als de productie-invariant
   (geen host-fontafhankelijkheid, gevalideerde fixtures en geen vaste zekerheid).
   Voer vervolgens `python -m pytest -q` en `node --test frontend/board.test.js` uit.
6. Laat Claude Opus de wijziging reviewen: maximaal zes rondes, de eerste twee met
   `--effort high` en daarna `--effort medium`. Geef per ronde de fout, de diff,
   testresultaten en open risico's mee. Alleen een expliciet `APPROVED` telt als
   goedkeuring; noteer anders het bezwaar en verwerk het in de volgende ronde.

Bij een nieuwe Wordfeud-clientweergave moet de wijziging een nieuw profiel en een
regressietest toevoegen, niet een machine-specifieke fontfallback.
