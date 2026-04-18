function baseChartConfig(labels, data, label, color, type) {
    return {
        type,
        data: {
            labels,
            datasets: [
                {
                    label,
                    data,
                    borderColor: color,
                    backgroundColor: `${color}33`,
                    fill: type === 'line',
                    tension: 0.35,
                    borderWidth: 3,
                    maxBarThickness: 36,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: {
                        usePointStyle: true,
                    },
                },
            },
            scales: {
                y: {
                    beginAtZero: true,
                    grid: {
                        color: 'rgba(148, 163, 184, 0.16)',
                    },
                },
                x: {
                    grid: {
                        display: false,
                    },
                },
            },
        },
    };
}

function createLineChart(canvasId, labels, data, label, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) {
        return;
    }
    new Chart(canvas, baseChartConfig(labels, data, label, color, 'line'));
}

function createBarChart(canvasId, labels, data, label, color) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) {
        return;
    }
    new Chart(canvas, baseChartConfig(labels, data, label, color, 'bar'));
}