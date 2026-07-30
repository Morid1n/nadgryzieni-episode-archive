// ===== Nadgryzieni Statistics Script =====
// Uses Chart.js v4 to render episode duration data

// Load and render all data
function loadData() {
    fetch('data.json')
        .then(response => response.json())
        .then(data => {
            renderStats(data);
            renderChart(data);
        })
        .catch(error => {
            console.error('Error loading data:', error);
            document.getElementById('chart-wrapper').innerHTML =
                '<p style="color: #F43E25; text-align: center; padding: 40px;">Błąd ładowania danych. Spróbuj odświeżyć stronę.</p>';
        });
}

function renderStats(data) {
    document.getElementById('total-episodes').textContent = data.stats.total_episodes;
    document.getElementById('total-hours').textContent = data.stats.total_listening_hours;
    document.getElementById('avg-duration').textContent = data.stats.average_duration;
    document.getElementById('max-duration').textContent = Math.round(data.stats.max_duration);
}

function renderChart(data) {
    const ctx = document.getElementById('episode-chart').getContext('2d');

    const episodes = data.episodes.map(ep => ({
        x: parseInt(ep.episode) || 0,
        y: ep.minutes,
        title: ep.title,
        date: ep.date,
        duration: ep.duration,
        episode: ep.episode,
    }));

    // Sort by episode number
    episodes.sort((a, b) => a.x - b.x);

    const chart = new Chart(ctx, {
        type: 'scatter',
        data: {
            datasets: [{
                label: '',
                data: episodes,
                backgroundColor: 'rgba(99, 47, 83, 0.7)',
                borderColor: '#632F53',
                borderWidth: 1,
                pointRadius: 4,
                pointHoverRadius: 7,
                pointHoverBorderWidth: 2,
                pointHoverBorderColor: '#fff',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: {
                intersect: false,
                mode: 'point',
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
                            const ep = context[0].raw;
                            return 'Ep: ' + ep.episode;
                        },
                        label: function(context) {
                            const ep = context.raw;
                            return 'T: ' + minutesToTime(ep.minutes);
                        },
                    },
                },
            },
            scales: {
                x: {
                    type: 'linear',
                    position: 'bottom',
                    grid: {
                        color: '#f0f0f0',
                        drawBorder: false,
                    },
                    ticks: {
                        color: '#29505F',
                        font: { size: 10 },
                        max: 600,
                        min: 0,
                    },
                    title: {
                        display: true,
                        text: 'Numer odcinka',
                        color: '#29505F',
                        font: { size: 13, weight: 600 },
                    },
                    suggestedMin: 0,
                    suggestedMax: 600,
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
            plugins: [
                {
                    id: 'avgLine',
                    afterDraw: function(chart) {
                        const ctx = chart.ctx;
                        const yScale = chart.scales.y;
                        const avgY = yScale.getPixelForValue(data.average_duration);
                        ctx.save();
                        ctx.strokeStyle = '#29505F';
                        ctx.lineWidth = 2;
                        ctx.setLineDash([5, 5]);
                        ctx.beginPath();
                        ctx.moveTo(chart.scales.x.left, avgY);
                        ctx.lineTo(chart.scales.x.right, avgY);
                        ctx.stroke();
                        ctx.setLineDash([]);
                        ctx.fillStyle = '#29505F';
                        ctx.font = 'italic 10px sans-serif';
                        ctx.textAlign = 'left';
                        ctx.fillText('Średnia: ' + data.average_duration + ' min', chart.scales.x.left + 5, avgY - 5);
                        ctx.restore();
                    },
                },
            ],
        },
    });
}

function minutesToTime(minutes) {
    const h = Math.floor(minutes / 60);
    const m = Math.floor(minutes % 60);
    const s = Math.round((minutes % 1) * 60);
    return h.toString().padStart(2, '0') + ':' + m.toString().padStart(2, '0') + ':' + s.toString().padStart(2, '0');
}

// Initialize (script is at bottom of body, DOM is already ready)
loadData();
