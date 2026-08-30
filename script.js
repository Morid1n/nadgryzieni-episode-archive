// ===== Nadgryzieni / archive experience =====
// The data file remains the source of truth; presentation is layered on top.

const DATA_VERSION = 141;
const SYSTEM_THEME_QUERY = '(prefers-color-scheme: dark)';
const PAGE_SIZE = 12;
const YEARLY_STATS_START = 2021;
const CHART_EPISODE_SPACING = 22.5;

let chartInstance = null;
let chartData = null;
let rawEpisodes = [];
let normalizedEpisodes = [];
let chartMode = 'line';
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
const monthFormatter = new Intl.DateTimeFormat('pl-PL', { month: 'short' });

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

function renderUpcomingEvent(payload) {
    const upcomingEvent = $('#upcoming-event');
    const upcomingEventTitle = $('#upcoming-event-title');
    const upcomingEventTime = $('#upcoming-event-time');
    const upcomingEventLink = $('#upcoming-event-link');
    if (!upcomingEvent || !upcomingEventTitle || !upcomingEventTime || !upcomingEventLink) {
        return;
    }

    upcomingEvent.hidden = true;
    const event = payload?.event;
    if (!event || typeof event !== 'object') {
        return;
    }
    const title = typeof event.title === 'string' ? event.title.trim() : '';
    const videoId = typeof event.video_id === 'string' ? event.video_id.trim() : '';
    const scheduledStart = typeof event.scheduled_start_utc === 'string'
        ? new Date(event.scheduled_start_utc)
        : new Date('invalid');
    if (!title || !/^[A-Za-z0-9_-]{6,20}$/.test(videoId) || Number.isNaN(scheduledStart.getTime()) || scheduledStart <= new Date()) {
        return;
    }

    if (typeof event.url !== 'string' || event.url.trim() !== event.url || /[?#]$/.test(event.url)) {
        return;
    }
    const canonicalUrlMatch = event.url.match(/^https:\/\/www\.youtube\.com\/watch\?v=([A-Za-z0-9_-]{6,20})$/);
    if (!canonicalUrlMatch || canonicalUrlMatch[1] !== videoId) {
        return;
    }

    let url;
    try {
        url = new URL(event.url);
    } catch (_error) {
        return;
    }
    const queryEntries = [...url.searchParams.entries()];
    if (url.protocol !== 'https:' || url.origin !== 'https://www.youtube.com'
        || url.hostname !== 'www.youtube.com' || url.username || url.password
        || (url.port && url.port !== '443') || url.pathname !== '/watch'
        || url.hash || queryEntries.length !== 1 || queryEntries[0][0] !== 'v'
        || queryEntries[0][1] !== videoId) {
        return;
    }

    const upcomingDateFormatter = new Intl.DateTimeFormat('pl-PL', {
        dateStyle: 'full',
        timeStyle: 'short',
        timeZone: 'Europe/Warsaw',
    });
    upcomingEventTitle.textContent = title;
    upcomingEventTime.textContent = upcomingDateFormatter.format(scheduledStart);
    upcomingEventTime.setAttribute('datetime', event.scheduled_start_utc);
    upcomingEventLink.href = url.toString();
    upcomingEventLink.textContent = 'Otwórz stream ↗';
    upcomingEvent.hidden = false;
}

async function loadUpcomingEvent() {
    const upcomingEvent = $('#upcoming-event');
    if (!upcomingEvent) {
        return;
    }
    try {
        const response = await fetch(`upcoming.json?v=${Date.now()}`, { cache: 'no-store' });
        if (!response.ok) {
            throw new Error(`Upcoming event request failed: ${response.status}`);
        }
        renderUpcomingEvent(await response.json());
    } catch (error) {
        upcomingEvent.hidden = true;
        console.warn('Upcoming Nadgryzieni event is unavailable:', error);
    }
}

function getEpisodeYear(episode) {
    return episode.date ? episode.date.slice(0, 4) : '—';
}

function getAverageDuration(data) {
    return Number(data.stats?.average_duration ?? data.average_duration ?? 0);
}

function getMedian(values) {
    if (!values.length) {
        return 0;
    }
    const sorted = [...values].sort((a, b) => a - b);
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function getShortMonth(date) {
    const parsedDate = new Date(`${date}T00:00:00`);
    return Number.isNaN(parsedDate.getTime()) ? '—' : monthFormatter.format(parsedDate).replace('.', '');
}

function getYearLabel(row) {
    return row.isCurrentYear ? `${row.year} YTD` : String(row.year);
}

function getSystemTheme() {
    return window.matchMedia(SYSTEM_THEME_QUERY).matches ? 'dark' : 'light';
}

function getTheme() {
    return getSystemTheme();
}

function watchSystemTheme() {
    const mediaQuery = window.matchMedia(SYSTEM_THEME_QUERY);
    const handleChange = () => applyTheme(getSystemTheme());
    if (typeof mediaQuery.addEventListener === 'function') {
        mediaQuery.addEventListener('change', handleChange);
    } else if (typeof mediaQuery.addListener === 'function') {
        mediaQuery.addListener(handleChange);
    }
}

function applyTheme(theme) {
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
            url: episode.url || '',
            category: episode.category || '',
            hosts: Array.isArray(episode.hosts) ? episode.hosts.filter((name) => typeof name === 'string' && name.trim()) : [],
            hostsStatus: episode.hosts_status || '',
            hostsSource: episode.hosts_source || '',
            hostsSourceUrl: episode.hosts_source_url || '',
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
    const firstDate = normalizedEpisodes[0]?.date || '';
    const lastDate = normalizedEpisodes.at(-1)?.date || '';
    const firstYear = firstDate.slice(0, 4);
    const lastYear = lastDate.slice(0, 4);
    const yearRange = firstYear && lastYear ? `${firstYear}—${lastYear}` : '—';
    const totalEpisodes = stats.total_episodes ?? normalizedEpisodes.length;
    setText('#total-episodes', formatNumber(stats.total_episodes ?? normalizedEpisodes.length, 0));
    setText('#total-hours', formatNumber(stats.total_listening_hours));
    setText('#avg-duration', formatNumber(stats.average_duration));
    setText('#max-duration', formatNumber(stats.max_duration ?? maxDuration, 0));
    setText('#chart-average', `${formatNumber(getAverageDuration(data))} min`);
    setText('#hero-episode-count', formatNumber(totalEpisodes, 0));
    setText('#hero-last-date', formatDate(lastDate));
    setText('#archive-year-range', yearRange);
    setText('#hero-lede', `${formatNumber(totalEpisodes, 0)} odcinków zebranych w jednym, żywym archiwum.`);
    setText('#footer-update', formatDate(normalizedEpisodes.at(-1)?.date));
}

const hostCollator = new Intl.Collator('pl-PL', { sensitivity: 'base', numeric: false });

function hostDedupeKey(value) {
    return value.normalize('NFKC').replace(/\s+/gu, ' ').trim().toLocaleLowerCase('pl-PL');
}

function hostNameSortKey(value) {
    const normalized = value.normalize('NFKC').replace(/\s+/gu, ' ').trim();
    const parts = normalized.split(' ');
    return {
        firstName: parts[0] || '',
        surname: parts.slice(1).join(' '),
        fullName: normalized,
    };
}

function compareHostNames(left, right) {
    const leftKey = hostNameSortKey(left);
    const rightKey = hostNameSortKey(right);
    return hostCollator.compare(leftKey.firstName, rightKey.firstName)
        || hostCollator.compare(leftKey.surname, rightKey.surname)
        || hostCollator.compare(leftKey.fullName, rightKey.fullName);
}

function renderHostsSummary() {
    const list = $('#host-summary-list');
    const empty = $('#hosts-summary-empty');
    if (!list || !empty) {
        return;
    }

    const counts = new Map();
    let noDataCount = 0;
    rawEpisodes.forEach((episode) => {
        const names = Array.isArray(episode.hosts)
            ? episode.hosts.filter((name) => typeof name === 'string' && name.trim())
            : [];
        const uniqueNames = new Map(names.map((name) => [hostDedupeKey(name), name.trim()]));
        if (!uniqueNames.size) {
            noDataCount += 1;
        }
        uniqueNames.forEach((displayName, key) => {
            const entry = counts.get(key) || { name: displayName, count: 0 };
            entry.count += 1;
            counts.set(key, entry);
        });
    });

    const entries = [...counts.values()].sort((a, b) => b.count - a.count || compareHostNames(a.name, b.name));
    list.replaceChildren(...entries.map((entry) => {
        const item = document.createElement('li');
        item.className = 'host-summary-item';
        const name = document.createElement('strong');
        name.textContent = entry.name;
        const count = document.createElement('span');
        count.textContent = `${integerFormatter.format(entry.count)} ${entry.count === 1 ? 'odcinek' : 'odcinków'}`;
        item.append(name, count);
        return item;
    }));
    empty.hidden = entries.length > 0;
    setText('#hosts-summary-description', entries.length
        ? `${integerFormatter.format(entries.length)} unikalnych prowadzących · ${integerFormatter.format(noDataCount)} odcinków bez opublikowanej listy.`
        : 'Lista prowadzących i liczba odcinków, w których pojawiają się w archiwum.');
}

function isAfterparty(episode) {
    return episode?.category === 'afterparty';
}

function getReleaseBaseId(episode) {
    const number = Number(episode?.episodeId);
    return Number.isFinite(number) ? String(Math.floor(number)) : episode?.episodeId || '';
}

function getLatestRelease() {
    const latest = normalizedEpisodes.at(-1);
    const previous = normalizedEpisodes.at(-2);
    if (!latest) {
        return { episodes: [], paired: false };
    }

    const paired = Boolean(
        previous
        && getReleaseBaseId(latest) === getReleaseBaseId(previous)
        && [latest, previous].some(isAfterparty)
        && [latest, previous].some((episode) => episode?.category === 'main'),
    );
    const episodes = paired ? [previous, latest].sort((a, b) => Number(isAfterparty(a)) - Number(isAfterparty(b))) : [latest];
    return { episodes, paired };
}

function releaseSourceLabel(episode) {
    return episode.url.includes('patreon.com') ? 'Patreon' : 'Retro Rocket Network';
}

function createLatestReleaseItem(episode) {
    const item = document.createElement('li');
    item.className = `latest-release-item${isAfterparty(episode) ? ' is-afterparty' : ''}`;

    const link = document.createElement('a');
    link.className = 'latest-release-link';
    link.href = episode.url || '#episodes';
    if (episode.url) {
        link.target = '_blank';
        link.rel = 'noreferrer';
    }
    link.setAttribute('aria-label', episode.url
        ? `Otwórz odcinek ${episode.episodeId} w ${releaseSourceLabel(episode)}`
        : `Przejdź do odcinka ${episode.episodeId} w archiwum`);

    const top = document.createElement('span');
    top.className = 'latest-release-item-top';
    const id = document.createElement('strong');
    id.className = 'latest-release-id';
    id.textContent = episode.episodeId;
    const badge = document.createElement('span');
    badge.className = 'latest-release-badge';
    badge.textContent = isAfterparty(episode) ? 'AFTERPARTY' : 'ODCINEK';
    top.append(id, badge);

    const title = document.createElement('span');
    title.className = 'latest-release-item-title';
    title.textContent = episode.title;

    const meta = document.createElement('span');
    meta.className = 'latest-release-item-meta';
    meta.textContent = `${episode.duration || minutesToTime(episode.y)} · ${episode.url ? releaseSourceLabel(episode) : 'Archiwum'}`;

    link.append(top, title, meta);
    item.appendChild(link);
    return item;
}

function renderLatestRelease() {
    const panel = $('.latest-release');
    const list = $('#latest-release-list');
    const { episodes, paired } = getLatestRelease();
    if (!panel || !list || !episodes.length) {
        return;
    }

    const baseId = getReleaseBaseId(episodes[0]);
    panel.classList.toggle('latest-release-paired', paired);
    setText('#latest-release-label', paired ? 'NAJNOWSZE WYDANIE' : 'NAJNOWSZY ODCINEK');
    setText('#latest-release-title', paired ? `Wydanie ${baseId}` : 'Najnowszy odcinek');
    setText('#latest-release-count', paired ? 'DWA ODCINKI' : 'JEDEN ODCINEK');
    setText('#latest-release-date', formatDate(episodes[0].date));
    setText('#latest-release-foot', paired ? 'Dwa najnowsze punkty na osi czasu' : 'Najnowszy punkt na osi czasu');
    list.setAttribute('aria-label', paired ? `Wydanie ${baseId}: dwa odcinki` : 'Najnowszy odcinek');
    list.replaceChildren(...episodes.map(createLatestReleaseItem));

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
    setText('#longest-title', longest.title);
    setText('#longest-meta', `${formatDate(longest.date)} · ${longest.duration || minutesToTime(longest.y)}`);
    setText('#shortest-duration', minutesLabel(shortest.y));
    setText('#shortest-title', shortest.title);
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
    setText('#year-axis-start', years[0] || '—');
    setText('#year-axis-end', years.at(-1) || '—');
    container.replaceChildren();
    years.forEach((year) => {
        const bar = document.createElement('div');
        bar.className = 'year-bar';
        const count = counts[year];
        bar.title = `${year}: ${count} odcinków`;
        bar.setAttribute('aria-label', `${year}: ${count} odcinków`);
        const barHeight = Math.max(4, (count / max) * 100);
        bar.style.setProperty('--bar-height', `${barHeight}%`);
        const countLabel = document.createElement('span');
        countLabel.className = 'year-bar-count';
        countLabel.textContent = integerFormatter.format(count);
        const fill = document.createElement('span');
        fill.className = 'year-bar-fill';
        fill.style.height = `${barHeight}%`;
        const label = document.createElement('span');
        label.className = 'year-bar-label';
        label.textContent = year;
        bar.append(countLabel, fill, label);
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

function getYearlyStats() {
    const currentYear = Number(normalizedEpisodes.at(-1)?.date.slice(0, 4));
    const grouped = normalizedEpisodes.reduce((result, episode) => {
        const year = Number(getEpisodeYear(episode));
        if (!Number.isFinite(year) || year < YEARLY_STATS_START) {
            return result;
        }
        result[year] ||= [];
        result[year].push(episode);
        return result;
    }, {});

    return Object.entries(grouped).sort(([a], [b]) => a.localeCompare(b)).map(([year, episodes]) => {
        const durations = episodes.map((episode) => episode.y).sort((a, b) => a - b);
        const dates = episodes.map((episode) => episode.date).sort();
        const gaps = dates.slice(1).map((date, index) => {
            const previous = new Date(`${dates[index]}T00:00:00`);
            const current = new Date(`${date}T00:00:00`);
            return Math.round((current - previous) / 86400000);
        });
        const monthCount = new Set(dates.map((date) => date.slice(0, 7))).size;
        const isCurrentYear = Number(year) === currentYear;
        const coverage = isCurrentYear
            ? `${getShortMonth(dates[0])}—${getShortMonth(dates.at(-1))} · YTD`
            : monthCount === 12
                ? 'Pełny rok'
                : `${getShortMonth(dates[0])}—${getShortMonth(dates.at(-1))} · częściowy`;
        return {
            year: Number(year),
            episodes,
            count: episodes.length,
            totalHours: durations.reduce((total, duration) => total + duration, 0) / 60,
            median: getMedian(durations),
            average: durations.reduce((total, duration) => total + duration, 0) / durations.length,
            overTwoHours: durations.filter((duration) => duration >= 120).length,
            overTwoHoursPercent: (durations.filter((duration) => duration >= 120).length / durations.length) * 100,
            firstDate: dates[0],
            lastDate: dates.at(-1),
            longestGap: Math.max(...gaps, 0),
            isCurrentYear,
            coverage,
        };
    });
}

function renderYearlyStats() {
    const rows = getYearlyStats();
    const episodeChart = $('#yearly-episode-chart');
    const durationChart = $('#yearly-duration-chart');
    const tableBody = $('#yearly-stats-body');
    if (!rows.length || !episodeChart || !durationChart || !tableBody) {
        return;
    }

    const mostEpisodes = rows.reduce((best, row) => row.count > best.count ? row : best, rows[0]);
    const mostHours = rows.reduce((best, row) => row.totalHours > best.totalHours ? row : best, rows[0]);
    const longestMedian = rows.reduce((best, row) => row.median > best.median ? row : best, rows[0]);
    setText('#yearly-highlight-count', integerFormatter.format(mostEpisodes.count));
    setText('#yearly-highlight-count-meta', `${getYearLabel(mostEpisodes)} · ${mostEpisodes.coverage}`);
    setText('#yearly-highlight-hours', `${formatNumber(mostHours.totalHours)} h`);
    setText('#yearly-highlight-hours-meta', `${getYearLabel(mostHours)} · ${mostHours.count} odcinków`);
    setText('#yearly-highlight-median', `${formatNumber(longestMedian.median)} min`);
    setText('#yearly-highlight-median-meta', `${getYearLabel(longestMedian)} · mediana`);
    setText('#yearly-analysis-range', `Zakres szczegółowej analizy: ${rows[0].year}—${rows.at(-1).year}`);
    setText('#yearly-analysis-summary', `Liczba, długość i rytm publikacji od ${rows[0].year} do ${rows.at(-1).year}. Dane pokazują także niepełne okresy.`);
    const partialPeriods = rows.filter((row) => row.coverage !== 'Pełny rok').map((row) => `${row.year}: ${row.coverage}`).join(' · ');
    setText('#yearly-coverage-note', partialPeriods || 'Wszystkie lata obejmują pełny zakres danych');
    const currentRow = rows.find((row) => row.isCurrentYear);
    setText('#yearly-table-note', currentRow
        ? `Wartości dla ${currentRow.year} obejmują ${currentRow.coverage.replace(' · YTD', '')} ${currentRow.year} i są oznaczone jako YTD; nie porównuj ich bezpośrednio z pełnym rokiem.`
        : 'Wartości roczne są obliczane bezpośrednio z danych archiwum.');

    const maxCount = Math.max(...rows.map((row) => row.count), 1);
    episodeChart.replaceChildren(...rows.map((row) => {
        const column = document.createElement('div');
        column.className = `yearly-column${row.isCurrentYear ? ' is-ytd' : ''}`;
        column.title = `${getYearLabel(row)}: ${row.count} odcinków`;
        const value = document.createElement('span');
        value.className = 'yearly-column-value';
        value.textContent = integerFormatter.format(row.count);
        const fill = document.createElement('span');
        fill.className = 'yearly-column-fill';
        fill.style.height = `${Math.max(6, (row.count / maxCount) * 100)}%`;
        const label = document.createElement('span');
        label.className = 'yearly-column-label';
        label.textContent = row.isCurrentYear ? `${row.year}*` : String(row.year);
        column.append(value, fill, label);
        return column;
    }));

    const durationScale = Math.max(180, Math.ceil(Math.max(...rows.map((row) => Math.max(row.median, row.average))) / 30) * 30);
    durationChart.replaceChildren(...rows.map((row) => {
        const item = document.createElement('div');
        item.className = `yearly-duration-row${row.isCurrentYear ? ' is-ytd' : ''}`;
        item.title = `${getYearLabel(row)}: mediana ${formatNumber(row.median)} min, średnia ${formatNumber(row.average)} min`;
        const label = document.createElement('span');
        label.className = 'yearly-duration-year';
        label.textContent = row.isCurrentYear ? `${row.year}*` : String(row.year);
        const track = document.createElement('span');
        track.className = 'yearly-duration-track';
        const median = document.createElement('span');
        median.className = 'yearly-duration-median';
        median.style.width = `${(row.median / durationScale) * 100}%`;
        const average = document.createElement('span');
        average.className = 'yearly-duration-average';
        average.style.left = `${Math.min(100, (row.average / durationScale) * 100)}%`;
        track.append(median, average);
        const value = document.createElement('span');
        value.className = 'yearly-duration-value';
        value.textContent = `${formatNumber(row.median)} min`;
        item.append(label, track, value);
        return item;
    }));

    tableBody.replaceChildren(...rows.map((row) => {
        const tableRow = document.createElement('tr');
        if (row.isCurrentYear) {
            tableRow.classList.add('is-ytd');
        }
        const values = [
            getYearLabel(row),
            integerFormatter.format(row.count),
            `${formatNumber(row.totalHours)} h`,
            `${formatNumber(row.median)} min`,
            `${formatNumber(row.average)} min`,
            `${integerFormatter.format(row.overTwoHours)} · ${formatNumber(row.overTwoHoursPercent)}%`,
            row.longestGap ? `${integerFormatter.format(row.longestGap)} dni` : '—',
            row.coverage,
        ];
        values.forEach((value, index) => {
            const cell = document.createElement(index === 0 ? 'th' : 'td');
            if (index === 0) {
                cell.scope = 'row';
            }
            cell.textContent = value;
            tableRow.appendChild(cell);
        });
        return tableRow;
    }));
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
    const minimumWidth = normalizedEpisodes.length * CHART_EPISODE_SPACING;
    chartWrapper.style.width = `${Math.max(scrollContainer.clientWidth, minimumWidth)}px`;
}

function hideChartTooltip() {
    const tooltip = $('#chart-tooltip');
    if (tooltip) {
        tooltip.hidden = true;
        tooltip.classList.remove('is-below');
    }
}

function positionChartTooltip(chart) {
    const tooltip = $('#chart-tooltip');
    const scrollContainer = $('#chart-scroll');
    const chartWrapper = $('#chart-wrapper');
    const chartTooltip = chart?.tooltip;
    if (!tooltip || !scrollContainer || !chartWrapper || !chartTooltip || chartTooltip.opacity === 0) {
        hideChartTooltip();
        return;
    }

    const canvasRect = chart.canvas.getBoundingClientRect();
    const wrapperRect = chartWrapper.getBoundingClientRect();
    const scrollRect = scrollContainer.getBoundingClientRect();
    const pointer = chart.$tooltipPointer;
    const pointX = pointer ? pointer.clientX : canvasRect.left + chartTooltip.caretX;
    const pointY = pointer ? pointer.clientY : canvasRect.top + chartTooltip.caretY;
    const edgePadding = 8;
    const horizontalGap = 12;
    let tooltipSize = tooltip._chartTooltipSize;
    if (!tooltipSize) {
        const rect = tooltip.getBoundingClientRect();
        tooltipSize = { width: rect.width, height: rect.height };
        tooltip._chartTooltipSize = tooltipSize;
    }
    const tooltipWidth = tooltipSize.width;
    const tooltipHeight = tooltipSize.height;
    const minimumLeft = scrollRect.left + edgePadding;
    const maximumLeft = Math.max(minimumLeft, scrollRect.right - tooltipWidth - edgePadding);
    const left = Math.min(maximumLeft, Math.max(minimumLeft, pointX - tooltipWidth / 2));
    const above = pointY - tooltipHeight - horizontalGap;
    const below = pointY + horizontalGap;
    const top = above >= scrollRect.top + edgePadding
        ? above
        : below + tooltipHeight <= scrollRect.bottom - edgePadding
            ? below
            : Math.max(scrollRect.top + edgePadding, Math.min(above, scrollRect.bottom - tooltipHeight - edgePadding));
    const caretOffset = Math.max(0, Math.min(tooltipWidth, pointX - left));
    const transform = `translate3d(${left - wrapperRect.left}px, ${top - wrapperRect.top}px, 0)`;

    if (tooltip.style.transform !== transform) {
        tooltip.style.transform = transform;
    }
    tooltip.style.setProperty('--chart-tooltip-caret-x', `${caretOffset}px`);
    tooltip.classList.toggle('is-below', top > pointY);
}

function renderExternalTooltip({ chart, tooltip }) {
    const tooltipElement = $('#chart-tooltip');
    const raw = tooltip.dataPoints?.[0]?.raw;
    if (!tooltipElement || tooltip.opacity === 0 || !raw) {
        hideChartTooltip();
        return;
    }

    const contentKey = JSON.stringify([raw.episodeId, raw.title, raw.y, raw.date]);
    if (tooltipElement._contentKey !== contentKey) {
        const title = document.createElement('strong');
        title.className = 'chart-tooltip-heading';
        title.textContent = `Odcinek: ${raw.episodeId}`;
        const episodeTitle = document.createElement('span');
        episodeTitle.className = 'chart-tooltip-title';
        episodeTitle.textContent = raw.title;
        const details = document.createElement('span');
        details.className = 'chart-tooltip-details';
        const date = formatDate(raw.date);
        details.textContent = date === '—'
            ? `Czas: ${minutesToTime(raw.y)}`
            : `Czas: ${minutesToTime(raw.y)} · Data: ${date}`;
        tooltipElement.replaceChildren(title, episodeTitle, details);
        tooltipElement._contentKey = contentKey;
        tooltipElement._chartTooltipSize = null;
    }
    tooltipElement.hidden = false;
    positionChartTooltip(chart);
}

const chartTooltipPointerPlugin = {
    id: 'tooltipPointer',
    beforeEvent(chart, { event }) {
        if (event.type === 'mouseout') {
            chart.$tooltipPointer = null;
            return;
        }
        if (Number.isFinite(event.x) && Number.isFinite(event.y)) {
            const canvasRect = chart.canvas.getBoundingClientRect();
            chart.$tooltipPointer = {
                clientX: canvasRect.left + event.x,
                clientY: canvasRect.top + event.y,
            };
        }
    },
};

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
        : 'Wykres punktowy zachowuje większe odstępy między odcinkami. Najedź na punkt, aby zobaczyć szczegóły, i przewijaj w poziomie, aby zobaczyć pełny wykres; identyfikatory zachowują wartości ułamkowe i specjalne.');
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
    hideChartTooltip();
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
            animation: { duration: 350, easing: 'easeOutQuart' },
            transitions: {
                active: { animation: { duration: 0 } },
                resize: { animation: { duration: 0 } },
            },
            interaction: { intersect: false, mode: 'nearest' },
            plugins: {
                legend: { display: false },
                tooltip: {
                    enabled: false,
                    position: 'nearest',
                    external: renderExternalTooltip,
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
                        maxTicksLimit: 60,
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
        plugins: [chartTooltipPointerPlugin, {
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
    const hosts = document.createElement('span');
    hosts.className = 'episode-hosts';
    hosts.textContent = episode.hosts.length
        ? `Prowadzący: ${episode.hosts.join(', ')}`
        : 'Prowadzący: Brak danych';
    const sourceLink = document.createElement('a');
    sourceLink.className = 'episode-link';
    sourceLink.href = episode.url;
    sourceLink.target = '_blank';
    sourceLink.rel = 'noreferrer';
    sourceLink.textContent = episode.url.includes('patreon.com') ? 'Patreon' : 'Retro Rocket Network';
    sourceLink.setAttribute('aria-label', `Otwórz odcinek ${episode.episodeId} w ${sourceLink.textContent}`);
    sourceLink.insertAdjacentText('beforeend', ' ↗');
    card.append(top, title, date, hosts, sourceLink);
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
            const searchText = `${episode.episodeId} ${episode.title} ${episode.date} ${episode.hosts.join(' ')}`.toLocaleLowerCase('pl-PL');
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
    watchSystemTheme();

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

    $('#chart-scroll')?.addEventListener('scroll', () => {
        if (chartInstance?.tooltip?.opacity) {
            positionChartTooltip(chartInstance);
        }
    });

    window.addEventListener('resize', () => {
        if (normalizedEpisodes.length) {
            setChartLayout(chartMode === 'line');
            chartInstance?.resize();
            const tooltip = $('#chart-tooltip');
            if (tooltip) {
                tooltip._chartTooltipSize = null;
            }
            if (chartInstance?.tooltip?.opacity) {
                positionChartTooltip(chartInstance);
            }
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
        rawEpisodes = Array.isArray(chartData.episodes) ? chartData.episodes : [];
        normalizedEpisodes = normalizeEpisodes(rawEpisodes);
        renderStats(chartData);
        renderLatestRelease();
        renderHostsSummary();
        renderSignals();
        renderYearBars();
        renderDurationBars();
        renderYearlyStats();
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

applyTheme(getTheme());
bindInteractions();
loadUpcomingEvent();
loadData();
