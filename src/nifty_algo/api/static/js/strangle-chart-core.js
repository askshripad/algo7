// static/js/strangle-chart-core.js
(function () {
  /**
   * Format timestamp for category x-axis (HH:MM). Accepts Date, ISO string, or "YYYY-MM-DD HH:MM:SS".
   */
  function toHHMM(ts) {
    if (ts == null || String(ts).trim() === "") return "";
    if (ts instanceof Date) {
      if (isNaN(ts.getTime())) return "";
      const h = String(ts.getHours()).padStart(2, "0");
      const m = String(ts.getMinutes()).padStart(2, "0");
      return h + ":" + m;
    }
    const s = String(ts).trim().replace("T", " ");
    const parts = s.split(/\s+/);
    return parts.length >= 2 ? parts[1].slice(0, 5) : String(ts).slice(0, 5);
  }

  /**
   * Backtest UI rule: Crores (÷1e7) when trade day === expiry day, else Lakhs (÷1e5).
   * Live chart receives unit + divisor from the server (same rule).
   */
  function applyVolumeDiffSeries(volumeChart, opts) {
    if (!volumeChart) return;
    const { labels, values, unit, titleElement } = opts || {};
    const unitSafe = unit || "Lakhs";
    volumeChart.data.labels = labels || [];
    volumeChart.data.datasets[0].data = values || [];
    volumeChart.data.datasets[0].label = "Volume Diff (CE − PE) [" + unitSafe + "]";
    const yVol = volumeChart.options.scales.y;
    if (yVol && yVol.title) {
      yVol.title.display = true;
      yVol.title.text = unitSafe;
    }
    if (titleElement) {
      titleElement.textContent = "Volume Diff (CE − PE) in " + unitSafe;
    }
    volumeChart.update("none");
  }

  function createStrangleCharts(opts) {
    const {
      premiumCanvasId,
      volumeCanvasId,
      volumeUnitLabel = "Lakhs",
      xAxisTime = true,
      /** If set, volume chart x-axis mode (category HH:MM vs time). Defaults to xAxisTime. */
      volumeXAxisTime,
      includeTarget = true,
    } = opts || {};
    const volTime = volumeXAxisTime !== undefined ? volumeXAxisTime : xAxisTime;

    const premiumCtx = document.getElementById(premiumCanvasId).getContext("2d");
    const volumeCtx = document.getElementById(volumeCanvasId).getContext("2d");
    const baseGrid = { color: "#253055" };
    const tickColor = "#95a3c7";
    const hover = {
      interaction: { mode: "index", intersect: false, axis: "x" },
      plugins: {
        tooltip: {
          mode: "index",
          intersect: false,
          animation: false,
          backgroundColor: "rgba(15, 26, 51, 0.96)",
          borderColor: "#2a4070",
          borderWidth: 1,
          padding: 10,
          titleColor: "#e7eefc",
          bodyColor: "#e7eefc",
        },
      },
    };

    const premiumDatasets = [
      {
        label: "Total Premium",
        data: [],
        yAxisID: "y",
        borderColor: "#ff4d4d",
        backgroundColor: "rgba(255,77,77,0.15)",
        fill: true,
        tension: 0.2,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBorderWidth: 2,
        borderWidth: 2,
      },
      {
        label: "Nifty Close",
        data: [],
        yAxisID: "y1",
        borderColor: "#4fa3ff",
        backgroundColor: "rgba(79,163,255,0.08)",
        fill: false,
        tension: 0.2,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHoverBorderWidth: 2,
        borderWidth: 2,
        spanGaps: true,
      },
    ];
    if (includeTarget) {
      premiumDatasets.push({
        label: "Target",
        data: [],
        yAxisID: "y",
        borderColor: "#f39c12",
        borderWidth: 1,
        borderDash: [5, 5],
        fill: false,
        pointRadius: 0,
        hidden: true,
      });
    }

    const premiumChart = new Chart(premiumCtx, {
      type: "line",
      data: { labels: [], datasets: premiumDatasets },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        ...hover,
        plugins: {
          legend: { labels: { color: "#e7eefc" } },
          ...hover.plugins,
        },
        scales: {
          x: xAxisTime
            ? { type: "time", time: { unit: "minute" }, ticks: { color: tickColor }, grid: baseGrid }
            : { ticks: { color: tickColor }, grid: baseGrid },
          y: {
            type: "linear",
            position: "left",
            ticks: { color: "#ff4d4d" },
            grid: baseGrid,
            title: { display: true, text: "Premium (₹)", color: "#ff4d4d", font: { size: 11 } },
          },
          y1: {
            type: "linear",
            position: "right",
            ticks: { color: "#4fa3ff" },
            grid: { drawOnChartArea: false },
            title: { display: true, text: "Nifty", color: "#95a3c7", font: { size: 11 } },
          },
        },
      },
    });

    const volumeChart = new Chart(volumeCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "Volume Diff (CE − PE) [" + volumeUnitLabel + "]",
            data: [],
            borderColor: "#ffb020",
            backgroundColor: "rgba(255,176,32,0.14)",
            fill: true,
            tension: 0.2,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointHoverBorderWidth: 2,
            borderWidth: 2,
          },
        ],
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        ...hover,
        plugins: {
          legend: { labels: { color: "#e7eefc" } },
          ...hover.plugins,
        },
        scales: {
          x: volTime
            ? { type: "time", time: { unit: "minute" }, ticks: { color: tickColor }, grid: baseGrid }
            : { ticks: { color: tickColor }, grid: baseGrid },
          y: {
            ticks: { color: tickColor },
            grid: baseGrid,
            title: { display: true, text: volumeUnitLabel, color: tickColor, font: { size: 11 } },
          },
        },
      },
    });

    return { premiumChart, volumeChart };
  }

  window.StrangleChartCore = {
    createStrangleCharts,
    toHHMM,
    applyVolumeDiffSeries,
  };
})();

  