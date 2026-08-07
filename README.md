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
| Liczba odcinków | 572 |
| Godziny odsłuchu | 981.6 |
| Średnia długość | 103.0 min |
| Maksymalna długość | 288.65 min |
| Afterparty | 9 |

## 🗂️ Struktura repozytorium

```
nadgryzieni-episode-archive/
├── index.html          # Strona główna z wykresem
├── style.css           # Style CSS
├── script.js           # Logika wykresu (Chart.js)
├── data.json           # Dane wszystkich odcinków (JSON)
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

Każdy odcinek zawiera: numer, tytuł, datę, długość w minutach, sformatowaną długość, kategorię i kanoniczny URL źródłowy. Odcinki Patreon Afterparty są pełnoprawnymi rekordami statystyk i zachowują ułamkowe identyfikatory, np. `595.5`.

## ⚙️ Automatyzacja

Aktywny pipeline jest uruchamiany z profilu R2-D2. Główny job Hermesa działa w sobotę o 04:00 czasu lokalnego, czyli w noc z piątku na sobotę. Przed uruchomieniem deterministycznego wrappera korzysta z narzędzi przeglądarki Hermesa do odczytu wyrenderowanych wpisów Patreon i rejestruje nowe metadane przez `cron/register-patreon-post.py`. Drugi wrapper działa we wtorek o tej samej porze, ale wykonuje pipeline wyłącznie wtedy, gdy sobotni przebieg nie znalazł nowych odcinków. Stan retry jest przechowywany poza repozytorium w chronionym pliku profilu.

`patreon_posts.json` jest śledzonym manifestem zweryfikowanych wpisów Afterparty. Może zawierać tytuł, datę i długość zebrane przez przeglądarkę, ponieważ Patreon blokuje zwykłe żądania HTTP. Dane uwierzytelniające nie są zapisywane w repozytorium.

Pipeline wymaga zweryfikowanego wygenerowanego outputu przed synchronizacją Obsidian i publikacją GitHub Pages. Gdy nie ma nowych odcinków, nie tworzy commita ani nie wykonuje pushu.

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
