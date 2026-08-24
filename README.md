# Nadgryzieni — Archiwum Statystyk Odcinków

Interaktywny wykres i archiwum danych dotyczących długości odcinków podcastu [Nadgryzieni](https://retrorocketnetwork.pl/category/nadgryzieni-rss/) prowadzonego przez Wojciecha Pietrusiewicza (iMagazine) od 2010 roku.

## 📊 Wykres interaktywny

[Odwiedź wykres →](https://morid1n.github.io/nadgryzieni-episode-archive/)

Strona prezentuje interaktywny wykres punktowy pokazujący długość każdego odcinka w minutach w zależności od numeru odcinka. Pozwala na:

- **Przeglądanie** długości wszystkich odcinków od początku istnienia podcastu
- **Najechanie kursorem** na punkt w celu wyświetlenia szczegółów (tytuł, numer odcinka, data, długość)
- **Przewijanie** w poziomie, aby zobaczyć wszystkie odcinki
- **Wizualizacja linii średniej** — przerywana linia pokazująca średnią długość odcinków

## 📈 Statystyki

| Statystyka | Wartość |
|------------|---------|
| Liczba odcinków | 576 |
| Godziny odsłuchu | 987.4 |
| Średnia długość | 102.9 min |
| Maksymalna długość | 288.65 min |
| Afterparty | 11 |

## 🗂️ Struktura repozytorium

```
nadgryzieni-episode-archive/
├── index.html          # Strona główna z wykresem
├── style.css           # Style CSS
├── script.js           # Logika wykresu (Chart.js)
├── data.json           # Dane wszystkich odcinków (JSON)
├── host_metadata.json  # Audytowalny manifest osób przypisanych do odcinków i proweniencji
├── nadgryzieni_hosts.py # Parser RRN/Patreon, audyt i bezpieczne apply hostów
├── nadgryzieni_pipeline.py # Bezpieczny pipeline RSS/Patreon → archiwum/site
├── patreon_posts.json  # Zweryfikowany fallback linków Patreon
├── cron/                # Wrappery harmonogramu weekendowego i retry
├── archive/legacy/      # Ręczne narzędzia pełnej odbudowy (nieaktywne)
├── tests/               # Testy regresyjne pipeline'u
└── README.md           # Ten plik
```

## 📁 Dane

Dane odcinków są dostępne w formacie JSON:
- [data.json (surowe dane) →](https://morid1n.github.io/nadgryzieni-episode-archive/data.json)

Każdy odcinek zawiera: stabilny `record_key`, numer, tytuł, datę, długość w minutach, sformatowaną długość, kategorię, kanoniczny URL źródłowy oraz `hosts`, `hosts_status`, `hosts_source`, `hosts_source_url` i — gdy potrzebne — `hosts_provenance`. Lista `hosts` obejmuje także osoby jednoznacznie wskazane w opisie odcinka; nie są one publikowane jako osobny typ osoby. Odcinki Patreon Afterparty są pełnoprawnymi rekordami statystyk i zachowują ułamkowe identyfikatory, np. `595.5`.

`host_metadata.json` jest źródłem audytowalnej proweniencji. Strony RRN bez strukturalnego bloku `Prowadzący` są uzupełniane tylko wtedy, gdy opis jednoznacznie wskazuje konkretną osobę; sama wzmianka o osobie odwiedzającej audycję bez nazwiska pozostaje `not_listed` i ma widoczne `Brak danych`. Afterparty od numeru 550 wzwyż dziedziczą hostów głównego odcinka wyłącznie przez jawny wpis `paired_rrn` z kluczem sparowanego rekordu.

Polityka aliasów hostów jest trwała: historyczne formy aliasu Norberta są normalizowane i publikowane jako `NPC` w parserach, manifeście, `data.json`, Markdown oraz przyszłych aktualizacjach i odczytach cache.

## ⚙️ Automatyzacja

Aktywny pipeline jest uruchamiany z profilu R2-D2. Główny job Hermesa działa w sobotę o 04:00 czasu lokalnego, czyli w noc z piątku na sobotę. Przed uruchomieniem deterministycznego wrappera korzysta z narzędzi przeglądarki Hermesa do odczytu wyrenderowanych wpisów Patreon i rejestruje nowe metadane przez `cron/register-patreon-post.py`. Drugi wrapper działa we wtorek o tej samej porze, ale wykonuje pipeline wyłącznie wtedy, gdy sobotni przebieg nie znalazł nowych odcinków. Stan retry jest przechowywany poza repozytorium w chronionym pliku profilu.

`patreon_posts.json` jest śledzonym manifestem zweryfikowanych wpisów Afterparty. Może zawierać tytuł, datę i długość zebrane przez przeglądarkę, ponieważ Patreon blokuje zwykłe żądania HTTP. Dane uwierzytelniające nie są zapisywane w repozytorium.

Pipeline wymaga zweryfikowanego wygenerowanego outputu przed synchronizacją Obsidian i publikacją GitHub Pages. Gdy nie ma nowych odcinków, nie tworzy commita ani nie wykonuje pushu.

Jednorazowe odtworzenie hostów wykonuje się dwuetapowo — audyt nie zmienia repozytorium, a `apply` odrzuca niepełny lub nieaktualny audyt:

```bash
python3 nadgryzieni_hosts.py audit --output /tmp/hosts-audit.json
python3 nadgryzieni_hosts.py apply --audit /tmp/hosts-audit.json --dry-run
python3 nadgryzieni_hosts.py apply --audit /tmp/hosts-audit.json --write
```

Audyt używa resumowalnego cache wyników poza repozytorium (`~/.hermes/profiles/r2-d2/state/nadgryzieni-host-cache.json`); `--refresh` wymusza ponowne odczytanie źródeł. Cache nie przechowuje treści HTML ani danych uwierzytelniających.

Cotygodniowy pipeline wzbogaca tylko nowe rekordy; jawne odświeżenie całości wymaga `--refresh-hosts`. `--force` regeneruje artefakty bez masowego pobierania hostów.

## 🛠️ Technologie

- **Chart.js v4.4.1** — biblioteka do tworzenia wykresów
- **Chart.js Plugin Dashboard** — wtyczka do wizualizacji danych dashboardowych
- **Czysta HTML/CSS/JS** — brak frameworków, single-page application
- **GitHub Pages** — hosting strony

## 📡 Źródło danych

Dane pobierane są z feedu RSS podcastu Nadgryzieni:
- [Feed RSS →](https://retrorocketnetwork.pl/category/nadgryzieni-rss/feed/)
- [Strona Retro Rocket Network →](https://retrorocketnetwork.pl/)

## 🤝 Wkład

Repozytorium jest prowadzone przez R2-D2 (AI assistant) dla potrzeb podcastu Nadgryzieni i iMagazine.

Jeśli chcesz zaproponować zmiany lub zgłosić błąd, otwórz issue na GitHubie.

## 📜 Licencja

Dane i kod udostępnione na licencji MIT.

---

Stworzone z ❤️ przez R2-D2 | Dane z archiwum Nadgryzieni
