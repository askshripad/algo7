// static/js/nifty-chart-core.js
(function () {
  const COLORS = {
    vrPremium: "#a16207",
    vrPremiumFill: "rgba(161, 98, 7, 0.08)",
    otmPremium: "#7e22ce",
    nifty: "#111827",
    orHigh: "#15803d",
    orLow: "#ea580c",
    candleUp: "#65a30d",
    candleDown: "#e11d48",
    candleHalo: "rgba(255, 255, 255, 0.95)",
    volDouble: "#6d28d9",
    volDoubleCe: "#dc2626",
    volDoublePe: "#65a30d",
    vrCeStrike: "#dc2626",
    vrPeStrike: "#1d4ed8",
    otmCeStrike: "#a21caf",
    otmPeStrike: "#0f766e",
    putFlow: "#c026d3",
    putFlowMark: "#1d4ed8",
    vrCeVolume: "#dc2626",
    vrPeVolume: "#1d4ed8",
    offsetVolume: "#0369a1",
    vrVolDiff: "#a16207",
  };

  function toHHMM(ts) {
    if (window.StrangleChartCore && window.StrangleChartCore.toHHMM) {
      return window.StrangleChartCore.toHHMM(ts);
    }
    if (ts instanceof Date) {
      return ts.getHours() + ":" + String(ts.getMinutes()).padStart(2, "0");
    }
    return String(ts).slice(11, 16);
  }

  /** IST session chart x-axis: 24h labels every 30 minutes (9:30, 10:00, …). */
  function marketTimeAxis(tickColor, grid) {
    return {
      type: "time",
      time: {
        unit: "minute",
        stepSize: 30,
        displayFormats: {
          minute: "H:mm",
          hour: "H:mm",
          second: "H:mm",
        },
        tooltipFormat: "H:mm",
      },
      ticks: {
        color: tickColor,
        maxRotation: 0,
        minRotation: 0,
        autoSkip: false,
        source: "auto",
        font: { size: 10 },
      },
      grid: grid,
    };
  }

  /** Pin x-axis to NSE cash session (09:15–15:30) for the trade date in view. */
  function applySessionXBounds(chart, niftySeries) {
    const x = chart.options.scales.x;
    if (!x) return;
    const pts = (niftySeries || []).filter(function (p) {
      return p && p.x;
    });
    if (!pts.length) {
      x.min = undefined;
      x.max = undefined;
      return;
    }
    const d0 = pts[0].x instanceof Date ? pts[0].x : new Date(pts[0].x);
    if (isNaN(d0.getTime())) return;
    const y = d0.getFullYear();
    const m = d0.getMonth();
    const day = d0.getDate();
    x.min = new Date(y, m, day, 9, 15, 0);
    x.max = new Date(y, m, day, 15, 30, 0);
  }

  /** Full-width horizontal guide on Nifty axis (y1). Ignores invalid levels. */
  function hLineOnNifty(niftySeries, level) {
    if (level == null || !isFinite(level) || level <= 0 || !niftySeries.length) return [];
    return niftySeries.map(function (p) {
      return { x: p.x, y: level };
    });
  }

  function sanitizePoint(p, minPositive) {
    if (!p) return { x: p?.x, y: null };
    const y = p.y;
    if (y == null || !isFinite(y) || y <= minPositive) return { x: p.x, y: null };
    return p;
  }

  function sanitizeSeries(arr, minPositive) {
    return (arr || []).map(function (p) {
      return sanitizePoint(p, minPositive);
    });
  }

  function niftySeriesValues(arr) {
    const vals = [];
    (arr || []).forEach(function (p) {
      if (p && p.y != null && isFinite(p.y) && p.y > 0) vals.push(p.y);
    });
    return vals;
  }

  /**
   * Nifty axis: zoom to spot (+ OR). Expand only when visible strikes break bounds.
   * All Nifty points stay in range; tooltips unchanged for clipped overlays.
   */
  function tightenNiftyY1Scale(chart, nifty, orLevels, strikes, layers) {
    const scale = chart.options.scales.y1;
    if (!scale) return;

    const L = layers || {};
    const vals = niftySeriesValues(nifty);
    if (L.or !== false) {
      if (orLevels?.or_high > 0) vals.push(orLevels.or_high);
      if (orLevels?.or_low > 0) vals.push(orLevels.or_low);
    }

    if (!vals.length) {
      scale.min = undefined;
      scale.max = undefined;
      return;
    }

    let lo = Math.min.apply(null, vals);
    let hi = Math.max.apply(null, vals);
    let span = hi - lo;
    const minSpan = 70;
    if (span < minSpan) {
      const mid = (hi + lo) / 2;
      lo = mid - minSpan / 2;
      hi = mid + minSpan / 2;
      span = minSpan;
    }

    let pad = Math.max(span * 0.04, 14);
    let min = lo - pad;
    let max = hi + pad;
    const coreLo = lo;
    const coreHi = hi;
    // Stretch the price scale to keep a strike on screen, up to this far from spot.
    // Farther strikes stay off the plot and get an edge tag instead of flattening Nifty.
    const maxStretch = 400;
    chart.$offscreenStrikes = [];

    function expandForStrikes(arr, visible, label, color) {
      if (!visible) return;
      let last = null;
      (arr || []).forEach(function (p) {
        if (!p || p.y == null || !isFinite(p.y) || p.y <= 0) return;
        last = p.y;
        if (p.y < coreLo - maxStretch || p.y > coreHi + maxStretch) return;
        if (p.y < min) min = p.y - pad;
        if (p.y > max) max = p.y + pad;
      });
      if (last == null) return;
      if (last > coreHi + maxStretch) {
        chart.$offscreenStrikes.push({
          side: "top",
          color: color,
          text: label + " " + Math.round(last) + " ↑",
        });
      } else if (last < coreLo - maxStretch) {
        chart.$offscreenStrikes.push({
          side: "bottom",
          color: color,
          text: label + " " + Math.round(last) + " ↓",
        });
      }
    }

    expandForStrikes(strikes?.vrCe, L.vrCe !== false, "VR CE", COLORS.vrCeStrike);
    expandForStrikes(strikes?.vrPe, L.vrPe !== false, "VR PE", COLORS.vrPeStrike);
    expandForStrikes(strikes?.otmCe, !!L.otmCe, "OTM CE", COLORS.otmCeStrike);
    expandForStrikes(strikes?.otmPe, !!L.otmPe, "OTM PE", COLORS.otmPeStrike);

    (chart.$userLevels || []).forEach(function (lv) {
      const price = Number(lv && lv.price);
      if (!isFinite(price) || price <= 0) return;
      const color = lv.color || "#0e7490";
      if (price > coreHi + maxStretch) {
        chart.$offscreenStrikes.push({ side: "top", color: color, text: "Level " + price.toFixed(1) + " ↑" });
        return;
      }
      if (price < coreLo - maxStretch) {
        chart.$offscreenStrikes.push({ side: "bottom", color: color, text: "Level " + price.toFixed(1) + " ↓" });
        return;
      }
      if (price < min) min = price - pad;
      if (price > max) max = price + pad;
    });

    scale.min = min;
    scale.max = max;
  }

  function refreshNiftyY1ScaleFromChart(chart) {
    if (!chart || !chart.data || !chart.data.datasets) return;
    const ds = chart.data.datasets;
    const orHigh = ds[4] && ds[4].data && ds[4].data[0] ? ds[4].data[0].y : null;
    const orLow = ds[5] && ds[5].data && ds[5].data[0] ? ds[5].data[0].y : null;
    tightenNiftyY1Scale(
      chart,
      ds[3] ? ds[3].data : [],
      { or_high: orHigh, or_low: orLow },
      {
        vrCe: ds[6] ? ds[6].data : [],
        vrPe: ds[7] ? ds[7].data : [],
        otmCe: ds[8] ? ds[8].data : [],
        otmPe: ds[9] ? ds[9].data : [],
      },
      readPremiumLayersFromDom()
    );
  }

  function tightenYScale(chart, scaleId, vals, padMin) {
    const scale = chart.options.scales[scaleId];
    if (!scale || !vals.length) {
      if (scale) {
        scale.min = undefined;
        scale.max = undefined;
      }
      return;
    }
    let lo = Math.min.apply(null, vals);
    let hi = Math.max.apply(null, vals);
    const pad = Math.max((hi - lo) * 0.04, padMin || 1);
    scale.min = lo - pad;
    scale.max = hi + pad;
  }

  function percentile(sorted, p) {
    if (!sorted.length) return NaN;
    const idx = (sorted.length - 1) * p;
    const lo = Math.floor(idx);
    const hi = Math.ceil(idx);
    if (lo === hi) return sorted[lo];
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (idx - lo);
  }

  function median(nums) {
    if (!nums.length) return NaN;
    const s = nums.slice().sort(function (a, b) {
      return a - b;
    });
    return percentile(s, 0.5);
  }

  /**
   * Premium scale uses the whole session, including the open.
   * Only a single print several times the session median is left out,
   * so one bad tick cannot flatten the line.
   */
  function collectPremiumScaleValues(vrPrem) {
    const allY = (vrPrem || [])
      .filter(function (p) {
        return p && p.y != null && isFinite(p.y) && p.y > 0;
      })
      .map(function (p) {
        return p.y;
      });
    if (allY.length <= 4) return allY;
    const med = median(allY);
    if (!(med > 0)) return allY;
    const kept = allY.filter(function (y) {
      return y <= med * 2.5 && y >= med * 0.35;
    });
    return kept.length >= 4 ? kept : allY;
  }

  function collectPremiumValues(vrPrem, putFlow) {
    return collectPremiumScaleValues(vrPrem);
  }

  function strikeSegmentDash(ceArr, peArr) {
    return {
      borderDash: function (ctx) {
        const i = ctx.p1DataIndex;
        if (i < 0 || !ceArr[i] || !peArr[i]) return undefined;
        const ce = ceArr[i].y;
        const pe = peArr[i].y;
        if (ce != null && pe != null && isFinite(ce) && isFinite(pe) && ce === pe) {
          return [6, 4];
        }
        return undefined;
      },
    };
  }

  function strikePoints(rows) {
    return rows || [];
  }

  /** First put-flow mark at or after 09:45. Later marks are dropped. */
  function firstPutFlowAfter945(points) {
    const cutoff = 9 * 60 + 45;
    for (let i = 0; i < (points || []).length; i++) {
      const p = points[i];
      if (!p || p.y == null || !isFinite(p.y) || p.y <= 0 || !p.x) continue;
      const d = p.x instanceof Date ? p.x : new Date(p.x);
      if (isNaN(d.getTime())) continue;
      if (d.getHours() * 60 + d.getMinutes() >= cutoff) return [p];
    }
    return [];
  }

  /**
   * Purple square on Nifty when the VR call−put volume gap is at least
   * double the previous reading, in the same direction.
   * Border is red when the call gap is larger (positive diff) and lime
   * when the put gap is larger (negative diff).
   * A near-zero previous print is skipped: it must be at least 30% of the
   * recent same-side median, so an opposite-side spike cannot hide a real double.
   * At most one mark per 5 minutes.
   */
  function volDoubleMarkers(nifty, diffs) {
    const out = [];
    const n = Math.min((nifty || []).length, (diffs || []).length);
    const recent = [];
    let lastMarkMs = null;
    for (let i = 1; i < n; i++) {
      const prevRaw = Number(diffs[i - 1]);
      const curRaw = Number(diffs[i]);
      const prev = Math.abs(prevRaw);
      const cur = Math.abs(curRaw);
      const sign = prevRaw > 0 ? 1 : prevRaw < 0 ? -1 : 0;
      const sameSide = [];
      for (let k = 0; k < recent.length; k++) {
        if (recent[k].sign === sign && recent[k].abs > 0) sameSide.push(recent[k].abs);
      }
      sameSide.push(prev);
      let nearZero = false;
      if (sameSide.length >= 3 && prev > 0) {
        sameSide.sort(function (a, b) { return a - b; });
        const median = sameSide[Math.floor(sameSide.length / 2)];
        nearZero = prev < median * 0.3;
      }
      recent.push({ abs: prev, sign: sign });
      if (recent.length > 15) recent.shift();
      if (!(prevRaw * curRaw > 0) || !(prev > 0) || nearZero) continue;
      if (!(cur >= prev * 2)) continue;
      const p = nifty[i];
      if (!p || p.y == null || !isFinite(p.y) || p.y <= 1000 || !p.x) continue;
      const d = p.x instanceof Date ? p.x : new Date(p.x);
      const ms = d.getTime();
      if (!isFinite(ms)) continue;
      if (lastMarkMs != null && ms - lastMarkMs < 5 * 60 * 1000) continue;
      lastMarkMs = ms;
      out.push({
        x: p.x,
        y: p.y,
        ratio: cur / prev,
        side: curRaw < 0 ? "pe" : "ce",
      });
    }
    return out;
  }

  /** 5-minute OHLC from the Nifty spot series already on the chart. */
  function buildFiveMinCandles(nifty) {
    const buckets = new Map();
    (nifty || []).forEach(function (p) {
      if (!p || p.y == null || !isFinite(p.y) || p.y <= 1000 || !p.x) return;
      const d = p.x instanceof Date ? p.x : new Date(p.x);
      if (isNaN(d.getTime())) return;
      const floored = new Date(
        d.getFullYear(),
        d.getMonth(),
        d.getDate(),
        d.getHours(),
        Math.floor(d.getMinutes() / 5) * 5,
        0,
        0
      );
      const key = floored.getTime();
      let bar = buckets.get(key);
      if (!bar) {
        buckets.set(key, { x: floored, o: p.y, h: p.y, l: p.y, c: p.y });
        return;
      }
      if (p.y > bar.h) bar.h = p.y;
      if (p.y < bar.l) bar.l = p.y;
      bar.c = p.y;
    });
    return Array.from(buckets.values()).sort(function (a, b) {
      return a.x - b.x;
    });
  }

  function candleContaining(candles, ts) {
    const t = ts instanceof Date ? ts.getTime() : Number(ts);
    if (!candles || !isFinite(t)) return null;
    for (let i = candles.length - 1; i >= 0; i--) {
      const start = candles[i].x instanceof Date ? candles[i].x.getTime() : Number(candles[i].x);
      if (t >= start && t < start + 5 * 60 * 1000) return candles[i];
    }
    return null;
  }

  function drawCandles(chart) {
    const candles = chart.$niftyCandles;
    if (!candles || !candles.length || chart.$showCandles === false) return;
    const xScale = chart.scales.x;
    const yScale = chart.scales.y1;
    if (!xScale || !yScale) return;
    const ctx = chart.ctx;
    const area = chart.chartArea;
    if (!area) return;
    ctx.save();
    ctx.beginPath();
    ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top);
    ctx.clip();
    candles.forEach(function (c) {
      const x = xScale.getPixelForValue(c.x);
      if (!isFinite(x) || x < area.left - 24 || x > area.right + 24) return;
      const xNext = xScale.getPixelForValue(c.x.getTime() + 5 * 60 * 1000);
      let bodyW = Math.abs(xNext - x) * 0.62;
      if (!isFinite(bodyW) || bodyW < 2.5) bodyW = 3;
      if (bodyW > 14) bodyW = 14;
      const yO = yScale.getPixelForValue(c.o);
      const yC = yScale.getPixelForValue(c.c);
      const yH = yScale.getPixelForValue(c.h);
      const yL = yScale.getPixelForValue(c.l);
      const up = c.c >= c.o;
      const ink = up ? COLORS.candleUp : COLORS.candleDown;
      ctx.strokeStyle = COLORS.candleHalo;
      ctx.lineWidth = 3.2;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(x, yH);
      ctx.lineTo(x, yL);
      ctx.stroke();
      ctx.strokeStyle = ink;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, yH);
      ctx.lineTo(x, yL);
      ctx.stroke();
      const top = Math.min(yO, yC);
      let h = Math.abs(yC - yO);
      if (h < 1.5) h = 1.5;
      const bx = x - bodyW / 2;
      ctx.fillStyle = ink;
      ctx.fillRect(bx, top, bodyW, h);
      ctx.lineWidth = 2.4;
      ctx.strokeStyle = COLORS.candleHalo;
      ctx.strokeRect(bx, top, bodyW, h);
    });
    ctx.restore();
  }

  function drawOffscreenStrikeTags(chart) {
    const tags = chart.$offscreenStrikes || [];
    const area = chart.chartArea;
    if (!tags.length || !area) return;
    const ctx = chart.ctx;
    function paint(list, fromTop) {
      list.forEach(function (t, i) {
        ctx.save();
        ctx.font = "700 11px Arial, sans-serif";
        const w = ctx.measureText(t.text).width + 12;
        const x = area.left + 8;
        const boxTop = fromTop ? area.top + 6 + i * 18 : area.bottom - 22 - i * 18;
        ctx.fillStyle = "rgba(255, 255, 255, 0.94)";
        ctx.strokeStyle = t.color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(x, boxTop, w, 16, 3);
        else ctx.rect(x, boxTop, w, 16);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = t.color;
        ctx.textBaseline = "middle";
        ctx.fillText(t.text, x + 6, boxTop + 8);
        ctx.restore();
      });
    }
    paint(tags.filter(function (t) { return t.side === "top"; }), true);
    paint(tags.filter(function (t) { return t.side === "bottom"; }), false);
  }

  function drawUserLevels(chart) {
    const levels = (chart.$userLevels || []).filter(function (lv) {
      return lv && isFinite(Number(lv.price));
    });
    const yScale = chart.scales.y1;
    const area = chart.chartArea;
    if (!levels.length || !yScale || !area) return;
    const ctx = chart.ctx;
    const placed = [];
    levels
      .slice()
      .sort(function (a, b) { return Number(a.price) - Number(b.price); })
      .forEach(function (lv) {
        const y = yScale.getPixelForValue(Number(lv.price));
        if (!isFinite(y) || y < area.top || y > area.bottom) return;
        const color = lv.color || "#0e7490";
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(area.left, y);
        ctx.lineTo(area.right, y);
        ctx.stroke();
        const text = Number(lv.price).toFixed(1);
        ctx.font = "700 11px Arial, sans-serif";
        const w = ctx.measureText(text).width + 10;
        const x = area.left + 8;
        let top = y - 8;
        placed.forEach(function (p) {
          if (Math.abs(p - top) < 16) top = p + 16;
        });
        if (top < area.top + 2) top = area.top + 2;
        if (top > area.bottom - 18) top = area.bottom - 18;
        placed.push(top);
        ctx.fillStyle = "rgba(255, 255, 255, 0.94)";
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.25;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(x, top, w, 16, 3);
        else ctx.rect(x, top, w, 16);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = color;
        ctx.textBaseline = "middle";
        ctx.fillText(text, x + 5, top + 8);
        ctx.restore();
      });
  }

  function drawArrowHead(ctx, x, tipY, dir, color) {
    const len = 18;
    const head = 7;
    const tailY = dir === "down" ? tipY - len : tipY + len;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = 2.25;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(x, tailY);
    ctx.lineTo(x, tipY);
    ctx.stroke();
    ctx.beginPath();
    if (dir === "down") {
      ctx.moveTo(x, tipY);
      ctx.lineTo(x - head, tipY - head);
      ctx.lineTo(x + head, tipY - head);
    } else {
      ctx.moveTo(x, tipY);
      ctx.lineTo(x - head, tipY + head);
      ctx.lineTo(x + head, tipY + head);
    }
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }

  function drawUserArrows(chart) {
    const arrows = chart.$userArrows || [];
    const candles = chart.$niftyCandles || [];
    const xScale = chart.scales.x;
    const yScale = chart.scales.y1;
    const area = chart.chartArea;
    if (!arrows.length || !candles.length || !xScale || !yScale || !area) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.rect(area.left, area.top, area.right - area.left, area.bottom - area.top);
    ctx.clip();
    arrows.forEach(function (a) {
      const t = Number(a.time);
      if (!isFinite(t)) return;
      let candle = null;
      let best = Infinity;
      for (let i = 0; i < candles.length; i++) {
        const d = Math.abs(candles[i].x.getTime() - t);
        if (d < best) {
          best = d;
          candle = candles[i];
        }
      }
      if (!candle || best > 6 * 60 * 1000) return;
      const x = xScale.getPixelForValue(candle.x);
      if (!isFinite(x)) return;
      const dir = a.dir === "down" ? "down" : "up";
      const price = dir === "down" ? candle.h : candle.l;
      let tip = yScale.getPixelForValue(price);
      if (!isFinite(tip)) return;
      tip = dir === "down" ? tip - 10 : tip + 10;
      drawArrowHead(ctx, x, tip, dir, a.color || (dir === "down" ? "#dc2626" : "#15803d"));
    });
    ctx.restore();
  }

  function drawCrosshair(chart) {
    const active = chart.getActiveElements ? chart.getActiveElements() : [];
    if (!active.length) return;
    const x = active[0].element && active[0].element.x;
    const area = chart.chartArea;
    if (!isFinite(x) || !area) return;
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = "rgba(17, 24, 39, 0.35)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, area.top);
    ctx.lineTo(x, area.bottom);
    ctx.stroke();
    ctx.restore();
  }

  /**
   * Front-most line dataset (drawn just before the signal dots).
   * Candles paint immediately after it, so bodies sit above Nifty, premium,
   * opening range, and strikes, while scatter markers stay on top.
   */
  function candleAnchorDatasetIndex(chart) {
    if (!chart.getSortedVisibleDatasetMetas) return null;
    const metas = chart.getSortedVisibleDatasetMetas();
    for (let i = 0; i < metas.length; i++) {
      const ds = chart.data.datasets[metas[i].index];
      if (!ds) continue;
      if (ds.type === "scatter" && (ds.order || 0) === 0) continue;
      return metas[i].index;
    }
    return null;
  }

  if (!window.__niftyChartPlugins) {
    window.__niftyChartPlugins = true;
    Chart.register(
      {
        id: "niftyCandles",
        afterDatasetDraw: function (chart, args) {
          if (!chart.canvas || chart.canvas.id !== "premiumChart") return;
          const anchor = candleAnchorDatasetIndex(chart);
          if (anchor == null || args.index !== anchor) return;
          drawCandles(chart);
        },
        afterDatasetsDraw: function (chart) {
          if (!chart.canvas || chart.canvas.id !== "premiumChart") return;
          if (candleAnchorDatasetIndex(chart) == null) drawCandles(chart);
        },
      },
      {
        id: "niftyCrosshair",
        afterDatasetsDraw: function (chart) {
          if (!chart.canvas || chart.canvas.id !== "premiumChart") return;
          drawUserLevels(chart);
          drawUserArrows(chart);
          drawOffscreenStrikeTags(chart);
          drawCrosshair(chart);
        },
      }
    );
  }

  function createNiftyCharts(opts) {
    const {
      premiumCanvasId,
      vrVolumeCanvasId,
      offsetVolumeCanvasId,
      volumeUnitLabel = "Lakhs",
    } = opts || {};

    const premiumCtx = document.getElementById(premiumCanvasId).getContext("2d");
    const vrVolCtx = document.getElementById(vrVolumeCanvasId).getContext("2d");
    const offVolCtx = document.getElementById(offsetVolumeCanvasId).getContext("2d");
    const baseGrid = { color: "#e5e7eb" };
    const tickColor = "#4b5563";
    const hover = {
      interaction: { mode: "index", intersect: false, axis: "x" },
      plugins: {
        tooltip: {
          mode: "index",
          intersect: false,
          animation: false,
          backgroundColor: "rgba(255, 255, 255, 0.97)",
          borderColor: "#d1d5db",
          borderWidth: 1,
          padding: 10,
          titleColor: "#111827",
          bodyColor: "#111827",
        },
      },
    };

    const strikeLine = function (label, color, dash, width) {
      return {
        label: label,
        data: [],
        yAxisID: "y1",
        borderColor: color,
        borderWidth: width || 1.5,
        borderDash: dash || [],
        stepped: "before",
        fill: false,
        tension: 0,
        pointRadius: 0,
        pointHoverRadius: 3,
        clip: true,
        order: 8,
      };
    };

    const tooltipRank = function (label) {
      switch (label) {
        case "Nifty": return 0;
        case "Vol Range Premium": return 1;
        case "OR High": return 2;
        case "OR Low": return 3;
        case "VR CE strike": return 4;
        case "VR PE strike": return 5;
        case "OTM Range Premium": return 6;
        case "OTM CE strike": return 7;
        case "OTM PE strike": return 8;
        default: return 20;
      }
    };

    const premiumChart = new Chart(premiumCtx, {
      type: "line",
      data: {
        datasets: [
          {
            label: "Vol Range Premium",
            data: [],
            yAxisID: "y",
            borderColor: COLORS.vrPremium,
            backgroundColor: COLORS.vrPremiumFill,
            fill: false,
            tension: 0.2,
            pointRadius: 0,
            pointHoverRadius: 4,
            borderWidth: 2.5,
            clip: true,
            order: 3,
          },
          {
            label: "OTM Range Premium",
            data: [],
            yAxisID: "y",
            borderColor: COLORS.otmPremium,
            fill: false,
            tension: 0.2,
            pointRadius: 0,
            borderWidth: 1.6,
            borderDash: [4, 3],
            hidden: true,
            order: 4,
          },
          {
            type: "scatter",
            label: "PUT flow (PE prem > CE prem)",
            data: [],
            yAxisID: "y",
            showLine: false,
            pointStyle: "circle",
            pointRadius: 9,
            pointHoverRadius: 11,
            pointBackgroundColor: COLORS.putFlowMark,
            pointBorderColor: "#111827",
            pointBorderWidth: 3,
            order: 0,
          },
          {
            label: "Nifty",
            data: [],
            yAxisID: "y1",
            borderColor: COLORS.nifty,
            fill: false,
            tension: 0.15,
            pointRadius: 0,
            pointHoverRadius: 4,
            borderWidth: 1.75,
            spanGaps: true,
            clip: true,
            order: 6,
          },
          {
            label: "OR High",
            data: [],
            yAxisID: "y1",
            borderColor: COLORS.orHigh,
            borderWidth: 2.5,
            borderDash: [8, 4],
            pointRadius: 0,
            fill: false,
            order: 1,
          },
          {
            label: "OR Low",
            data: [],
            yAxisID: "y1",
            borderColor: COLORS.orLow,
            borderWidth: 2.5,
            borderDash: [8, 4],
            pointRadius: 0,
            fill: false,
            order: 1,
          },
          strikeLine("VR CE strike", COLORS.vrCeStrike, [], 3),
          strikeLine("VR PE strike", COLORS.vrPeStrike, [], 3),
          Object.assign(strikeLine("OTM CE strike", COLORS.otmCeStrike, [3, 3]), { hidden: true }),
          Object.assign(strikeLine("OTM PE strike", COLORS.otmPeStrike, [3, 3]), { hidden: true }),
          {
            type: "scatter",
            label: "Vol diff ×2",
            data: [],
            yAxisID: "y1",
            showLine: false,
            pointStyle: "rect",
            pointRadius: 7,
            pointHoverRadius: 9,
            pointBackgroundColor: COLORS.volDouble,
            pointBorderColor: function (ctx) {
              const raw = ctx.raw;
              if (raw && raw.side === "pe") return COLORS.volDoublePe;
              if (raw && raw.side === "ce") return COLORS.volDoubleCe;
              return "#111827";
            },
            pointBorderWidth: 2.5,
            order: 0,
          },
        ],
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        ...hover,
        plugins: {
          legend: { display: false },
          tooltip: {
            mode: "index",
            intersect: false,
            animation: false,
            backgroundColor: "rgba(255, 255, 255, 0.97)",
            borderColor: "#d1d5db",
            borderWidth: 1,
            padding: 10,
            titleColor: "#111827",
            bodyColor: "#111827",
            usePointStyle: true,
            boxPadding: 3,
            filter: function (item) {
              const y = item.parsed && item.parsed.y;
              return y != null && isFinite(y);
            },
            itemSort: function (a, b) {
              return tooltipRank(a.dataset.label) - tooltipRank(b.dataset.label);
            },
            callbacks: {
              label: function (ctx) {
                const raw = ctx.raw;
                if (raw && typeof raw === "object" && raw.ratio) {
                  const side = raw.side === "pe" ? "PE larger" : "CE larger";
                  return "Vol diff " + Number(raw.ratio).toFixed(1) + "× previous · " + side;
                }
                const y = ctx.parsed && ctx.parsed.y;
                if (y == null || !isFinite(y)) return null;
                const name = ctx.dataset.label || "";
                if (ctx.dataset.yAxisID === "y") return name + ": ₹" + y.toFixed(1);
                return name + ": " + y.toFixed(1);
              },
              afterBody: function (items) {
                if (!items.length) return [];
                const chart = items[0].chart;
                if (!chart || chart.$showCandles === false) return [];
                const ts = items[0].parsed && items[0].parsed.x;
                const c = candleContaining(chart.$niftyCandles, ts);
                if (!c) return [];
                return [
                  "",
                  "5m candle  " + (c.c >= c.o ? "up" : "down"),
                  "O " + c.o.toFixed(1) + "   H " + c.h.toFixed(1),
                  "L " + c.l.toFixed(1) + "   C " + c.c.toFixed(1),
                ];
              },
            },
          },
        },
        scales: {
          x: marketTimeAxis(tickColor, baseGrid),
          y: {
            position: "right",
            ticks: { color: COLORS.vrPremium },
            grid: baseGrid,
            title: { display: true, text: "Premium (₹)", color: COLORS.vrPremium, font: { size: 11 } },
          },
          y1: {
            position: "left",
            ticks: { color: COLORS.nifty },
            grid: { drawOnChartArea: false },
            title: { display: true, text: "Nifty / Strikes", color: COLORS.nifty, font: { size: 11 } },
          },
        },
      },
    });

    function volumeChartOptions() {
      return {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        ...hover,
        plugins: { legend: { labels: { color: "#1f2937", font: { size: 10 } } }, ...hover.plugins },
        scales: {
          x: { ticks: { color: tickColor, maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: baseGrid },
          y: {
            ticks: { color: tickColor },
            grid: baseGrid,
            title: { display: true, text: volumeUnitLabel, color: tickColor, font: { size: 11 } },
          },
        },
      };
    }

    const vrVolumeChart = new Chart(vrVolCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "VR CE Volume",
            data: [],
            borderColor: COLORS.vrCeVolume,
            fill: false,
            tension: 0.2,
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: "VR PE Volume",
            data: [],
            borderColor: COLORS.vrPeVolume,
            fill: false,
            tension: 0.2,
            pointRadius: 0,
            borderWidth: 2,
          },
        ],
      },
      options: volumeChartOptions(),
    });

    const offsetVolumeChart = new Chart(offVolCtx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "Offset CE − PE",
            data: [],
            borderColor: COLORS.offsetVolume,
            backgroundColor: "rgba(56,189,248,0.12)",
            fill: false,
            tension: 0.2,
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: "VR CE − PE",
            data: [],
            borderColor: COLORS.vrVolDiff,
            backgroundColor: "rgba(212,225,87,0.1)",
            fill: false,
            tension: 0.2,
            pointRadius: 0,
            borderWidth: 2,
            borderDash: [4, 3],
          },
        ],
      },
      options: volumeChartOptions(),
    });

    return { premiumChart, vrVolumeChart, offsetVolumeChart, COLORS, toHHMM };
  }

  function applyNiftyPremiumChart(chart, data, orLevels) {
    const orSafe = {
      or_high: orLevels?.or_high > 0 ? orLevels.or_high : null,
      or_low: orLevels?.or_low > 0 ? orLevels.or_low : null,
    };
    const vrPrem = sanitizeSeries(data.vrPremium, 0);
    const nifty = sanitizeSeries(data.nifty, 1000);
    const putFlow = firstPutFlowAfter945(data.putFlowScatter);
    const volDoubles = volDoubleMarkers(nifty, data.vrVolDiffValues);
    const vrCeStrike = sanitizeSeries(data.vrCeStrike, 0);
    const vrPeStrike = sanitizeSeries(data.vrPeStrike, 0);
    const otmCeStrike = sanitizeSeries(data.otmCeStrike, 0);
    const otmPeStrike = sanitizeSeries(data.otmPeStrike, 0);
    const vrStrikeSeg = strikeSegmentDash(vrCeStrike, vrPeStrike);

    chart.data.datasets[0].data = vrPrem;
    chart.data.datasets[1].hidden = true;
    chart.data.datasets[2].data = putFlow;
    chart.data.datasets[3].data = nifty;
    chart.data.datasets[4].data = hLineOnNifty(nifty, orSafe.or_high);
    chart.data.datasets[5].data = hLineOnNifty(nifty, orSafe.or_low);
    chart.data.datasets[6].data = vrCeStrike;
    chart.data.datasets[6].spanGaps = true;
    chart.data.datasets[6].segment = vrStrikeSeg;
    chart.data.datasets[7].data = vrPeStrike;
    chart.data.datasets[7].spanGaps = true;
    chart.data.datasets[7].segment = vrStrikeSeg;
    chart.data.datasets[8].data = otmCeStrike;
    chart.data.datasets[8].spanGaps = true;
    chart.data.datasets[9].data = otmPeStrike;
    chart.data.datasets[9].spanGaps = true;
    chart.data.datasets[10].data = volDoubles;
    chart.$niftyCandles = buildFiveMinCandles(nifty);
    chart.$volDoubleCount = volDoubles.length;

    tightenYScale(chart, "y", collectPremiumValues(vrPrem, putFlow), 5);
    tightenNiftyY1Scale(
      chart,
      nifty,
      orSafe,
      {
        vrCe: vrCeStrike,
        vrPe: vrPeStrike,
        otmCe: otmCeStrike,
        otmPe: otmPeStrike,
      },
      readPremiumLayersFromDom()
    );
    applySessionXBounds(chart, nifty);

    // Tint premium line purple when latest point is PUT flow
    const lastSignal = data.putFlowActive === true;
    chart.data.datasets[0].borderColor = lastSignal ? COLORS.putFlow : COLORS.vrPremium;
    chart.data.datasets[0].backgroundColor = lastSignal
      ? "rgba(217,70,239,0.08)"
      : COLORS.vrPremiumFill;
    chart.data.datasets[0].fill = false;

    chart.update("none");
    return lastSignal;
  }

  /** Toggle premium-chart overlay datasets (indices fixed in createNiftyCharts). */
  function applyPremiumLayerVisibility(chart, layers) {
    if (!chart || !layers) return;
    const L = layers;
    if (L.vrPrem != null) chart.data.datasets[0].hidden = !L.vrPrem;
    chart.data.datasets[3].hidden = !L.nifty;
    chart.data.datasets[4].hidden = !L.or;
    chart.data.datasets[5].hidden = !L.or;
    chart.data.datasets[6].hidden = !L.vrCe;
    chart.data.datasets[7].hidden = !L.vrPe;
    chart.data.datasets[8].hidden = !L.otmCe;
    chart.data.datasets[9].hidden = !L.otmPe;
    refreshNiftyY1ScaleFromChart(chart);
    chart.update("none");
  }

  function readPremiumLayersFromDom() {
    const premEl = document.getElementById("ly-vr-prem");
    return {
      vrPrem: premEl ? !!premEl.checked : true,
      nifty: !!document.getElementById("ly-nifty")?.checked,
      or: !!document.getElementById("ly-or")?.checked,
      vrCe: !!document.getElementById("ly-vr-ce")?.checked,
      vrPe: !!document.getElementById("ly-vr-pe")?.checked,
      otmCe: !!document.getElementById("ly-otm-ce")?.checked,
      otmPe: !!document.getElementById("ly-otm-pe")?.checked,
    };
  }

  function setShowCandles(chart, visible) {
    if (!chart) return;
    chart.$showCandles = visible !== false;
    chart.update("none");
  }

  function bindPremiumLayerToggles(chart, onChange) {
    const ids = ["ly-vr-prem", "ly-nifty", "ly-or", "ly-vr-ce", "ly-vr-pe", "ly-otm-ce", "ly-otm-pe"];
    ids.forEach(function (id) {
      const el = document.getElementById(id);
      if (!el) return;
      el.addEventListener("change", function () {
        const layers = readPremiumLayersFromDom();
        applyPremiumLayerVisibility(chart, layers);
        if (onChange) onChange(layers);
      });
    });
    applyPremiumLayerVisibility(chart, readPremiumLayersFromDom());
  }

  function applyVolumeHalfHourTicks(chart, labels) {
    const x = chart.options.scales.x;
    if (!x) return;
    if (!x.ticks) x.ticks = {};
    x.ticks.maxRotation = 0;
    x.ticks.minRotation = 0;
    x.ticks.autoSkip = false;
    x.ticks.callback = function (_val, index) {
      const lbl = labels[index];
      if (!lbl) return "";
      const m = /^(\d{1,2}):(\d{2})$/.exec(String(lbl));
      if (!m) return lbl;
      const mins = parseInt(m[2], 10);
      return mins === 0 || mins === 30 ? lbl : "";
    };
  }

  function applyVrVolumeChart(chart, labels, ceValues, peValues, unit) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = ceValues;
    chart.data.datasets[1].data = peValues;
    const yVol = chart.options.scales.y;
    if (yVol && yVol.title) yVol.title.text = unit || "Lakhs";
    applyVolumeHalfHourTicks(chart, labels);
    chart.update("none");
  }

  function applyOffsetVolumeChart(chart, labels, offsetDiff, vrDiff, unit) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = offsetDiff;
    chart.data.datasets[1].data = vrDiff;
    const yVol = chart.options.scales.y;
    if (yVol && yVol.title) yVol.title.text = unit || "Lakhs";
    applyVolumeHalfHourTicks(chart, labels);
    chart.update("none");
  }

  function applyVolumeLineChart(chart, labels, values, unit) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = values;
    const yVol = chart.options.scales.y;
    if (yVol && yVol.title) yVol.title.text = unit || "Lakhs";
    chart.update("none");
  }

  function setUserArrows(chart, arrows) {
    if (!chart) return;
    chart.$userArrows = (arrows || [])
      .filter(function (a) {
        return a && isFinite(Number(a.time));
      })
      .map(function (a) {
        return {
          id: a.id,
          time: Number(a.time),
          dir: a.dir === "down" ? "down" : "up",
          color: a.color || (a.dir === "down" ? "#dc2626" : "#15803d"),
        };
      });
    chart.update("none");
  }

  function setUserLevels(chart, levels) {
    if (!chart) return;
    chart.$userLevels = (levels || [])
      .filter(function (lv) {
        return lv && isFinite(Number(lv.price)) && Number(lv.price) > 0;
      })
      .map(function (lv) {
        return {
          id: lv.id,
          price: Number(lv.price),
          color: lv.color || "#0e7490",
        };
      });
    refreshNiftyY1ScaleFromChart(chart);
    chart.update("none");
  }

  function setPutFlowChrome(active) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) {
      const m = document.createElement("meta");
      m.name = "theme-color";
      document.head.appendChild(m);
    }
    const theme = document.querySelector('meta[name="theme-color"]');
    if (active) {
      document.title = "PUT FLOW | Nifty Chart";
      if (theme) theme.content = "#6d28d9";
      document.body.classList.add("put-flow-active");
    } else {
      document.title = "Nifty Chart";
      if (theme) theme.content = "#f8fafc";
      document.body.classList.remove("put-flow-active");
    }
    const banner = document.getElementById("put-flow-banner");
    if (banner) banner.hidden = !active;
  }

  window.NiftyChartCore = {
    createNiftyCharts,
    applyNiftyPremiumChart,
    applyPremiumLayerVisibility,
    setShowCandles,
    setUserLevels,
    setUserArrows,
    bindPremiumLayerToggles,
    readPremiumLayersFromDom,
    applyVrVolumeChart,
    applyOffsetVolumeChart,
    applyVolumeLineChart,
    setPutFlowChrome,
    toHHMM,
    COLORS,
  };
})();
