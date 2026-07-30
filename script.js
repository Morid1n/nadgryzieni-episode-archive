// Nadgryzieni Episode Chart
// Uses Chart.js with horizontal scrolling for dense scatter plot

document.addEventListener('DOMContentLoaded', function() {
    fetch('data.json')
        .then(response => response.json())
        .then(data => {
            renderChart(data);
            renderStats(data);
            renderLegend(data);
        })
        .catch(error => {
            console.error('Error loading data:', error);
            document.getElementById('chart-wrapper').innerHTML =
                '<p style="color: #F43E25; text-align: center; padding: 40px;">Błąd ładowania danych. Spróbuj odświeżyć stronę.</p>';
        });
});

function renderStats(data) {
    document.getElementById('total-episodes').textContent = data.stats.total_episodes;
    document.getElementById('total-hours').textContent = data.stats.total_listening_hours;
    document.getElementById('avg-duration').textContent = data.stats.average_duration;
    document.getElementById('max-duration').textContent = Math.round(data.stats.max_duration);
}

function renderLegend(data) {
    const legendItems = document.getElementById('legend-items');
    legendItems.innerHTML = '';

    for (const [key, info] of Object.entries(data.categories)) {
        const item = document.createElement('div');
        item.className = 'legend-item';
        item.innerHTML =
            '<div class="legend-color" style="background-color: ' + info.color + ';"></div>' +
            '<span>' + info.label + '</span>';
        legendItems.appendChild(item);
    }
}

function renderChart(data) {
    const ctx = document.getElementById('episode-chart').getContext('2d');

    // Group episodes by category for datasets
    const datasets = {};
    for (const [key, info] of Object.entries(data.categories)) {
        datasets[key] = {
            label: info.label,
            data: [],
            backgroundColor: info.color,
            borderColor: info.color,
            borderWidth: 0,
            pointRadius: 5,
            pointHoverRadius: 8,
            pointHoverBorderWidth: 2,
            pointHoverBorderColor: '#fff',
            showLine: false,
        };
    }

    // Populate datasets
    data.episodes.forEach(ep => {
        const point = {
            x: parseInt(ep.episode) || 0,
            y: ep.minutes,
            title: ep.title,
            date: ep.date,
            duration: ep.duration,
            episode: ep.episode,
        };
        if (datasets[ep.category]) {
            datasets[ep.category].data.push(point);
        }
    });

    // Sort datasets to match legend order
    const orderedDatasets = [];
    const legendOrder = ['main', 'live', 'po_godzinach', 'sp', 'half', 'special', 'afterparty', 'prawie', 'na_placu_budowy', 'na_spacerze', 'video', 'w_biegu'];
    legendOrder.forEach(key => {
        if (datasets[key] && datasets[key].data.length > 0) {
            orderedDatasets.push(datasets[key]);
        }
    });

    const chart = new Chart(ctx, {
        type: 'scatter',
        data: {
            datasets: orderedDatasets,
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
                            return 'Odcinek ' + ep.episode + ': ' + ep.title;
                        },
                        label: function(context) {
                            const ep = context.raw;
                            return [
                                'Data: ' + ep.date,
                                'Czas trwania: ' + ep.duration,
                                'Minuty: ' + ep.y.toFixed(1),
                            ];
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
}// Cache: Thu Jul 30 11:02:11 CEST 2026
