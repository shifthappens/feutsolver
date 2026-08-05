# Productie-VPS runbook

## Verbinding

De Feutsolver-productiehost is VPS `Andromeda` op `142.93.135.135`. Voor
serverbeheer mag rechtstreeks als `root` worden verbonden met de lokale
Andromeda-sleutel:

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -i /Users/coen/.ssh/Andromeda_ed25519 \
  root@142.93.135.135
```

De publieke sleutel heeft het commentaar `root@Andromeda`. Controleer bij
twijfel dus zowel de bestandsnaam als dat commentaar. De private sleutel is
alleen lokaal aanwezig en mag nooit worden gekopieerd naar deze repository,
een log, issue of chat.

Deze verbinding is op 5 augustus 2026 gecontroleerd: hostnaam `Andromeda`,
gebruiker `root`.

## Belangrijke productiepaden

- Applicatiebasis: `/var/www/html/domains/coen.at/public_html/feutsolver`
- Actieve release: bovenstaande basis met `/current`
- Systemd-service: `feutsolver.service`
- Apache-vhost: `/etc/apache2/sites-available/coen.at-le-ssl.conf`
- Omgevingsconfiguratie: `/etc/feutsolver/`

Gebruik voor gewone releases eerst de handmatige GitHub Actions-workflow uit
de README. Gebruik directe roottoegang voor hostconfiguratie, diagnose,
herstel en controles die niet door de deployworkflow worden beheerd. Maak
voor iedere materiële serverwijziging eerst een root-only herstelkopie,
valideer configuraties vóór reload/restart en controleer daarna ten minste de
service, listener op `127.0.0.1:8501` en de publieke loginroute.
