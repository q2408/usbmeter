/* USB Meter Dashboard 诊断页 */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  let last = null;
  let timer = null;

  const esc = (s) => String(s == null ? '' : s);

  function hhmmss(ts) {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
  }

  function fmtUptime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    return h ? `${h}h ${m}m ${s}s` : `${m}m ${s}s`;
  }

  function pick(id, text, cls) {
    const el = $(id);
    el.textContent = text;
    el.className = cls || '';
  }

  function render(d) {
    const info = d.info || {};
    const env = d.env || {};
    const session = d.session || {};

    $('connPill').textContent = info.connected ? '已连接' : '未连接';
    $('connPill').style.color = info.connected ? 'var(--ok)' : 'var(--muted)';
    pick('kModel', info.model || '—', info.model ? 'ok' : '');
    pick('kSerial', info.serial || '—');
    pick('kRate', `${(info.rate || 0).toFixed(0)} sps`);
    pick('kPackets', `${session.packets || 0} 包 / ${session.count || 0} 采样，CRC 异常 ${session.crc_errors || 0}`);
    const en = d.enum || {};
    pick(
      'kEnum',
      `native=${en.native ?? '?'}  hidapi=${en.hidapi ?? '?'}${en.error ? '  错误: ' + en.error : ''}`,
      en.error ? 'err' : ''
    );
    pick('kUptime', fmtUptime(info.uptime));
    pick('kError', info.error || '无', info.error ? 'err' : '');
    pick('kMessage', info.message || '—');

    pick('kPython', env.python || '—');
    pick('kExe', env.executable || '—');
    pick('kSystem', `${env.platform || '—'} / ${env.machine || ''}`);
    pick('kRoot', (d.paths || {}).root || '—');
    pick('kData', (d.paths || {}).data || '—');
    pick('kPid', String(d.pid || '—'));

    // devices
    const devices = d.devices || [];
    $('devCount').textContent = d.hid_total
      ? `${devices.length} 个仪表（共 ${d.hid_total} 个 HID，其他已隐藏）`
      : `${devices.length} 个`;
    const body = $('devBody');
    body.textContent = '';
    for (const dev of devices) {
      const tr = document.createElement('tr');
      if (dev.model) tr.className = 'hit';
      const vid = (dev.vendor_id || 0).toString(16).toUpperCase().padStart(4, '0');
      const pid = (dev.product_id || 0).toString(16).toUpperCase().padStart(4, '0');
      const idText = dev.vendor_id || dev.product_id ? `${vid}:${pid}` : '****:****';
      const cells = [
        idText,
        dev.model ? `${dev.product || ''}  [${dev.model}]` : (dev.product || '—'),
        dev.manufacturer || '—',
        dev.serial || '—',
        `${dev.usage_page ?? '?'}/${dev.usage ?? '?'}`,
        dev.reports ? `${dev.reports.input}/${dev.reports.output}/${dev.reports.feature}` : '—',
        dev.path || '',
      ];
      cells.forEach((text, i) => {
        const td = document.createElement('td');
        td.textContent = esc(text);
        if (i === 6) td.className = 'path';
        tr.append(td);
      });
      body.append(tr);
    }

    // events
    const events = d.events || [];
    const ev = $('events');
    ev.textContent = '';
    for (const e of events) {
      const row = document.createElement('div');
      row.className = `event ${e.level || 'info'}`;
      const t = document.createElement('time');
      t.textContent = hhmmss(e.t);
      const s = document.createElement('span');
      s.textContent = e.text;
      row.append(t, s);
      ev.append(row);
    }
    ev.scrollTop = ev.scrollHeight;

    // raw
    const raw = (d.raw || []).slice().reverse();
    $('rawCount').textContent = `${d.raw_total || 0} 包`;
    const list = $('rawList');
    list.textContent = '';
    for (const r of raw) {
      const box = document.createElement('div');
      box.className = 'raw';
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = `${hhmmss(r.t)} · ${r.len} 字节 · ${r.kind || 'data'} · CRC ${r.crc ? 'OK' : 'BAD'}`;
      const hex = document.createElement('div');
      hex.className = 'hex';
      hex.textContent = r.hex || '';
      const vals = document.createElement('div');
      vals.className = 'vals';
      vals.innerHTML = `V <b>${(r.v ?? 0).toFixed(4)}</b>  I <i>${(r.i ?? 0).toFixed(4)}</i>  ` +
        `D+ ${(r.dp ?? 0).toFixed(2)}  D- ${(r.dn ?? 0).toFixed(2)}  T ${(r.temp ?? 0).toFixed(1)}`;
      box.append(meta, hex, vals);
      list.append(box);
    }

    $('updated').textContent = `更新于 ${new Date().toLocaleTimeString()}`;
  }

  async function refresh() {
    try {
      const res = await fetch('/api/debug', { cache: 'no-store' });
      if (!res.ok) throw new Error(res.status);
      last = await res.json();
      render(last);
      document.body.classList.remove('stale');
    } catch (err) {
      $('updated').textContent = `刷新失败: ${err.message}`;
      document.body.classList.add('stale');
    }
  }

  function report() {
    if (!last) return '暂无数据';
    const info = last.info || {};
    const env = last.env || {};
    const lines = [];
    lines.push('USB Meter Dashboard 诊断信息');
    lines.push('=====================');
    lines.push(`时间: ${new Date().toISOString()}`);
    lines.push(`连接: ${info.connected ? '已连接' : '未连接'}  型号: ${info.model || '-'}  SN: ${info.serial || '-'}`);
    lines.push(`采样率: ${(info.rate || 0).toFixed(0)} sps  运行: ${fmtUptime(info.uptime)}`);
    lines.push(`枚举: native=${(last.enum || {}).native}  hidapi=${(last.enum || {}).hidapi}  ` +
      `错误=${(last.enum || {}).error || '无'}`);
    lines.push(`错误: ${info.error || '无'}`);
    lines.push(`提示: ${info.message || '-'}`);
    lines.push(`Python: ${env.python || '-'}  (${env.executable || '-'})`);
    lines.push(`系统: ${env.platform || '-'} / ${env.machine || '-'}`);
    lines.push(`目录: ${(last.paths || {}).root || '-'}`);
    lines.push('');
    lines.push(`HID 设备 (${(last.devices || []).length} 个仪表` +
      (last.hid_total ? `，共 ${last.hid_total} 个 HID，其他已隐藏` : '') + '):');
    for (const dev of last.devices || []) {
      const vid = (dev.vendor_id || 0).toString(16).toUpperCase().padStart(4, '0');
      const pid = (dev.product_id || 0).toString(16).toUpperCase().padStart(4, '0');
      const idText = dev.vendor_id || dev.product_id ? `${vid}:${pid}` : '****:****';
      const rep = dev.reports ? `in/out/feat=${dev.reports.input}/${dev.reports.output}/${dev.reports.feature}` : 'reports=?';
      lines.push(`  ${idText} ${dev.product || ''} ${dev.manufacturer || ''} SN=${dev.serial || '-'} ` +
        `usage=${dev.usage_page}/${dev.usage} ${rep}${dev.model ? '  <= ' + dev.model : ''}`);
      lines.push(`    ${dev.path || ''}`);
    }
    lines.push('');
    lines.push('事件日志 (最近 40 条):');
    for (const e of (last.events || []).slice(-40)) {
      lines.push(`  ${hhmmss(e.t)} [${e.level}] ${e.text}`);
    }
    lines.push('');
    lines.push('原始数据包 (最近 10 条):');
    for (const r of (last.raw || []).slice(-10)) {
      lines.push(`  ${hhmmss(r.t)} len=${r.len} crc=${r.crc} V=${r.v} I=${r.i} T=${r.temp}`);
      lines.push(`    ${r.hex}`);
    }
    return lines.join('\n');
  }

  async function copyReport() {
    const text = report();
    try {
      await navigator.clipboard.writeText(text);
      toast('诊断信息已复制到剪贴板');
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.append(ta);
      ta.select();
      try { document.execCommand('copy'); toast('诊断信息已复制'); }
      catch (e) { toast('复制失败，请手动选择文本'); }
      ta.remove();
    }
  }

  let toastTimer = null;
  function toast(text) {
    let el = document.getElementById('dbgToast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'dbgToast';
      el.className = 'toast';
      document.body.append(el);
    }
    el.textContent = text;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
  }

  $('btnCopy').addEventListener('click', copyReport);
  $('btnDisconnect').addEventListener('click', async () => {
    try {
      await fetch('/api/device/disconnect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      toast('已断开，稍后会自动重连');
      setTimeout(refresh, 800);
    } catch (_) { toast('请求失败'); }
  });
  $('btnRescan').addEventListener('click', async () => {
    try {
      await fetch('/api/rescan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      toast('已请求重新扫描');
      setTimeout(refresh, 600);
    } catch (_) { toast('请求失败'); }
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearInterval(timer); timer = null; }
    else if (!timer) { refresh(); timer = setInterval(refresh, 1000); }
  });

  refresh();
  timer = setInterval(refresh, 1000);
})();
