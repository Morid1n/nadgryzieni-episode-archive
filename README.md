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
| Liczba odcinków | 564 |
| Godziny odsłuchu | 970.0 |
| Średnia długość | 103.2 min |
| Maksymalna długość | 288.7 min |

## 🗂️ Struktura repozytorium

```
nadgryzieni-episode-archive/
├── index.html          # Strona główna z wykresem
├── style.css           # Style CSS
├── script.js           # Logika wykresu (Chart.js)
├── data.json           # Dane wszystkich odcinków (JSON)
└── README.md           # Ten plik
```

## 📁 Dane

Dane odcinków są dostępne w formacie JSON:
- [data.json (surowe dane) →](https://morid1n.github.io/nadgryzieni-episode-archive/data.json)

Każdy odcinek zawiera: numer, tytuł, datę, długość w minutach oraz sformatowaną długość.

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

Repozytorium jest prowadzone przez C-3PO (AI assistant) dla potrzeb podcastu Nadgryzieni i iMagazine.

Jeśli chcesz zaproponować zmiany lub zgłosić błąd, otwórz issue na GitHubie.

## 📜 Licencja

Dane i kod udostępnione na licencji MIT.

---

Stworzone z ❤️ przez C-3PO | Dane z archiwum Nadgryzieni
