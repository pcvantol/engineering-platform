# Verplaatsen van installatie-opslag

De Operations Console kan de platformdatabase en de File Inbox naar een door de
operator gekozen lokale map verplaatsen. Deze actie is uitsluitend
installatiebreed; een geselecteerd project heeft geen invloed op de bestemming.

## Veilig verloop

1. De Console controleert dat de gekozen map lokaal, absoluut en beschrijfbaar
   is. Voor de File Inbox moet de inbox leeg zijn; anders blijft de actie
   geblokkeerd.
2. De Server schrijft een beperkte, atomische verhuisopdracht naar zijn eigen
   runtime-map en antwoordt aan de Console.
3. Daarna stopt de LaunchAgent de Server. Daarmee stoppen de lifecycle-worker,
   inboxverwerking en andere schrijvende onderdelen voordat bestanden worden
   gewijzigd.
4. Bij de volgende start voert de Server de opdracht uit, vóór hij workers
   start. Een database wordt met SQLite's backup-API gekopieerd en met
   `integrity_check` gevalideerd. De vertrouwde installatiepaden worden daarna
   atomair omgezet naar een lokale verwijzing naar de gekozen bestemming.
5. De succesvolle wijziging wordt als configuratie-auditgebeurtenis in de
   platformdatabase vastgelegd. De Console laadt vervolgens de nieuwe locatie.

Een crash of herstart tussen stap 2 en 4 verliest de opdracht niet: de opdracht
blijft staan en wordt op een volgende schone start opnieuw veilig verwerkt.

## Herstel

Als de gekozen bestemming tijdens de herstart niet meer bruikbaar is, blijft de
opdracht staan en wordt er niet stilzwijgend naar een andere locatie
geschreven. Herstel de rechten of bestemming en herstart de Engineering
Platform Server. Gebruik geen handmatige bestandsverplaatsing terwijl de
Server draait.
