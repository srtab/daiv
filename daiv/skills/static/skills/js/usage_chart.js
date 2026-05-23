(function () {
  let retries = 0;
  function init() {
    if (typeof Chart === "undefined") {
      // Chart.js still loading — retry briefly. Cap at ~3s so a blocked CDN
      // (network policy, ad-blocker, air-gap) doesn't leak an endless timer.
      if (++retries < 60) setTimeout(init, 50);
      return;
    }
    const dataNode = document.getElementById("skill-usage-data");
    const canvas = document.getElementById("skill-usage-canvas");
    if (!dataNode || !canvas) return;

    let series;
    try {
      series = JSON.parse(dataNode.textContent);
    } catch (err) {
      console.warn("skill-usage-data parse failed", err);
      return;
    }

    const style = getComputedStyle(canvas);
    const barColor = style.getPropertyValue("--chart-bar").trim() || "rgba(255,255,255,0.18)";
    const barHover = style.getPropertyValue("--chart-bar-hover").trim() || "rgba(255,255,255,0.42)";
    const tickColor = style.getPropertyValue("--chart-tick").trim() || "rgba(255,255,255,0.35)";
    const gridColor = style.getPropertyValue("--chart-grid").trim() || "rgba(255,255,255,0.04)";
    const tooltipBg = style.getPropertyValue("--chart-tooltip-bg").trim() || "rgba(15,23,42,0.95)";
    const tooltipFg = style.getPropertyValue("--chart-tooltip-fg").trim() || "rgba(255,255,255,0.92)";
    const fontFamily = style.fontFamily || "Outfit, sans-serif";

    Chart.defaults.font.family = fontFamily;
    Chart.defaults.color = tickColor;

    const labels = series.map(function (entry) { return entry.day; });
    const values = series.map(function (entry) { return entry.count; });

    new Chart(canvas, {
      type: "bar",
      data: {
        labels: labels,
        datasets: [{
          data: values,
          backgroundColor: barColor,
          hoverBackgroundColor: barHover,
          borderWidth: 0,
          borderRadius: 2,
          maxBarThickness: 14,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: tooltipBg,
            titleColor: tooltipFg,
            bodyColor: tooltipFg,
            displayColors: false,
            padding: 8,
            cornerRadius: 6,
            callbacks: {
              title: function (items) {
                const d = new Date(items[0].label);
                return d.toLocaleDateString(undefined, {
                  weekday: "short", day: "numeric", month: "short",
                });
              },
              label: function (item) {
                const n = item.parsed.y;
                return n === 1 ? "1 invocation" : n + " invocations";
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: {
              color: tickColor,
              maxRotation: 0,
              autoSkip: true,
              autoSkipPadding: 12,
              callback: function (value, index) {
                if (index % 5 !== 0 && index !== labels.length - 1) return "";
                const d = new Date(labels[index]);
                return d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
              },
            },
          },
          y: {
            beginAtZero: true,
            ticks: { color: tickColor, precision: 0, maxTicksLimit: 4 },
            grid: { color: gridColor, drawTicks: false },
            border: { display: false },
          },
        },
      },
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
