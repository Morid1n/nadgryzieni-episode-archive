// ===== Nadgryzieni / archive experience =====
// The data file remains the source of truth; presentation is layered on top.

const DATA_VERSION = 110;
const THEME_STORAGE_KEY = 'nadgryzieni-theme';
const PAGE_SIZE = 12;

let chartInstance = null;
let chartData = null;
let normalizedEpisodes = [];
let chartMode = 'scatter';
const archiveState = {
    query: '',
    year: 'all',
    limit: PAGE_SIZE,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const formatter = new Intl.NumberFormat('pl-PL', { maximumFractionDigits: 1 });
const integerFormatter = new Intl.NumberFormat('pl-PL', { maximumFractionDigits: 0 });
const dateFormatter = new Intl.DateTimeFormat('pl-PL', { dateStyle: 'medium' });

function setText(selector, value) {
    const element = $(selector);
    if (element) {
        element.textContent = value;
    }
}

function formatNumber(value, maximumFractionDigits = 1) {
    const number = Number(value);
    if (!Number.isFinite(number)) {
        return '—';
    }
    return new Intl.NumberFormat('pl-PL', { maximumFractionDigits }).format(number);
}

function minutesToTime(minutes) {
    const totalSeconds = Math.max(0, Math.round(Number(minutes) * 60));
    const hours = Math.floor(totalSeconds / 3600);
    const mins = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return `${hours.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}

function minutesLabel(minutes) {
    return `${formatNumber(minutes)} min`;
}

function formatDate(date) {
    if (!date) {
        return '—';
    }
    const parsedDate = new Date(`${date}T00:00:00`);
    return Number.isNaN(parsedDate.getTime()) ? date : dateFormatter.format(parsedDate);
}

function getEpisodeYear(episode) {
    return episode.date ? episode.date.slice(0, 4) : '—';
}

function getAverageDuration(data) {
    return Number(data.stats?.average_duration ?? data.average_duration ?? 0);
}

function getTheme() {
    try {
        const saved = localStorage.getItem(THEME_STORAGE_KEY);
        if (saved === 'dark' || saved === 'light') {
            return saved;
        }
    } catch (error) {
        // Private browsing can disable localStorage; system preference still works.
    }
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function applyTheme(theme, persist = true) {
    document.documentElement.dataset.theme = theme;
    const toggle = $('#theme-toggle');
    const label = $('#theme-label');
    if (toggle) {
        toggle.setAttribute('aria-pressed', String(theme === 'dark'));
        toggle.setAttribute('aria-label', theme === 'dark' ? 'Włącz tryb jasny' : 'Włącz tryb ciemny');
    }
    if (label) {
        label.textContent = theme === 'dark' ? 'Tryb jasny' : 'Tryb ciemny';
    }
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#1f1f2c' : '#fde6bd');
    if (persist) {
        try {
            localStorage.setItem(THEME_STORAGE_KEY, theme);
        } catch (error) {
            // Theme still applies for this visit when persistence is unavailable.
        }
    }
    if (chartInstance) {
        renderChart();
    }
}

function normalizeEpisodes(episodes) {
    return episodes
        .map((episode, sourceIndex) => ({
            sourceIndex,
            episodeId: String(episode.episode ?? ''),
            numericEpisode: Number.isFinite(Number(episode.episode)) ? Number(episode.episode) : null,
            y: Number(episode.minutes),
            title: episode.title || 'Brak tytułu',
            date: episode.date || '',
            duration: episode.duration || '',
        }))
        .filter((episode) => Number.isFinite(episode.y))
        .sort((a, b) => {
            const dateOrder = a.date.localeCompare(b.date);
            return dateOrder || a.sourceIndex - b.sourceIndex;
        })
        .map((episode, plotIndex) => ({ ...episode, plotIndex }));
}

function renderStats(data) {
    const stats = data.stats || {};
    const maxDuration = normalizedEpisodes.reduce((max, episode) => Math.max(max, episode.y), 0);
    setText('#total-episodes', formatNumber(stats.total_episodes ?? normalizedEpisodes.length, 0));
    setText('#total-hours', formatNumber(stats.total_listening_hours));
    setText('#avg-duration', formatNumber(stats.average_duration));
    setText('#max-duration', formatNumber(stats.max_duration ?? maxDuration, 0));
    setText('#chart-average', `${formatNumber(getAverageDuration(data))} min`);
    setText('#hero-episode-count', formatNumber(stats.total_episodes ?? normalizedEpisodes.length, 0));
    setText('#hero-last-date', formatDate(normalizedEpisodes.at(-1)?.date));
    setText('#footer-update', formatDate(normalizedEpisodes.at(-1)?.date));
}

function renderLatestEpisode() {
    const latest = normalizedEpisodes.at(-1);
    if (!latest) {
        return;
    }
    setText('#latest-episode-id', latest.episodeId);
    setText('#latest-title', latest.title);
    setText('#latest-date', formatDate(latest.date));
    setText('#latest-duration', latest.duration || minutesToTime(latest.y));

    const sparkline = $('#latest-sparkline');
    if (!sparkline) {
        return;
    }
    sparkline.replaceChildren();
    const recent = normalizedEpisodes.slice(-12);
    const max = Math.max(...recent.map((episode) => episode.y), 1);
    recent.forEach((episode) => {
        const bar = document.createElement('span');
        bar.style.height = `${Math.max(12, (episode.y / max) * 100)}%`;
        bar.title = `${episode.episodeId}: ${minutesLabel(episode.y)}`;
        sparkline.appendChild(bar);
    });
}

function renderSignals() {
    if (!normalizedEpisodes.length) {
        return;
    }
    const longest = normalizedEpisodes.reduce((best, episode) => episode.y > best.y ? episode : best, normalizedEpisodes[0]);
    const shortest = normalizedEpisodes.reduce((best, episode) => episode.y < best.y ? episode : best, normalizedEpisodes[0]);
    const countsByYear = normalizedEpisodes.reduce((counts, episode) => {
        const year = getEpisodeYear(episode);
        counts[year] = (counts[year] || 0) + 1;
        return counts;
    }, {});
    const busiest = Object.entries(countsByYear).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0];

    setText('#longest-duration', minutesLabel(longest.y));
    setText('#longest-title', `${longest.episodeId} · ${longest.title}`);
    setText('#longest-meta', `${formatDate(longest.date)} · ${longest.duration || minutesToTime(longest.y)}`);
    setText('#shortest-duration', minutesLabel(shortest.y));
    setText('#shortest-title', `${shortest.episodeId} · ${shortest.title}`);
    setText('#shortest-meta', `${formatDate(shortest.date)} · ${shortest.duration || minutesToTime(shortest.y)}`);
    setText('#busiest-year', busiest?.[0] || '—');
    setText('#busiest-year-count', busiest ? `${integerFormatter.format(busiest[1])} odcinków` : '—');
    setText('#chart-range', `${formatDate(normalizedEpisodes[0].date)} → ${formatDate(normalizedEpisodes.at(-1).date)}`);
}

function renderYearBars() {
    const container = $('#year-bars');
    if (!container) {
        return;
    }
    const counts = normalizedEpisodes.reduce((result, episode) => {
        const year = getEpisodeYear(episode);
        result[year] = (result[year] || 0) + 1;
        return result;
    }, {});
    const years = Object.keys(counts).sort();
    const max = Math.max(...Object.values(counts), 1);
    setText('#year-range', years.length ? `${years[0]}—${years.at(-1)}` : '—');
    setText('#year-axis-end', years.at(-1) || '—');
    container.replaceChildren();
    years.forEach((year) => {
        const bar = document.createElement('div');
        bar.className = 'year-bar';
        bar.title = `${year}: ${counts[year]} odcinków`;
        const fill = document.createElement('span');
        fill.className = 'year-bar-fill';
        fill.style.height = `${Math.max(4, (counts[year] / max) * 100)}%`;
        const label = document.createElement('span');
        label.className = 'year-bar-label';
        label.textContent = year;
        bar.append(fill, label);
        container.appendChild(bar);
    });
}

function renderDurationBars() {
    const container = $('#duration-bars');
    if (!container) {
        return;
    }
    const buckets = [
        { label: '0—60', min: 0, max: 60 },
        { label: '60—90', min: 60, max: 90 },
        { label: '90—120', min: 90, max: 120 },
        { label: '120+', min: 120, max: Infinity },
    ].map((bucket) => ({ ...bucket, count: normalizedEpisodes.filter((episode) => episode.y >= bucket.min && episode.y < bucket.max).length }));
    const max = Math.max(...buckets.map((bucket) => bucket.count), 1);
    container.replaceChildren();
    buckets.forEach((bucket) => {
        const row = document.createElement('div');
        row.className = 'duration-row';
        const label = document.createElement('span');
        label.className = 'duration-label';
        label.textContent = `${bucket.label} min`;
        const track = document.createElement('span');
        track.className = 'duration-track';
        const fill = document.createElement('span');
        fill.className = 'duration-fill';
        fill.style.width = `${Math.max(2, (bucket.count / max) * 100)}%`;
        const count = document.createElement('span');
        count.className = 'duration-count';
        count.textContent = integerFormatter.format(bucket.count);
        track.appendChild(fill);
        row.append(label, track, count);
        container.appendChild(row);
    });
}

function chartTokens() {
    const styles = getComputedStyle(document.documentElement);
    return {
        grid: styles.getPropertyValue('--chart-grid').trim(),
        axis: styles.getPropertyValue('--chart-axis').trim(),
        line: styles.getPropertyValue('--chart-line').trim(),
        point: styles.getPropertyValue('--chart-point').trim(),
        tooltipBackground: styles.getPropertyValue('--chart-tooltip-bg').trim(),
        tooltipText: styles.getPropertyValue('--chart-tooltip-text').trim(),
        teal: styles.getPropertyValue('--teal').trim(),
    };
}

function setChartLayout(isLineMode) {
    const scrollContainer = $('#chart-scroll');
    const chartWrapper = $('#chart-wrapper');
    if (!scrollContainer || !chartWrapper) {
        return;
    }
    chartWrapper.classList.toggle('line-mode', isLineMode);
    if (isLineMode) {
        const minimumWidth = normalizedEpisodes.length * 15;
        chartWrapper.style.width = `${Math.max(scrollContainer.clientWidth, minimumWidth)}px`;
    } else {
        chartWrapper.style.width = '';
    }
}

function episodeTickLabel(value) {
    const plotIndex = Math.round(Number(value));
    return normalizedEpisodes[plotIndex]?.episodeId || '';
}

function updateModeControls() {
    $$('.mode-button').forEach((button) => {
        const isActive = button.dataset.chartMode === chartMode;
        button.classList.toggle('is-active', isActive);
        button.setAttribute('aria-pressed', String(isActive));
    });
    setText('#chart-hint', chartMode === 'line'
        ? 'Wykres liniowy pokazuje kolejność wszystkich odcinków. Przewijaj w poziomie, aby zobaczyć pełną linię.'
        : 'Najedź na punkt, aby zobaczyć szczegóły odcinka. Identyfikatory zachowują wartości ułamkowe i specjalne.');
}

function renderChart() {
    if (!chartData || !normalizedEpisodes.length || typeof Chart === 'undefined') {
        return;
    }
    const canvas = $('#episode-chart');
    if (!canvas) {
        return;
    }
    const isLineMode = chartMode === 'line';
    const colors = chartTokens();
    setChartLayout(isLineMode);
    chartInstance?.destroy();

    Chart.defaults.font.family = 'Manrope, sans-serif';
    chartInstance = new Chart(canvas.getContext('2d'), {
        type: isLineMode ? 'line' : 'scatter',
        data: {
            datasets: [{
                label: 'Długość odcinka',
                data: normalizedEpisodes.map((episode) => ({
                    x: episode.plotIndex,
                    y: episode.y,
                    episodeId: episode.episodeId,
                    title: episode.title,
                    date: episode.date,
                    duration: episode.duration,
                })),
                backgroundColor: colors.point,
                borderColor: colors.line,
                borderWidth: isLineMode ? 2 : 1,
                tension: 0,
                pointRadius: isLineMode ? 2.8 : 4,
                pointHoverRadius: 7,
                pointHoverBorderWidth: 2,
                pointHoverBorderColor: colors.tooltipText,
                fill: false,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'nearest' },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: colors.tooltipBackground,
                    titleColor: colors.tooltipText,
                    bodyColor: colors.tooltipText,
                    borderColor: colors.line,
                    borderWidth: 1,
                    padding: 13,
                    cornerRadius: 10,
                    displayColors: false,
                    callbacks: {
                        title: (context) => `Odcinek: ${context[0].raw.episodeId}`,
                        afterTitle: (context) => context[0].raw.title,
                        label: (context) => `Czas: ${minutesToTime(context.raw.y)}`,
                        afterLabel: (context) => {
                            const date = formatDate(context.raw.date);
                            return date === '—' ? '' : `Data: ${date}`;
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
                    grid: { color: colors.grid, drawBorder: false },
                    ticks: {
                        color: colors.axis,
                        font: { size: 10 },
                        autoSkip: true,
                        maxTicksLimit: isLineMode ? 60 : 12,
                        callback: episodeTickLabel,
                    },
                    title: { display: true, text: 'Kolejność odcinków', color: colors.axis, font: { size: 12, weight: 600 } },
                },
                y: {
                    grid: { color: colors.grid, drawBorder: false },
                    ticks: { color: colors.axis, font: { size: 10 } },
                    title: { display: true, text: 'Długość (minuty)', color: colors.axis, font: { size: 12, weight: 600 } },
                    beginAtZero: true,
                },
            },
        },
        plugins: [{
            id: 'avgLine',
            afterDraw(chart) {
                const average = getAverageDuration(chartData);
                const yScale = chart.scales.y;
                const averageY = yScale.getPixelForValue(average);
                const context = chart.ctx;
                context.save();
                context.strokeStyle = colors.teal;
                context.lineWidth = 1.5;
                context.setLineDash([5, 5]);
                context.beginPath();
                context.moveTo(chart.scales.x.left, averageY);
                context.lineTo(chart.scales.x.right, averageY);
                context.stroke();
                context.setLineDash([]);
                context.fillStyle = colors.teal;
                context.font = '600 10px IBM Plex Mono, monospace';
                context.textAlign = 'left';
                context.fillText(`Średnia: ${formatNumber(average)} min`, chart.scales.x.left + 6, averageY - 7);
                context.restore();
            },
        }],
    });
}

function createEpisodeCard(episode) {
    const card = document.createElement('article');
    card.className = 'episode-card';
    const top = document.createElement('div');
    top.className = 'episode-card-top';
    const id = document.createElement('span');
    id.className = 'episode-id';
    id.textContent = episode.episodeId;
    const duration = document.createElement('span');
    duration.className = 'episode-duration';
    duration.textContent = episode.duration || minutesToTime(episode.y);
    top.append(id, duration);
    const title = document.createElement('h3');
    title.textContent = episode.title;
    const date = document.createElement('span');
    date.className = 'episode-date';
    date.textContent = formatDate(episode.date);
    card.append(top, title, date);
    return card;
}

function populateYearFilter() {
    const filter = $('#year-filter');
    if (!filter) {
        return;
    }
    const years = [...new Set(normalizedEpisodes.map(getEpisodeYear))].filter((year) => year !== '—').sort().reverse();
    years.forEach((year) => {
        const option = document.createElement('option');
        option.value = year;
        option.textContent = year;
        filter.appendChild(option);
    });
}

function getFilteredEpisodes() {
    const query = archiveState.query.toLocaleLowerCase('pl-PL');
    return normalizedEpisodes
        .slice()
        .reverse()
        .filter((episode) => {
            const matchesYear = archiveState.year === 'all' || getEpisodeYear(episode) === archiveState.year;
            const searchText = `${episode.episodeId} ${episode.title} ${episode.date}`.toLocaleLowerCase('pl-PL');
            return matchesYear && (!query || searchText.includes(query));
        });
}

function renderArchive() {
    const grid = $('#episode-grid');
    const empty = $('#archive-empty');
    const loadMore = $('#load-more');
    if (!grid || !empty || !loadMore) {
        return;
    }
    const filtered = getFilteredEpisodes();
    const visible = filtered.slice(0, archiveState.limit);
    grid.replaceChildren(...visible.map(createEpisodeCard));
    empty.hidden = filtered.length > 0;
    loadMore.hidden = filtered.length <= visible.length;
    setText('#archive-result-count', filtered.length === normalizedEpisodes.length
        ? `${integerFormatter.format(filtered.length)} odcinków w archiwum`
        : `Pokazano ${integerFormatter.format(visible.length)} z ${integerFormatter.format(filtered.length)} pasujących odcinków`);
    const hasFilters = Boolean(archiveState.query || archiveState.year !== 'all');
    $('#clear-filters').hidden = !hasFilters;
}

function bindInteractions() {
    $('#theme-toggle')?.addEventListener('click', () => {
        const nextTheme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        applyTheme(nextTheme);
    });

    $$('.mode-button').forEach((button) => {
        button.addEventListener('click', () => {
            chartMode = button.dataset.chartMode;
            updateModeControls();
            renderChart();
        });
    });

    $('#episode-search')?.addEventListener('input', (event) => {
        archiveState.query = event.target.value.trim();
        archiveState.limit = PAGE_SIZE;
        renderArchive();
    });

    $('#year-filter')?.addEventListener('change', (event) => {
        archiveState.year = event.target.value;
        archiveState.limit = PAGE_SIZE;
        renderArchive();
    });

    $('#clear-filters')?.addEventListener('click', () => {
        archiveState.query = '';
        archiveState.year = 'all';
        archiveState.limit = PAGE_SIZE;
        $('#episode-search').value = '';
        $('#year-filter').value = 'all';
        renderArchive();
    });

    $('#load-more')?.addEventListener('click', () => {
        archiveState.limit += PAGE_SIZE;
        renderArchive();
    });

    window.addEventListener('resize', () => {
        if (chartMode === 'line' && normalizedEpisodes.length) {
            setChartLayout(true);
            chartInstance?.resize();
        }
    });
}

async function loadData() {
    try {
        const response = await fetch(`data.json?v=${DATA_VERSION}`);
        if (!response.ok) {
            throw new Error(`Data request failed: ${response.status}`);
        }
        chartData = await response.json();
        normalizedEpisodes = normalizeEpisodes(chartData.episodes || []);
        renderStats(chartData);
        renderLatestEpisode();
        renderSignals();
        renderYearBars();
        renderDurationBars();
        populateYearFilter();
        renderArchive();
        updateModeControls();
        renderChart();
    } catch (error) {
        console.error('Error loading Nadgryzieni data:', error);
        ['#total-episodes', '#total-hours', '#avg-duration', '#max-duration'].forEach((selector) => setText(selector, '—'));
        const scroll = $('#chart-scroll');
        if (scroll) {
            scroll.innerHTML = '<p class="chart-error" role="alert">Nie udało się załadować danych. Spróbuj odświeżyć stronę.</p>';
        }
        setText('#archive-result-count', 'Archiwum jest chwilowo niedostępne');
    }
}

applyTheme(getTheme(), false);
bindInteractions();
loadData();
