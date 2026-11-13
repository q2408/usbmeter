/* USB Meter Dashboard 前端 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const SERIES = {
    v: { key: 'v', color: '#38bdf8', label: '电压', unit: 'V', dec: 4, axis: 2 },
    i: { key: 'i', color: '#fb923c', label: '电流', unit: 'A', dec: 4, axis: 3 },
    p: { key: 'p', color: '#4ade80', label: '功率', unit: 'W', dec: 3, axis: 1 },
    t: { key: 'temp', color: '#f472b6', label: '温度', unit: '°C', dec: 1, axis: 1 },
    r: { key: 'v', color: '#a78bfa', label: '纹波', unit: 'mV', dec: 1, axis: 3, ripple: true },
  };

  const CAP = 30 * 60 * 100; // 30 分钟 @100sps

  // 曲线的最小纵轴跨度，避免把几毫伏的噪声放大成“锯齿山”
  const MIN_SPAN = { v: 0.02, i: 0.01, p: 0.05, t: 0.2, r: 0.004 };

  class Ring {
    constructor(cap) {
      this.cap = cap;
      this.n = 0;
      this.start = 0;
      this.t = new Float64Array(cap);
      this.v = new Float32Array(cap);
      this.i = new Float32Array(cap);
      this.p = new Float32Array(cap);
      this.dp = new Float32Array(cap);
      this.dn = new Float32Array(cap);
      this.temp = new Float32Array(cap);
    }
    clear() { this.n = 0; this.start = 0; }
    push(t, v, i, dp, dn, temp) {
      if (this.n < this.cap) {
        const k = (this.start + this.n) % this.cap;
        this.t[k] = t; this.v[k] = v; this.i[k] = i; this.p[k] = v * i;
        this.dp[k] = dp; this.dn[k] = dn; this.temp[k] = temp;
        this.n++;
      } else {
        const k = this.start;
        this.t[k] = t; this.v[k] = v; this.i[k] = i; this.p[k] = v * i;
        this.dp[k] = dp; this.dn[k] = dn; this.temp[k] = temp;
        this.start = (this.start + 1) % this.cap;
      }
    }
    idx(i) { return (this.start + i) % this.cap; }
    at(i) { return this.idx(i); }
    lastT() { return this.n ? this.t[this.idx(this.n - 1)] : 0; }
    get(i, field) { return this[field][this.idx(i)]; }
    /** 第一个 t >= value 的逻辑下标 */
    lowerBound(value) {
      let lo = 0, hi = this.n;
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (this.t[this.idx(mid)] < value) lo = mid + 1; else hi = mid;
      }
      return lo;
    }
  }

  const ring = new Ring(CAP);

  const state = {
    paused: false,
    chartPaused: false,
    series: 'v',
    windowSec: 30,
    frozenT: 0,
    hover: null,
    device: null,
    stats: null,
    config: { alarms: { v: 28, i: 8, p: 224, t: 80 }, window: 30 },
    sound: true,
    alarmUntil: 0,
    lastBeep: 0,
    dirty: true,
    redraw: false,
    axis: null,
    rippleDc: 0,
    chartPts: null,
    hoverPt: null,
    sseOk: false,
  };

  /* ---------------------------------------------------------------- utils */
  const fmt = (n, d) => (Number.isFinite(n) ? n.toFixed(d) : '–');

  function durationText(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    const pad = (x) => String(x).padStart(2, '0');
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  function toast(text) {
    const el = $('toast');
    el.textContent = text;
    el.classList.add('show');
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.remove('show'), 2200);
  }

  async function api(path, body) {
    const opts = body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    };
    const res = await fetch(path, opts);
    if (!res.ok) {
      let msg = `${res.status}`;
      try {
        const data = await res.json();
        if (data && data.error) msg = data.error;
      } catch (_) { /* 忽略 */ }
      throw new Error(msg);
    }
    return res.json();
  }

  /* ------------------------------------------------------------ audio beep */
  let audioCtx = null;
  function beep() {
    if (!state.sound) return;
    const now = performance.now();
    if (now - state.lastBeep < 1500) return;
    state.lastBeep = now;
    try {
      audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.type = 'square';
      o.frequency.value = 880;
      g.gain.value = 0.0001;
      o.connect(g).connect(audioCtx.destination);
      const t = audioCtx.currentTime;
      g.gain.exponentialRampToValueAtTime(0.06, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.28);
      o.start(t);
      o.stop(t + 0.3);
    } catch (_) { /* 忽略 */ }
  }

  /* ---------------------------------------------------------------- chart */
  const canvas = $('chart');
  const ctx = canvas.getContext('2d');

  function niceTicks(min, max, count) {
    if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) {
      return { min: 0, max: 1, step: 0.25, ticks: [0, 0.25, 0.5, 0.75, 1] };
    }
    const raw = (max - min) / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm >= 5 ? 10 : norm >= 2.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
    const lo = Math.floor(min / step) * step;
    const hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step * 0.001; v += step) ticks.push(v);
    return { min: lo, max: hi, step, ticks };
  }

  function nearestPoint(pts, x) {
    let lo = 0, hi = pts.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (pts[mid].x < x) lo = mid + 1;
      else hi = mid;
    }
    if (lo > 0 && Math.abs(pts[lo - 1].x - x) <= Math.abs(pts[lo].x - x)) return pts[lo - 1];
    return pts[lo];
  }

  function drawChart() {
    const wrap = $('chartWrap');
    const W = wrap.clientWidth;
    const H = wrap.clientHeight;
    if (!W || !H) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
      canvas.width = Math.round(W * dpr);
      canvas.height = Math.round(H * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    const padL = 54, padR = 16, padT = 16, padB = 26;
    const plotW = Math.max(10, W - padL - padR);
    const plotH = Math.max(10, H - padT - padB);
    const conf = SERIES[state.series];

    if (ring.n < 2) {
      ctx.fillStyle = 'rgba(255,255,255,0.28)';
      ctx.font = '12px "Segoe UI", sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('等待数据…', padL + plotW / 2, padT + plotH / 2);
      $('chartRange').textContent = '–';
      return;
    }

    // 全局暂停时时间轴也冻结，避免“暂停了但曲线还在走”
    const frozen = state.chartPaused || state.paused;
    const tEnd = frozen ? state.frozenT : ring.lastT();
    let tStart = tEnd - state.windowSec;
    // 会话刚开始时不要留一大片空白：从第一个样本开始画
    const firstT = ring.get(0, 't');
    if (firstT > tStart) tStart = firstT;
    if (tStart >= tEnd) tStart = tEnd - 0.5;

    let i0 = ring.lowerBound(tStart);
    let i1 = ring.n;
    if (frozen) {
      const bound = ring.lowerBound(tEnd);
      i1 = Math.min(ring.n, Math.max(bound + 1, 1));
      if (i1 <= i0) i1 = Math.min(ring.n, i0 + 1);
    }
    if (i0 > 0) i0 -= 1;
    const count = i1 - i0;
    // 绘制抽样：可见点再多也只扫约 12000 个（每像素仍有多点参与取平均），
    // 30 分钟窗口从 18 万点降到 1.2 万点，重绘成本基本与窗口长度无关。
    const stride = Math.max(1, Math.ceil(count / 12000));

    const isRipple = !!conf.ripple;
    // 纹波模式：用“当前显示窗口末端”往前 2 秒的均值做直流基线。
    // 冻结时必须用冻结窗口的末端算，否则新数据会不断改变基线，整条曲线上下漂。
    let dc = 0;
    if (isRipple && i1 > i0) {
      const k0 = Math.max(i0, i1 - 200);
      let sum = 0;
      for (let k = k0; k < i1; k++) sum += ring.get(k, conf.key);
      dc = sum / Math.max(1, i1 - k0);
      state.rippleDc = dc;
    }
    const valAt = (i) => (isRipple ? ring.get(i, conf.key) - dc : ring.get(i, conf.key));

    let min = Infinity, max = -Infinity;
    for (let i = i0; i < i1; i += stride) {
      const val = valAt(i);
      if (val < min) min = val;
      if (val > max) max = val;
    }
    if (!Number.isFinite(min)) { min = 0; max = 1; }
    const minSpan = MIN_SPAN[state.series] || 0;
    if (max - min < minSpan) {
      const mid = (max + min) / 2;
      min = mid - minSpan / 2;
      max = mid + minSpan / 2;
    }
    if (max - min < 1e-9) { max = min + Math.max(Math.abs(min) * 0.02, 0.01); }

    // 纵轴做迟滞：只有数据越界才扩展，明显富余并持续一段时间才收缩，
    // 否则噪声在刻度边界来回蹦会让整条曲线每帧上下跳（看起来就是“闪”）。
    const padY = (max - min) * 0.12;
    let lo = min - padY;
    let hi = max + padY;
    if (!isRipple && lo < 0) lo = 0;
    if (isRipple) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    const now = performance.now();
    let ax = state.axis;
    if (!ax || ax.series !== state.series || !Number.isFinite(ax.min)) {
      ax = niceTicks(lo, hi, 5);
      state.axis = { series: state.series, min: ax.min, max: ax.max, ticks: ax.ticks, step: ax.step, shrinkSince: 0 };
    } else if (frozen) {
      // 冻结期间纵轴完全不动，悬停重绘也不会让画面整体上下跳
      ax.shrinkSince = 0;
    } else if (lo < ax.min || hi > ax.max) {
      // 一旦越界就多留 15% 余量，避免每个噪声尖峰都把比例改一次
      const span = Math.max(hi - lo, ax.max - ax.min);
      const next = niceTicks(Math.min(ax.min, lo) - span * 0.15, Math.max(ax.max, hi) + span * 0.15, 5);
      ax.min = next.min; ax.max = next.max; ax.ticks = next.ticks; ax.step = next.step;
      ax.shrinkSince = 0;
    } else if ((hi - lo) < (ax.max - ax.min) * 0.40) {
      if (!ax.shrinkSince) ax.shrinkSince = now;
      else if (now - ax.shrinkSince > 3000) {
        const next = niceTicks(lo, hi, 5);
        ax.min = next.min; ax.max = next.max; ax.ticks = next.ticks; ax.step = next.step;
        ax.shrinkSince = 0;
      }
    } else {
      ax.shrinkSince = 0;
    }
    const tick = { min: ax.min, max: ax.max, ticks: ax.ticks, step: ax.step };
    const yDec = isRipple ? 1 : Math.max(1, Math.min(4, Math.ceil(-Math.log10(tick.step))));

    const xOf = (t) => padL + ((t - tStart) / (tEnd - tStart || 1)) * plotW;
    const yOf = (val) => padT + plotH - ((val - tick.min) / (tick.max - tick.min)) * plotH;

    // 横向网格
    ctx.lineWidth = 1;
    ctx.font = '11px "Bahnschrift","Consolas",monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (const tv of tick.ticks) {
      const y = Math.round(yOf(tv)) + 0.5;
      ctx.strokeStyle = 'rgba(255,255,255,0.055)';
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(W - padR, y);
      ctx.stroke();
      ctx.fillStyle = 'rgba(160,178,198,0.62)';
      ctx.fillText(isRipple ? (tv * 1000).toFixed(yDec) : tv.toFixed(yDec), padL - 9, y);
    }

    // 纵向时间网格（以“现在”为锚点，按整秒/整分取刻度）
    const span = Math.max(0.001, tEnd - tStart);
    const candidates = [0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600];
    const stepT = candidates.find((s) => span / s <= 7) || 900;
    const firstRel = Math.ceil((tStart - tEnd) / stepT) * stepT;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let rel = firstRel; rel <= 1e-6; rel += stepT) {
      const x = Math.round(xOf(tEnd + rel)) + 0.5;
      ctx.strokeStyle = 'rgba(255,255,255,0.04)';
      ctx.beginPath();
      ctx.moveTo(x, padT);
      ctx.lineTo(x, padT + plotH);
      ctx.stroke();
      let label;
      if (Math.abs(rel) < 1e-6) {
        label = '现在';
      } else if (stepT >= 60) {
        const mm = Math.floor(Math.abs(rel) / 60);
        const ss = Math.round(Math.abs(rel) % 60);
        label = `-${mm}:${String(ss).padStart(2, '0')}`;
      } else if (stepT < 5) {
        label = `${rel.toFixed(1)}s`;
      } else {
        label = `${rel.toFixed(0)}s`;
      }
      ctx.fillStyle = 'rgba(140,160,180,0.5)';
      if (x >= W - padR - 2) {
        ctx.textAlign = 'right';
        ctx.fillText(label, x - 2, padT + plotH + 7);
        ctx.textAlign = 'center';
      } else {
        ctx.fillText(label, x, padT + plotH + 7);
      }
    }

    // 采样：按固定步长分桶，避免每来一包就把全图重新分桶（那会看起来一直抖）
    const cols = Math.max(1, Math.floor(plotW));
    const step = Math.max(1, Math.floor(count / cols));
    const buckets = Math.ceil(count / step);
    const pts = [];
    for (let c = 0; c < buckets; c++) {
      const a = i0 + c * step;
      if (a >= i1) break;
      const b = Math.min(i1, a + step);
      let sum = 0, tn = 0;
      for (let i = a; i < b; i += stride) {
        sum += valAt(i);
        tn++;
      }
      if (!tn) continue;
      const avg = sum / tn;
      const tMid = ring.get(a + ((b - a - 1) >> 1), 't');
      pts.push({ x: xOf(tMid), y: yOf(avg), val: avg, t: tMid });
    }
    if (!pts.length) return;
    // 记录本帧真正画出来的点，悬停圆点/读数都用它，保证一定落在线上
    state.chartPts = pts;

    // 面积（纹波模式填充到 0 轴）
    const baseY = isRipple ? yOf(0) : padT + plotH;
    const grad = ctx.createLinearGradient(0, padT, 0, padT + plotH);
    grad.addColorStop(0, hexA(conf.color, 0.30));
    grad.addColorStop(1, hexA(conf.color, 0));
    ctx.beginPath();
    ctx.moveTo(pts[0].x, baseY);
    for (const pt of pts) ctx.lineTo(pt.x, pt.y);
    ctx.lineTo(pts[pts.length - 1].x, baseY);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    if (isRipple) {
      const zy = yOf(0);
      ctx.strokeStyle = 'rgba(255,255,255,0.16)';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(padL, zy);
      ctx.lineTo(W - padR, zy);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // 不再画 min/max 包络：长窗口下个别异常采样（掉到 0 或尖峰）会被拉成
    // 贯穿整个绘图区的竖条，看起来像“梳子”。曲线只画每列的平均值，
    // 峰值信息仍然保留在右侧的「峰值/极值」里。

    // 曲线：只用一条 2px 的实线。之前外面那圈宽描边在尖峰处会被拉成
    // “长辉光”，这里去掉，只保留干净的单线。
    ctx.beginPath();
    pts.forEach((pt, i) => (i ? ctx.lineTo(pt.x, pt.y) : ctx.moveTo(pt.x, pt.y)));
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.strokeStyle = conf.color;
    ctx.lineWidth = 2;
    ctx.stroke();

    // 最新点（冻结时画在冻结窗口末端，不能用还在增长的最新样本）
    const last = pts[pts.length - 1];
    if (!frozen && last) {
      const tLast = ring.lastT();
      const lx = xOf(tLast) - 2;
      const ly = yOf(valAt(ring.n - 1));
      ctx.beginPath();
      ctx.arc(lx, ly, 3.4, 0, Math.PI * 2);
      ctx.fillStyle = conf.color;
      ctx.fill();
      ctx.beginPath();
      ctx.arc(lx, ly, 7.5, 0, Math.PI * 2);
      ctx.fillStyle = hexA(conf.color, 0.18);
      ctx.fill();
    }

    // 悬停十字线
    if (state.hover && state.hover.x >= padL && state.hover.x <= W - padR) {
      const hx = state.hover.x;
      const hp = nearestPoint(pts, hx);
      state.hoverPt = hp;
      ctx.strokeStyle = 'rgba(255,255,255,0.22)';
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, padT + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      if (hp) {
        ctx.beginPath();
        ctx.arc(hp.x, hp.y, 4, 0, Math.PI * 2);
        ctx.fillStyle = conf.color;
        ctx.fill();
        ctx.beginPath();
        ctx.arc(hp.x, hp.y, 8, 0, Math.PI * 2);
        ctx.fillStyle = hexA(conf.color, 0.16);
        ctx.fill();
      }
    } else {
      state.hoverPt = null;
    }

    $('chartRange').textContent = isRipple
      ? `范围 ${(tick.min * 1000).toFixed(1)} – ${(tick.max * 1000).toFixed(1)} mV · ${ring.n} 点`
      : `范围 ${tick.min.toFixed(conf.axis)} – ${tick.max.toFixed(conf.axis)} ${conf.unit} · ${ring.n} 点`;
  }

  function hexA(hex, a) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  /* ----------------------------------------------------------- sparklines */
  function drawSparks() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    for (const key of ['v', 'i', 'p', 't']) {
      const cv = $(`spark-${key}`);
      if (!cv) continue;
      const w = 108, h = 36;
      if (cv.width !== w * dpr) {
        cv.width = w * dpr;
        cv.height = h * dpr;
      }
      const c = cv.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, w, h);
      if (ring.n < 3) continue;
      const conf = SERIES[key];
      const tEnd = ring.lastT();
      const start = ring.lowerBound(tEnd - 30);
      const n = ring.n - start;
      if (n < 2) continue;
      // 趋势线也按设置里的平滑时间常数过滤一遍，避免小图上全是毛刺
      const tau = Number(state.config.smooth) || 0;
      const alpha = tau > 0 ? 1 - Math.exp(-0.01 / tau) : 1;
      const vals = new Float32Array(n);
      let ema = null;
      for (let k = 0; k < n; k++) {
        const v = ring.get(start + k, conf.key);
        ema = ema === null ? v : ema + alpha * (v - ema);
        vals[k] = ema;
      }
      let mn = Infinity, mx = -Infinity;
      for (let k = 0; k < n; k++) {
        if (vals[k] < mn) mn = vals[k];
        if (vals[k] > mx) mx = vals[k];
      }
      let span = mx - mn || 1;
      const minSpan = MIN_SPAN[key] || 0;
      if (span < minSpan) {
        const mid = (mx + mn) / 2;
        mn = mid - minSpan / 2;
        mx = mid + minSpan / 2;
        span = mx - mn;
      }
      const stepPx = Math.max(1, Math.floor(n / w));
      c.beginPath();
      let first = true;
      for (let k = 0; k < n; k += stepPx) {
        const x = (k / n) * w;
        const y = h - 4 - ((vals[k] - mn) / span) * (h - 8);
        if (first) { c.moveTo(x, y); first = false; } else c.lineTo(x, y);
      }
      c.strokeStyle = hexA(conf.color, 0.85);
      c.lineWidth = 1.4;
      c.stroke();
      c.lineTo(w, h); c.lineTo(0, h); c.closePath();
      const g = c.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, hexA(conf.color, 0.22));
      g.addColorStop(1, hexA(conf.color, 0));
      c.fillStyle = g;
      c.fill();
    }
  }

  /* -------------------------------------------------------------- updates */
  function lastSample() {
    if (!ring.n) return null;
    const i = ring.n - 1;
    return {
      t: ring.get(i, 't'), v: ring.get(i, 'v'), i: ring.get(i, 'i'),
      p: ring.get(i, 'p'), dp: ring.get(i, 'dp'), dn: ring.get(i, 'dn'),
      temp: ring.get(i, 'temp'),
    };
  }

  function updateKPIs() {
    const s = lastSample();
    const st = state.stats;
    const unit = state.config.temperature_unit === 'F' ? '°F' : '°C';
    const conv = (c) => (state.config.temperature_unit === 'F' ? c * 9 / 5 + 32 : c);

    // 平滑值来自服务端（同一份数据也用于控制台窗口），没有时退回原始采样
    const sm = st && st.smooth;
    const sv = sm ? sm.v : s ? s.v : 0;
    const si = sm ? sm.i : s ? s.i : 0;
    const sp = sm ? sm.p : s ? s.p : 0;
    const stemp = sm ? sm.temp : s ? s.temp : 0;

    $('val-v').textContent = s || sm ? fmt(sv, 4) : '0.0000';
    $('val-i').textContent = s || sm ? fmt(si, 4) : '0.0000';
    $('val-p').textContent = s || sm ? fmt(sp, 3) : '0.000';
    $('val-t').textContent = s || sm ? fmt(conv(stemp), 1) : '0.0';
    $('kpi-t').querySelector('u').textContent = unit;

    if (st) {
      $('min-v').textContent = fmt(st.min_v, 3);
      $('max-v').textContent = fmt(st.max_v, 3);
      $('min-i').textContent = fmt(st.min_i, 3);
      $('max-i').textContent = fmt(st.max_i, 3);
      $('peak-p').textContent = fmt(st.peak_power, 2) + ' W';
      $('avg-p').textContent = fmt(st.avg_p, 2) + ' W';
      $('val-r').textContent = si > 1e-4 ? fmt(sv / si, 3) + ' Ω' : '–';
      const heat = stemp;
      $('val-heat').textContent = heat < 40 ? '正常' : heat < 55 ? '偏热' : '过热';
      $('val-heat').style.color = heat < 40 ? 'var(--ok)' : heat < 55 ? 'var(--warn)' : 'var(--bad)';

      $('cap').textContent = fmt(st.capacity_ah, 4);
      $('nrg').textContent = fmt(st.energy_wh, 4);
      $('peakw').textContent = fmt(st.peak_power, 2);
      $('duration').textContent = durationText(st.duration);

      $('sv-min').textContent = fmt(st.min_v, 3);
      $('sv-avg').textContent = fmt(st.avg_v, 3);
      $('sv-max').textContent = fmt(st.max_v, 3);
      $('si-min').textContent = fmt(st.min_i, 3);
      $('si-avg').textContent = fmt(st.avg_i, 3);
      $('si-max').textContent = fmt(st.max_i, 3);
      $('sp-avg').textContent = fmt(st.avg_p, 2);
      $('sp-max').textContent = fmt(st.peak_power, 2);
      $('packets').textContent = st.packets;
      $('crc').textContent = st.crc_errors;
      $('csvName').textContent = st.csv ? `记录文件：${st.csv}` : '未创建记录文件';
      $('recState').classList.toggle('off', !st.recording);
      $('recState').lastChild.textContent = st.recording ? '记录中' : '未记录（点击开始）';
    }

    $('dp').textContent = s ? `${fmt(s.dp, 3)} V` : '–';
    $('dn').textContent = s ? `${fmt(s.dn, 3)} V` : '–';
    $('res').textContent = si > 1e-4 ? `${fmt(sv / si, 4)} Ω` : '–';

    const rp = (state.stats && state.stats.ripple) || {};
    $('rippleVpp').textContent = rp.n ? `${fmt(rp.vpp * 1000, 2)} mV` : '–';
    $('rippleVrms').textContent = rp.n ? `${fmt(rp.vrms * 1000, 2)} mV` : '–';
    $('temp').textContent = s || sm ? `${fmt(conv(stemp), 1)} ${unit}` : '–';

    const rate = state.device ? state.device.rate : 0;
    $('ratePill').textContent = `${rate.toFixed(0)} sps`;

    checkAlarms(s);
  }

  function checkAlarms(s) {
    if (!s) return;
    const al = state.config.alarms || {};
    const hits = [];
    if (s.v > al.v) hits.push({ key: 'v', text: `过压 ${fmt(s.v, 2)}V` });
    if (s.i > al.i) hits.push({ key: 'i', text: `过流 ${fmt(s.i, 2)}A` });
    if (s.p > al.p) hits.push({ key: 'p', text: `过功率 ${fmt(s.p, 1)}W` });
    if (s.temp > al.t) hits.push({ key: 't', text: `过温 ${fmt(s.temp, 0)}°C` });

    for (const key of ['v', 'i', 'p', 't']) {
      $(`kpi-${key}`).classList.toggle('alarm', hits.some((h) => h.key === key));
    }
    const foot = document.querySelector('.foot');
    foot.classList.toggle('alarm', hits.length > 0);
    $('alarmText').textContent = hits.length ? hits.map((h) => h.text).join(' · ') : '报警未触发';
    if (hits.length) beep();
  }

  function updateConnection() {
    const d = state.device;
    const conn = $('conn');
    const nameEl = $('devName');
    const subEl = $('devSub');
    if (!d) {
      conn.dataset.state = 'offline';
      nameEl.textContent = '正在连接服务…';
      subEl.textContent = '';
      return;
    }
    if (d.connected) {
      conn.dataset.state = 'online';
      nameEl.textContent = d.model || 'USB 功率计';
      const sn = d.serial ? `SN ${d.serial}` : '';
      subEl.textContent = [sn, `${(d.rate || 0).toFixed(0)} sps`].filter(Boolean).join(' · ');
    } else {
      conn.dataset.state = 'offline';
      nameEl.textContent = d.model ? `${d.model} · 未连接` : '未检测到设备';
      subEl.textContent = d.message || '';
    }
    const showEmpty = !d.connected;
    $('emptyState').hidden = !showEmpty;
    setEmptyTitle(d);
    if (d.error) $('emptyNote').textContent = d.error;
    else if (d.message) $('emptyNote').textContent = d.message;
  }

  function setEmptyTitle(d) {
    const h = $('emptyTitle');
    if (!h) return;
    h.textContent = '';
    if (d && d.model && !d.connected) {
      h.append('已找到 ');
      const s = document.createElement('span');
      s.textContent = d.model;
      h.append(s);
      h.append('，但连接失败');
    } else {
      h.append('未检测到 ');
      const s = document.createElement('span');
      s.textContent = 'USB 功率计';
      h.append(s);
    }
  }

  /* --------------------------------------------------------------- SSE */
  let sse = null;
  function connectSSE() {
    if (sse) sse.close();
    sse = new EventSource('/api/stream');
    sse.onopen = () => { state.sseOk = true; };
    sse.onerror = () => {
      state.sseOk = false;
      $('conn').dataset.state = 'offline';
      $('devName').textContent = '服务连接中断';
      $('devSub').textContent = '正在重试…';
    };
    sse.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === 'data') {
        for (const row of msg.s) ring.push(row[0], row[1], row[2], row[3], row[4], row[5]);
        if (!state.paused) state.dirty = true;
      } else if (msg.type === 'stats') {
        state.stats = msg.stats;
        state.device = msg.device;
        updateConnection();
      } else if (msg.type === 'hello') {
        state.device = msg.device;
        state.stats = msg.stats;
        if (msg.config) {
          state.config = msg.config;
          applyConfig();
        }
        updateConnection();
      }
    };
  }

  function applyConfig() {
    const al = state.config.alarms || {};
    $('set-v').value = al.v;
    $('set-i').value = al.i;
    $('set-p').value = al.p;
    $('set-t').value = al.t;
    $('set-smooth').value = String(state.config.smooth ?? 0);
    $('set-port').value = location.port || '80';
    if (state.config.window) setWindow(state.config.window, false);
    const unit = state.config.temperature_unit === 'F' ? 'F' : 'C';
    if (typeof updateKPIs === 'function') updateKPIs();
    void unit;
  }

  /* ------------------------------------------------------------- controls */
  function setPaused(v) {
    state.paused = v;
    if (v) state.frozenT = ring.lastT();
    $('btnPause').classList.toggle('on', v);
    $('btnPause').querySelector('span').textContent = v ? '继续' : '暂停';
    if (!v) { state.dirty = true; state.redraw = true; }
  }

  function setChartPaused(v) {
    state.chartPaused = v;
    if (v) state.frozenT = ring.lastT();
    $('chartBadge').hidden = !v;
    $('btnChartPause').classList.toggle('on', v);
    state.redraw = true;
  }

  function setSeries(key) {
    state.series = key;
    document.querySelectorAll('#chartTabs button').forEach((b) => {
      b.classList.toggle('on', b.dataset.series === key);
    });
    $('chartHint').textContent = key === 'r'
      ? '纹波 = 去直流后的交流分量（100 sps，≤50 Hz；USB 数据本身已平均，开关纹波通常只剩 1 mV 以下；真实纹波请看表的高速纹波模式）'
      : '把鼠标移到曲线上查看读数';
    state.dirty = true;
  }

  function setWindow(sec, save = true) {
    state.windowSec = sec;
    document.querySelectorAll('#windowSeg button').forEach((b) => {
      b.classList.toggle('on', Number(b.dataset.win) === sec);
    });
    state.dirty = true;
    if (save) api('/api/settings', { window: sec }).catch(() => {});
  }

  document.querySelectorAll('#chartTabs button').forEach((b) => {
    b.addEventListener('click', () => setSeries(b.dataset.series));
  });
  document.querySelectorAll('#windowSeg button').forEach((b) => {
    b.addEventListener('click', () => setWindow(Number(b.dataset.win)));
  });

  $('btnChartPause').addEventListener('click', () => setChartPaused(!state.chartPaused));
  $('btnPause').addEventListener('click', () => setPaused(!state.paused));

  $('btnNew').addEventListener('click', async () => {
    try {
      await api('/api/session/new', {});
      ring.clear();
      state.stats = null;
      state.dirty = true;
      toast('已开始新的记录会话');
    } catch (_) { toast('操作失败'); }
  });

  $('btnExport').addEventListener('click', () => {
    if (!state.stats || !state.stats.csv) {
      toast('还没有记录文件：先点右侧「未记录（点击开始）」');
      return;
    }
    window.location.href = '/api/export';
    toast('正在导出 CSV…');
  });

  $('recState').addEventListener('click', async () => {
    const next = !(state.stats && state.stats.recording);
    try {
      await api('/api/record', { recording: next });
      toast(next ? '开始记录 CSV' : '已停止记录，图表继续刷新');
    } catch (_) {
      toast('无法开始记录：程序目录不可写或服务异常');
    }
  });

  $('btnRetry').addEventListener('click', async () => {
    try {
      await api('/api/rescan', {});
      toast('正在重新扫描 USB 设备…');
    } catch (_) { toast('重试失败'); }
  });

  /* settings */
  $('btnSettings').addEventListener('click', () => {
    applyConfig();
    $('set-sound').checked = state.sound;
    $('set-privacy').checked = !!state.config.privacy;
    $('settingsModal').hidden = false;
  });
  $('btnCloseSettings').addEventListener('click', () => { $('settingsModal').hidden = true; });
  $('btnCancelSettings').addEventListener('click', () => { $('settingsModal').hidden = true; });
  $('settingsModal').addEventListener('click', (e) => {
    if (e.target === $('settingsModal')) $('settingsModal').hidden = true;
  });
  $('btnSaveSettings').addEventListener('click', async () => {
    const alarms = {
      v: Number($('set-v').value) || 0,
      i: Number($('set-i').value) || 0,
      p: Number($('set-p').value) || 0,
      t: Number($('set-t').value) || 0,
    };
    state.sound = $('set-sound').checked;
    localStorage.setItem('usbmd.sound', state.sound ? '1' : '0');
    const wantPort = Number($('set-port').value) || 0;
    const curPort = Number(location.port || 80);
    try {
      const res = await api('/api/settings', {
        alarms,
        smooth: Number($('set-smooth').value) || 0,
        privacy: $('set-privacy').checked,
      });
      state.config = res.config;
      if (wantPort && wantPort !== curPort) {
        const changed = await api('/api/port', { port: wantPort });
        toast(`端口已切换到 ${changed.port}，正在跳转…`);
        setTimeout(() => {
          location.href = `${location.protocol}//${location.hostname}:${changed.port}${location.pathname}`;
        }, 700);
        return;
      }
      $('settingsModal').hidden = true;
      toast('设置已保存');
    } catch (err) {
      toast(`保存失败：${err.message}`);
    }
  });

  /* hover */
  const wrap = $('chartWrap');
  wrap.addEventListener('mousemove', (e) => {
    const rect = wrap.getBoundingClientRect();
    state.hover = { x: e.clientX - rect.left, y: e.clientY - rect.top };
    state.redraw = true;
    updateCrosshair(rect);
  });
  wrap.addEventListener('mouseleave', () => {
    state.hover = null;
    $('crosshair').hidden = true;
    state.redraw = true;
  });

  function updateCrosshair(rect) {
    const conf = SERIES[state.series];
    const padL = 54, padR = 16, padT = 16, padB = 26;
    const W = rect.width, H = rect.height;
    const x = state.hover.x, y = state.hover.y;
    if (x < padL || x > W - padR || y < padT || y > H - padB || ring.n < 2) {
      $('crosshair').hidden = true;
      return;
    }
    // 用“本帧真正画出来的点”取读数，圆点也在线上，不会跳到原始尖峰上
    const pts = state.chartPts;
    if (!pts || !pts.length) {
      $('crosshair').hidden = true;
      return;
    }
    const p = nearestPoint(pts, x);
    const tEnd = (state.chartPaused || state.paused) ? state.frozenT : ring.lastT();
    const el = $('crosshair');
    el.hidden = false;
    el.style.left = `${Math.min(Math.max(x, 60), W - 60)}px`;
    el.style.top = `${y}px`;
    $('chTime').textContent = `${(p.t - tEnd).toFixed(2)} s`;
    $('chVal').textContent = conf.ripple
      ? `${conf.label} ${fmt(p.val * 1000, 1)} mV`
      : `${conf.label} ${fmt(p.val, conf.dec)} ${conf.unit}`;
  }

  /* keyboard */
  window.addEventListener('keydown', (e) => {
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.code === 'Space') { e.preventDefault(); setPaused(!state.paused); }
    else if (e.key === 'n' || e.key === 'N') $('btnNew').click();
    else if (e.key === 'e' || e.key === 'E') $('btnExport').click();
    else if (e.key === 'd' || e.key === 'D') window.open('/debug', '_blank');
  });

  /* ------------------------------------------------------------ main loop */
  let lastKpi = 0, lastSpark = 0, lastDraw = 0;
  function frame(ts) {
    if (!state.paused) {
      const wantDraw = state.redraw || (state.dirty && !state.chartPaused);
      // 10m/30m 这种长窗口数据量大，降频到 5 fps 重画，避免拖窗口/移动鼠标时卡顿
      const tooSoon = state.windowSec >= 600 && !state.redraw && ts - lastDraw < 200;
      if (wantDraw && !tooSoon) {
        drawChart();
        state.dirty = false;
        state.redraw = false;
        lastDraw = ts;
      }
      if (ts - lastKpi > 100) {
        lastKpi = ts;
        updateKPIs();
      }
      if (ts - lastSpark > 400) {
        lastSpark = ts;
        drawSparks();
      }
    } else if (state.redraw) {
      // 暂停时只响应鼠标/窗口尺寸变化，不再跟着新数据重画
      drawChart();
      state.redraw = false;
      state.dirty = false;
    }
    requestAnimationFrame(frame);
  }

  /* ----------------------------------------------------------------- init */
  function boot() {
    const saved = localStorage.getItem('usbmd.sound');
    if (saved !== null) state.sound = saved === '1';
    connectSSE();
    requestAnimationFrame(frame);
    window.addEventListener('resize', () => { state.redraw = true; });
    setTimeout(() => {
      if (!state.device) {
        $('emptyNote').textContent = '长时间没有反应时，可以打开「诊断」页面查看设备状态和日志。';
      }
    }, 4000);
  }

  boot();
})();
