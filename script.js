// ===== Nadgryzieni Statistics Script =====
// Uses Chart.js v4 to render episode duration data.

const DATA_VERSION = 110;
let chartInstance = null;
let chartData = null;
let normalizedEpisodes = [];
let chartMode = 'scatter';

function loadData() {
    fetch(`data.json?v=${DATA_VERSION}`)
        .then(response => {
            if (!response.ok) {
                throw new Error(`Data request failed: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            chartData = data;
            normalizedEpisodes = normalizeEpisodes(data.episodes);
            renderStats(data);
            updateModeControls();
            renderChart();
        })
        .catch(error => {
            console.error('Error loading data:', error);
            ['total-episodes', 'total-hours', 'avg-duration', 'max-duration']
                .forEach(id => {
                    document.getElementById(id).textContent = '—';
                });
            document.getElementById('chart-scroll').innerHTML =
                '<p class="chart-error" role="alert">Błąd ładowania danych. Spróbuj odświeżyć stronę.</p>';
        });
}

function renderStats(data) {
    document.getElementById('total-episodes').textContent = data.stats.total_episodes;
    document.getElementById('total-hours').textContent = data.stats.total_listening_hours;
    document.getElementById('avg-duration').textContent = data.stats.average_duration;
    document.getElementById('max-duration').textContent = Math.round(data.stats.max_duration);
}

function minutesToTime(minutes) {
    const totalSeconds = Math.max(0, Math.round(Number(minutes) * 60));
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    return h.toString().padStart(2, '0') + ':' +
        m.toString().padStart(2, '0') + ':' +
        s.toString().padStart(2, '0');
}

function formatDate(date) {
    if (!date) {
        return '';
    }

    const parsedDate = new Date(`${date}T00:00:00`);
    if (Number.isNaN(parsedDate.getTime())) {
        return date;
    }

    return new Intl.DateTimeFormat('pl-PL', {
        dateStyle: 'medium',
    }).format(parsedDate);
}

function normalizeEpisodes(episodes) {
    return episodes
        .map((ep, sourceIndex) => ({
            sourceIndex,
            episodeId: String(ep.episode ?? ''),
            numericEpisode: Number.isFinite(Number(ep.episode)) ? Number(ep.episode) : null,
            y: Number(ep.minutes),
            title: ep.title || 'Brak tytułu',
            date: ep.date || '',
            duration: ep.duration || '',
        }))
        .sort((a, b) => {
            const dateOrder = a.date.localeCompare(b.date);
            return dateOrder || a.sourceIndex - b.sourceIndex;
        })
        .map((episode, plotIndex) => ({
            ...episode,
            plotIndex,
        }));
}

function setChartLayout(isLineMode) {
    const scrollContainer = document.getElementById('chart-scroll');
    const chartWrapper = document.getElementById('chart-wrapper');

    chartWrapper.classList.toggle('line-mode', isLineMode);

    if (isLineMode) {
        // Ten pixels per episode keeps the line readable while ensuring that
        // the complete line chart is wider than the viewport.
        const minimumWidth = normalizedEpisodes.length * 10;
        chartWrapper.style.width = `${Math.max(scrollContainer.clientWidth, minimumWidth)}px`;
    } else {
        chartWrapper.style.width = '';
    }
}

function updateModeControls() {
    document.querySelectorAll('.chart-mode-button').forEach(button => {
        const isActive = button.dataset.chartMode === chartMode;
        button.classList.toggle('is-active', isActive);
        button.setAttribute('aria-pressed', String(isActive));
    });

    const hint = document.getElementById('chart-hint');
    if (chartMode === 'line') {
        hint.textContent = 'Wykres liniowy pokazuje kolejność wszystkich odcinków. Przewijaj w poziomie, aby zobaczyć pełną linię; najedź na punkt, aby zobaczyć szczegóły.';
    } else {
        hint.textContent = 'Wykres punktowy: najedź na punkt, aby zobaczyć szczegóły. Identyfikatory odcinków zachowują także wartości ułamkowe.';
    }
}

function episodeTickLabel(value) {
    const plotIndex = Math.round(Number(value));
    return normalizedEpisodes[plotIndex]?.episodeId || '';
}

function renderChart() {
    if (!chartData || !normalizedEpisodes.length) {
        return;
    }

    const isLineMode = chartMode === 'line';
    const canvas = document.getElementById('episode-chart');
    const ctx = canvas.getContext('2d');

    setChartLayout(isLineMode);

    if (chartInstance) {
        chartInstance.destroy();
    }

    chartInstance = new Chart(ctx, {
        type: isLineMode ? 'line' : 'scatter',
        data: {
            datasets: [{
                label: 'Długość odcinka',
                data: normalizedEpisodes.map(episode => ({
                    x: episode.plotIndex,
                    y: episode.y,
                    episodeId: episode.episodeId,
                    title: episode.title,
                    date: episode.date,
                    duration: episode.duration,
                })),
                backgroundColor: 'rgba(99, 47, 83, 0.7)',
                borderColor: '#632F53',
                borderWidth: isLineMode ? 2 : 1,
                tension: 0,
                pointRadius: isLineMode ? 2.5 : 4,
                pointHoverRadius: 7,
                pointHoverBorderWidth: 2,
                pointHoverBorderColor: '#fff',
                fill: false,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'nearest',
            },
            plugins: {
                legend: {
                    display: false,
                },
                tooltip: {
                    backgroundColor: 'rgba(255, 255, 255, 0.95)',
                    titleColor: '#111',
                    titleFont: { size: 14, weight: 600 },
                    bodyColor: '#29505F',
                    bodyFont: { size: 12 },
                    borderColor: '#F43E25',
                    borderWidth: 1,
                    padding: 12,
                    cornerRadius: 8,
                    displayColors: false,
                    callbacks: {
                        title: function(context) {
                            return 'Odcinek: ' + context[0].raw.episodeId;
                        },
                        afterTitle: function(context) {
                            return context[0].raw.title;
                        },
                        label: function(context) {
                            return 'Czas: ' + minutesToTime(context.raw.y);
                        },
                        afterLabel: function(context) {
                            const date = formatDate(context.raw.date);
                            return date ? 'Data: ' + date : '';
                        },
                    },
                },
            },
            scales: {
                x: {
                    type: 'linear',
                    position: 'bottom',
                    min: 0,
                    max: Math.max(0, normalizedEpisodes.length - 1),
                    grid: {
                        color: '#f0f0f0',
                        drawBorder: false,
                    },
                    ticks: {
                        color: '#29505F',
                        font: { size: 10 },
                        autoSkip: true,
                        maxTicksLimit: isLineMode ? 60 : 12,
                        callback: episodeTickLabel,
                    },
                    title: {
                        display: true,
                        text: 'Kolejność odcinków',
                        color: '#29505F',
                        font: { size: 13, weight: 600 },
                    },
                },
                y: {
                    grid: {
                        color: '#f0f0f0',
                        drawBorder: false,
                    },
                    ticks: {
                        color: '#29505F',
                        font: { size: 10 },
                    },
                    title: {
                        display: true,
                        text: 'Długość (minuty)',
                        color: '#29505F',
                        font: { size: 13, weight: 600 },
                    },
                    beginAtZero: true,
                },
            },
        },
        plugins: [
            {
                id: 'avgLine',
                afterDraw: function(chart) {
                    const chartContext = chart.ctx;
                    const yScale = chart.scales.y;
                    const avgY = yScale.getPixelForValue(chartData.average_duration);
                    chartContext.save();
                    chartContext.strokeStyle = '#29505F';
                    chartContext.lineWidth = 2;
                    chartContext.setLineDash([5, 5]);
                    chartContext.beginPath();
                    chartContext.moveTo(chart.scales.x.left, avgY);
                    chartContext.lineTo(chart.scales.x.right, avgY);
                    chartContext.stroke();
                    chartContext.setLineDash([]);
                    chartContext.fillStyle = '#29505F';
                    chartContext.font = 'italic 10px sans-serif';
                    chartContext.textAlign = 'left';
                    chartContext.fillText('Średnia: ' + chartData.average_duration + ' min', chart.scales.x.left + 5, avgY - 5);
                    chartContext.restore();
                },
            },
        ],
    });
}

document.querySelectorAll('.chart-mode-button').forEach(button => {
    button.addEventListener('click', () => {
        chartMode = button.dataset.chartMode;
        updateModeControls();
        renderChart();
    });
});

window.addEventListener('resize', () => {
    if (chartMode === 'line' && normalizedEpisodes.length) {
        setChartLayout(true);
        chartInstance?.resize();
    }
});

updateModeControls();
loadData();
