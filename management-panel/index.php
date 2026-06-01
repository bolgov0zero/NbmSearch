<?php
ob_start();
ini_set('display_errors', 0);

require_once __DIR__ . '/config.php';

session_start([
    'cookie_lifetime' => SESSION_LIFETIME,
    'cookie_httponly' => true,
    'cookie_samesite' => 'Lax',
]);

// ── Auth ─────────────────────────────────────────────────────────────────────

if (isset($_POST['do_login'])) {
    if ($_POST['password'] === PANEL_PASSWORD) {
        $_SESSION['auth'] = true;
        header('Location: ' . strtok($_SERVER['REQUEST_URI'], '?'));
        exit;
    }
    $loginError = 'Неверный пароль';
}

if (isset($_POST['do_logout'])) {
    session_destroy();
    header('Location: ' . strtok($_SERVER['REQUEST_URI'], '?'));
    exit;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function load_servers(): array {
    if (!file_exists(SERVERS_FILE)) return [];
    $data = json_decode(file_get_contents(SERVERS_FILE), true);
    return is_array($data) ? $data : [];
}

function h(string $s): string { return htmlspecialchars($s, ENT_QUOTES, 'UTF-8'); }

function fmt_uptime(int $secs): string {
    if ($secs < 60)   return $secs . ' сек';
    if ($secs < 3600) return floor($secs/60) . ' мин';
    if ($secs < 86400)return floor($secs/3600) . ' ч ' . floor(($secs%3600)/60) . ' мин';
    return floor($secs/86400) . ' д ' . floor(($secs%86400)/3600) . ' ч';
}

$servers = load_servers();
$view    = $_GET['view'] ?? 'dashboard';
$serverId = $_GET['id'] ?? '';

?><!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NbmSearch — Панель управления серверами</title>
<link rel="stylesheet" href="assets/style.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.2/dist/chart.umd.min.js"></script>
</head>
<body>

<?php if (empty($_SESSION['auth'])): ?>
<!-- ══ LOGIN ══════════════════════════════════════════════════════════════════ -->
<div class="login-wrap">
  <div class="login-box">
    <div class="login-logo">
      <div class="ico">N</div>
      <div class="login-title">NbmSearch Panel</div>
      <div class="login-sub">Панель управления серверами</div>
    </div>
    <?php if (!empty($loginError)): ?>
      <div class="login-error"><?= h($loginError) ?></div>
    <?php endif; ?>
    <form method="post">
      <div class="field">
        <label>Пароль</label>
        <input type="password" name="password" autofocus placeholder="Введите пароль">
      </div>
      <button type="submit" name="do_login" class="btn btn-primary login-btn">Войти</button>
    </form>
  </div>
</div>

<?php else: ?>
<!-- ══ APP ════════════════════════════════════════════════════════════════════ -->

<header>
  <div class="logo">
    <div class="logo-icon">N</div>
    NbmSearch <span class="logo-sub">/ Серверы</span>
  </div>
  <div class="header-right">
    <?php if ($view === 'server'): ?>
      <a href="index.php" class="btn btn-ghost btn-sm">← К серверам</a>
    <?php endif; ?>
    <form method="post" style="margin:0">
      <button type="submit" name="do_logout" class="btn-logout">Выйти</button>
    </form>
  </div>
</header>

<div class="container">

<?php if ($view === 'dashboard'): ?>
<!-- ── DASHBOARD ─────────────────────────────────────────────────────────── -->

<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
  <div>
    <div class="page-title">Серверы NbmSearch</div>
    <div class="page-sub" style="margin-bottom:0">Управление и мониторинг</div>
  </div>
  <button class="btn btn-primary" onclick="openAddModal()">
    <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5">
      <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
    </svg>
    Добавить сервер
  </button>
</div>

<!-- Summary strip (filled by JS) -->
<div class="stats-strip" id="summaryStrip">
  <div class="stat-box"><div class="stat-label">Серверов</div><div class="stat-value white" id="sumServers"><?= count($servers) ?></div></div>
  <div class="stat-box"><div class="stat-label">Онлайн</div><div class="stat-value green" id="sumOnline">—</div></div>
  <div class="stat-box"><div class="stat-label">Файлов всего</div><div class="stat-value" id="sumFiles">—</div></div>
  <div class="stat-box"><div class="stat-label">Запросов сегодня</div><div class="stat-value" id="sumSearches">—</div></div>
</div>

<!-- Server cards -->
<div class="server-grid" id="serverGrid">
  <?php if (empty($servers)): ?>
    <div style="grid-column:1/-1">
      <div class="empty">
        <svg width="40" height="40" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.3">
          <rect x="2" y="3" width="20" height="14" rx="2"/>
          <line x1="8" y1="21" x2="16" y2="21" stroke-linecap="round"/>
          <line x1="12" y1="17" x2="12" y2="21" stroke-linecap="round"/>
        </svg>
        <p>Серверы не добавлены</p>
        <p style="margin-top:6px">Нажмите «Добавить сервер» чтобы начать</p>
      </div>
    </div>
  <?php else: ?>
    <?php foreach ($servers as $s): ?>
    <div class="server-card" id="card-<?= h($s['id']) ?>" onclick="goServer('<?= h($s['id']) ?>')">
      <div class="sc-top">
        <div class="sc-icon">
          <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8">
            <rect x="2" y="3" width="20" height="14" rx="2"/>
            <line x1="8" y1="21" x2="16" y2="21" stroke-linecap="round"/>
            <line x1="12" y1="17" x2="12" y2="21" stroke-linecap="round"/>
          </svg>
        </div>
        <div class="sc-info">
          <div class="sc-name"><?= h($s['name']) ?></div>
          <div class="sc-url"><?= h($s['url']) ?></div>
          <div style="margin-top:6px;display:flex;gap:5px;flex-wrap:wrap">
            <span class="sc-badge badge-offline" id="status-<?= h($s['id']) ?>">
              <span style="width:5px;height:5px;border-radius:50%;background:currentColor;display:inline-block"></span>
              Загрузка…
            </span>
            <span class="sc-badge badge-version" id="ver-<?= h($s['id']) ?>" style="display:none"></span>
          </div>
        </div>
      </div>
      <div class="sc-stats">
        <div class="sc-stat">
          <span class="sc-stat-val" id="fc-<?= h($s['id']) ?>">—</span>
          <span class="sc-stat-lbl">Файлов</span>
        </div>
        <div class="sc-stat">
          <span class="sc-stat-val" id="idx-<?= h($s['id']) ?>">—</span>
          <span class="sc-stat-lbl">Индексов</span>
        </div>
        <div class="sc-stat">
          <span class="sc-stat-val" id="sq-<?= h($s['id']) ?>">—</span>
          <span class="sc-stat-lbl">Запросов сег.</span>
        </div>
      </div>
      <div class="sc-actions" onclick="event.stopPropagation()">
        <button class="btn btn-ghost btn-sm" onclick="goServer('<?= h($s['id']) ?>')">Подробнее</button>
        <button class="btn btn-ghost btn-sm" id="rbtn-<?= h($s['id']) ?>" onclick="confirmRestart('<?= h($s['id']) ?>','<?= h($s['name']) ?>')" disabled>Перезапустить</button>
        <button class="btn btn-ghost btn-sm" style="margin-left:auto;color:var(--red);border-color:transparent" onclick="confirmRemove('<?= h($s['id']) ?>','<?= h($s['name']) ?>')">Удалить</button>
      </div>
    </div>
    <?php endforeach; ?>
  <?php endif; ?>
</div>

<?php elseif ($view === 'server'): ?>
<!-- ── SERVER DETAIL ─────────────────────────────────────────────────────── -->

<?php
$server = null;
foreach ($servers as $s) { if ($s['id'] === $serverId) { $server = $s; break; } }
if (!$server) { echo '<p style="color:var(--red)">Сервер не найден.</p>'; }
else:
?>

<div class="detail-header">
  <div style="flex:1;min-width:0">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:4px">
      <div class="detail-title"><?= h($server['name']) ?></div>
      <span class="sc-badge badge-offline" id="detailStatus">Загрузка…</span>
      <span class="sc-badge badge-version" id="detailVersion" style="display:none"></span>
    </div>
    <div class="detail-url"><?= h($server['url']) ?></div>
  </div>
  <div style="display:flex;gap:8px;flex-shrink:0">
    <button class="btn btn-ghost btn-sm" onclick="loadDetail()" title="Обновить">
      <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" stroke-linecap="round"/>
      </svg>
      Обновить
    </button>
    <button class="btn btn-ghost btn-sm" id="detailRestartBtn" onclick="confirmRestart('<?= h($server['id']) ?>','<?= h($server['name']) ?>')" disabled>Перезапустить</button>
  </div>
</div>

<!-- Stats -->
<div class="detail-grid" id="detailStats">
  <div class="stat-box"><div class="stat-label">Файлов в индексе</div><div class="stat-value" id="dFiles">—</div></div>
  <div class="stat-box"><div class="stat-label">Индексов</div><div class="stat-value white" id="dIndexes">—</div></div>
  <div class="stat-box"><div class="stat-label">Запросов сегодня</div><div class="stat-value white" id="dToday">—</div></div>
  <div class="stat-box"><div class="stat-label">За неделю</div><div class="stat-value white" id="dWeek">—</div></div>
  <div class="stat-box"><div class="stat-label">За месяц</div><div class="stat-value white" id="dMonth">—</div></div>
  <div class="stat-box"><div class="stat-label">Uptime</div><div class="stat-value white" style="font-size:1.1rem" id="dUptime">—</div></div>
</div>

<!-- Search chart -->
<div class="card" style="margin-bottom:16px">
  <div class="card-head">
    <div class="card-title">Статистика запросов</div>
    <div class="period-tabs">
      <button class="period-tab active" onclick="selectPeriod('day',this)">День</button>
      <button class="period-tab" onclick="selectPeriod('month',this)">Месяц</button>
      <button class="period-tab" onclick="selectPeriod('year',this)">Год</button>
    </div>
  </div>
  <div class="card-body">
    <div class="chart-wrap"><canvas id="searchChart"></canvas>
      <div id="chartLoading" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--text-muted);font-size:.83rem">Загрузка…</div>
    </div>
  </div>
</div>

<!-- Indexes -->
<div class="card" style="margin-bottom:16px">
  <div class="card-head"><div class="card-title">Индексы</div></div>
  <div id="indexesBody"><div class="loading"><span class="spinner-lg"></span>Загрузка…</div></div>
</div>

<!-- Schedules -->
<div class="card">
  <div class="card-head"><div class="card-title">Планировщик</div></div>
  <div id="schedulesBody"><div class="loading"><span class="spinner-lg"></span>Загрузка…</div></div>
</div>

<?php endif; ?>
<?php endif; ?>

</div><!-- /container -->

<!-- ── Add server modal ──────────────────────────────────────────────────── -->
<div class="overlay" id="addOverlay">
  <div class="modal">
    <h3>Добавить сервер</h3>
    <p>Введите адрес NbmSearch и токен из блока «Сервер управления» в админке сервера.</p>
    <div class="field">
      <label>Название (необязательно)</label>
      <input type="text" id="addName" placeholder="Главный сервер">
    </div>
    <div class="field">
      <label>Адрес сервера</label>
      <input type="text" id="addUrl" placeholder="http://192.168.1.10:8080">
    </div>
    <div class="field">
      <label>Токен авторизации</label>
      <input type="text" id="addToken" placeholder="Вставьте токен из админки">
    </div>
    <div class="form-error" id="addError"></div>
    <div class="modal-btns">
      <button class="btn btn-ghost" onclick="closeAddModal()">Отмена</button>
      <button class="btn btn-primary" id="addBtn" onclick="doAddServer()">Добавить</button>
    </div>
  </div>
</div>

<!-- ── Confirm dialog ────────────────────────────────────────────────────── -->
<div class="confirm-overlay" id="confirmOverlay">
  <div class="modal">
    <h3 id="confirmTitle"></h3>
    <p id="confirmMsg"></p>
    <div class="modal-btns">
      <button class="btn btn-ghost" onclick="closeConfirm()">Отмена</button>
      <button class="btn btn-danger" id="confirmOk">Подтвердить</button>
    </div>
  </div>
</div>

<script>
// ── Utils ──────────────────────────────────────────────────────────────────
function api(action, params={}) {
  const qs = new URLSearchParams({action, ...params});
  return fetch('api.php?' + qs).then(r => r.json());
}
function apiPost(action, body={}) {
  return fetch('api.php', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action, ...body})
  }).then(r => r.json());
}
function fmt(n) { return typeof n === 'number' ? n.toLocaleString('ru-RU') : '—'; }
function fmtUptime(s) {
  if (!s) return '—';
  if (s < 60)   return s + ' сек';
  if (s < 3600) return Math.floor(s/60) + ' мин';
  if (s < 86400)return Math.floor(s/3600) + ' ч ' + Math.floor((s%3600)/60) + ' мин';
  return Math.floor(s/86400) + ' д ' + Math.floor((s%86400)/3600) + ' ч';
}

// ── Confirm dialog ─────────────────────────────────────────────────────────
function showConfirm(title, msg, onOk) {
  document.getElementById('confirmTitle').textContent = title;
  document.getElementById('confirmMsg').textContent   = msg;
  document.getElementById('confirmOk').onclick = () => { closeConfirm(); onOk(); };
  document.getElementById('confirmOverlay').classList.add('visible');
}
function closeConfirm() { document.getElementById('confirmOverlay').classList.remove('visible'); }

// ── Add server modal ───────────────────────────────────────────────────────
function openAddModal() { document.getElementById('addOverlay').classList.add('visible'); document.getElementById('addUrl').focus(); }
function closeAddModal() { document.getElementById('addOverlay').classList.remove('visible'); document.getElementById('addError').textContent = ''; }
document.getElementById('addOverlay').addEventListener('click', e => { if (e.target===e.currentTarget) closeAddModal(); });

async function doAddServer() {
  const name  = document.getElementById('addName').value.trim();
  const url   = document.getElementById('addUrl').value.trim();
  const token = document.getElementById('addToken').value.trim();
  const errEl = document.getElementById('addError');
  const btn   = document.getElementById('addBtn');
  errEl.textContent = '';
  if (!url || !token) { errEl.textContent = 'Заполните адрес и токен'; return; }
  btn.disabled = true; btn.textContent = 'Подключение…';
  try {
    const d = await apiPost('add_server', {name, url, token});
    if (d.error) { errEl.textContent = d.error; return; }
    closeAddModal();
    location.reload();
  } catch(e) { errEl.textContent = 'Ошибка соединения'; }
  finally { btn.disabled = false; btn.textContent = 'Добавить'; }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeAddModal(); closeConfirm(); }
  if (e.key === 'Enter' && document.getElementById('addOverlay').classList.contains('visible')) doAddServer();
});

// ── Navigation ─────────────────────────────────────────────────────────────
function goServer(id) { location.href = 'index.php?view=server&id=' + encodeURIComponent(id); }

// ── Restart & Remove ───────────────────────────────────────────────────────
function confirmRestart(id, name) {
  showConfirm('Перезапустить сервер?',
    `Сервер «${name}» будет перезапущен. Поиск будет недоступен несколько секунд.`,
    async () => {
      const d = await apiPost('restart', {id});
      if (d.error) alert('Ошибка: ' + d.error);
      else { setTimeout(() => location.reload(), 3000); }
    });
}

function confirmRemove(id, name) {
  showConfirm('Удалить сервер?',
    `Сервер «${name}» будет удалён из панели. Данные на самом сервере не затрагиваются.`,
    async () => {
      await apiPost('remove_server', {id});
      location.reload();
    });
}

<?php if ($view === 'dashboard' && !empty($servers)): ?>
// ── Dashboard: load all servers info ──────────────────────────────────────
(async function() {
  const servers = await api('get_all');
  let online = 0, totalFiles = 0, totalSearches = 0;
  servers.forEach(d => {
    const id = d._server?.id;
    if (!id) return;
    const isOnline = !d._error;
    if (isOnline) online++;
    // Status badge
    const st = document.getElementById('status-' + id);
    if (st) {
      st.className = 'sc-badge ' + (isOnline ? 'badge-online' : 'badge-offline');
      st.innerHTML = `<span style="width:5px;height:5px;border-radius:50%;background:currentColor;display:inline-block"></span> ${isOnline ? 'Онлайн' : 'Офлайн'}`;
    }
    const card = document.getElementById('card-' + id);
    if (card && !isOnline) card.classList.add('offline');
    // Version
    const vEl = document.getElementById('ver-' + id);
    if (vEl && d.version) { vEl.textContent = 'v' + d.version; vEl.style.display = ''; }
    // Stats
    const fc = d.file_count || 0;
    const searches = d.search_summary?.today || 0;
    totalFiles += fc; totalSearches += searches;
    const setEl = (id2, val) => { const el = document.getElementById(id2); if(el) el.textContent = val; };
    setEl('fc-'+id, fmt(fc));
    setEl('idx-'+id, fmt(d.folder_count || 0));
    setEl('sq-'+id, fmt(searches));
    // Restart button
    const rb = document.getElementById('rbtn-' + id);
    if (rb && isOnline) rb.disabled = false;
  });
  document.getElementById('sumOnline').textContent = online;
  document.getElementById('sumFiles').textContent  = fmt(totalFiles);
  document.getElementById('sumSearches').textContent = fmt(totalSearches);
})();
<?php endif; ?>

<?php if ($view === 'server' && $server): ?>
// ── Server detail ──────────────────────────────────────────────────────────
const SERVER_ID = '<?= h($server['id']) ?>';
let _chart = null;
let _chartPeriod = 'day';

async function loadDetail() {
  const d = await api('get_info', {id: SERVER_ID});
  const isOnline = !d._error;

  // Status
  const st = document.getElementById('detailStatus');
  st.className = 'sc-badge ' + (isOnline ? 'badge-online' : 'badge-offline');
  st.innerHTML = `<span style="width:5px;height:5px;border-radius:50%;background:currentColor;display:inline-block"></span> ${isOnline ? 'Онлайн' : 'Офлайн'}`;
  const vEl = document.getElementById('detailVersion');
  if (d.version) { vEl.textContent = 'v' + d.version; vEl.style.display = ''; }
  const rb = document.getElementById('detailRestartBtn');
  if (rb) rb.disabled = !isOnline;

  if (!isOnline) {
    ['dFiles','dIndexes','dToday','dWeek','dMonth','dUptime'].forEach(id => {
      const el = document.getElementById(id); if(el) el.textContent = '—';
    });
    document.getElementById('indexesBody').innerHTML = '<div class="empty">Сервер недоступен</div>';
    document.getElementById('schedulesBody').innerHTML = '<div class="empty">Сервер недоступен</div>';
    return;
  }

  // Stats
  const set = (id, val) => { const el=document.getElementById(id); if(el) el.textContent=val; };
  set('dFiles',   fmt(d.file_count));
  set('dIndexes', fmt(d.folder_count));
  set('dToday',   fmt(d.search_summary?.today));
  set('dWeek',    fmt(d.search_summary?.week));
  set('dMonth',   fmt(d.search_summary?.month));
  set('dUptime',  fmtUptime(d.uptime));

  // Indexes table
  const folders = d.folders || [];
  if (!folders.length) {
    document.getElementById('indexesBody').innerHTML = '<div class="empty">Индексы не добавлены</div>';
  } else {
    document.getElementById('indexesBody').innerHTML = `
      <table class="data-table">
        <thead><tr><th>Название</th><th>Файлов</th><th>Watchdog</th><th>Последняя индексация</th></tr></thead>
        <tbody>${folders.map(f => {
          const lri = f.last_reindex_at
            ? new Date(f.last_reindex_at*1000).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'})
            : '—';
          const wd = f.watchdog_enabled
            ? '<span class="chip chip-green">Следит</span>'
            : '<span class="chip chip-dim">Выкл</span>';
          return `<tr>
            <td><div class="td-name">${esc(f.name)}</div><div class="td-path">${esc(f.path)}</div></td>
            <td><span class="chip chip-accent">${fmt(f.file_count)}</span></td>
            <td>${wd}</td>
            <td style="font-size:.78rem">${lri}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
  }

  // Schedules table
  const schedules = d.schedules || [];
  if (!schedules.length) {
    document.getElementById('schedulesBody').innerHTML = '<div class="empty">Расписания не настроены</div>';
  } else {
    const schedLabels = {30:'каждые 30 мин',60:'каждый час',180:'каждые 3 ч',360:'каждые 6 ч',720:'каждые 12 ч',1440:'раз в сутки'};
    document.getElementById('schedulesBody').innerHTML = `
      <table class="data-table">
        <thead><tr><th>Индекс</th><th>Периодичность</th><th>Последний запуск</th><th>Следующий</th></tr></thead>
        <tbody>${schedules.map(s => {
          const fmt_ts = ts => ts ? new Date(ts*1000).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—';
          return `<tr>
            <td class="td-name">${esc(s.folder_name)}</td>
            <td><span class="chip chip-accent">${schedLabels[s.reindex_minutes] || s.reindex_minutes+' мин'}</span></td>
            <td style="font-size:.78rem">${fmt_ts(s.last_run_at)}</td>
            <td style="font-size:.78rem">${fmt_ts(s.next_run_at)}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>`;
  }
}

async function loadChart() {
  document.getElementById('chartLoading').style.display = 'flex';
  if (_chart) { _chart.destroy(); _chart = null; }
  const d = await api('get_search_stats', {id: SERVER_ID, period: _chartPeriod});
  document.getElementById('chartLoading').style.display = 'none';
  const timeline = d.timeline || [];
  if (!timeline.length || timeline.every(t => t.cnt === 0)) {
    document.getElementById('chartLoading').textContent = 'Нет данных';
    document.getElementById('chartLoading').style.display = 'flex';
    return;
  }
  const ctx = document.getElementById('searchChart');
  _chart = new Chart(ctx, {
    type:'line',
    data:{
      labels: timeline.map(r => r.period),
      datasets:[{
        label:'Запросов', data: timeline.map(r => r.cnt),
        borderColor:'#2ecc71', backgroundColor:'rgba(46,204,113,.1)',
        borderWidth:2, pointRadius: timeline.length>30?0:3,
        pointHoverRadius:5, pointBackgroundColor:'#2ecc71', fill:true, tension:0.4
      }]
    },
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{backgroundColor:'#1a1d27',borderColor:'#2d3148',borderWidth:1,titleColor:'#e8eaf6',bodyColor:'#8b90b8',padding:10}},
      scales:{
        x:{grid:{color:'rgba(45,49,72,.6)'},ticks:{color:'#8b90b8',font:{size:11},maxTicksLimit:12}},
        y:{grid:{color:'rgba(45,49,72,.6)'},ticks:{color:'#8b90b8',font:{size:11},precision:0},beginAtZero:true}
      }
    }
  });
}

function selectPeriod(p, btn) {
  _chartPeriod = p;
  document.querySelectorAll('.period-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  loadChart();
}

function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

loadDetail();
loadChart();
<?php endif; ?>
</script>

<?php endif; ?>
</body>
</html>
