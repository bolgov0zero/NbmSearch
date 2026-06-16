<?php
/**
 * NbmSearch Management Panel — API proxy
 * Handles AJAX requests from the frontend, proxies to NbmSearch servers via curl.
 */

require_once __DIR__ . '/config.php';

session_start([
    'cookie_lifetime' => SESSION_LIFETIME,
    'cookie_httponly' => true,
    'cookie_samesite' => 'Lax',
]);

header('Content-Type: application/json; charset=utf-8');

// Auth check
if (empty($_SESSION['auth'])) {
    http_response_code(401);
    echo json_encode(['error' => 'unauthorized']);
    exit;
}

// Release the session lock immediately — we only read auth, never write.
// Without this PHP serializes all concurrent api.php requests on the session file lock.
session_write_close();

// ── Helpers ──────────────────────────────────────────────────────────────────

function load_servers(): array {
    if (!file_exists(SERVERS_FILE)) return [];
    $data = json_decode(file_get_contents(SERVERS_FILE), true);
    if (!is_array($data)) return [];
    usort($data, fn($a, $b) => strcasecmp($a['name'] ?? '', $b['name'] ?? ''));
    return $data;
}

function save_servers(array $servers): void {
    file_put_contents(SERVERS_FILE, json_encode(array_values($servers), JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE));
}

function find_server(string $id): ?array {
    foreach (load_servers() as $s) {
        if ($s['id'] === $id) return $s;
    }
    return null;
}

function nbm_request(string $url, string $token, string $method = 'GET', ?array $body = null): array {
    $ch = curl_init();
    curl_setopt_array($ch, [
        CURLOPT_URL            => $url,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => CURL_TIMEOUT,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_HTTPHEADER     => [
            'X-Management-Token: ' . $token,
            'Content-Type: application/json',
            'Accept: application/json',
        ],
    ]);
    if ($body !== null) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body));
    }
    $response = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $error    = curl_error($ch);
    curl_close($ch);

    if ($error || !$response) {
        return ['_error' => true, '_message' => $error ?: 'No response', '_code' => 0];
    }
    $data = json_decode($response, true);
    if (!is_array($data)) {
        return ['_error' => true, '_message' => 'Invalid JSON', '_code' => $httpCode];
    }
    if ($httpCode >= 400) {
        return array_merge($data, ['_error' => true, '_code' => $httpCode]);
    }
    return $data;
}

function panel_url(): string {
    $scheme = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';
    $host   = $_SERVER['HTTP_HOST'] ?? 'localhost';
    $path   = dirname($_SERVER['SCRIPT_NAME']);
    $path   = rtrim($path, '/');
    return $scheme . '://' . $host . $path . '/index.php';
}

// ── Router ───────────────────────────────────────────────────────────────────

$input  = json_decode(file_get_contents('php://input'), true) ?? [];
$action = $_GET['action'] ?? $_POST['action'] ?? ($input['action'] ?? '');

switch ($action) {

    // ── Add server ────────────────────────────────────────────────────────────
    case 'add_server': {
        $name  = trim($input['name'] ?? '');
        $url   = rtrim(trim($input['url'] ?? ''), '/');
        $token = trim($input['token'] ?? '');
        if (!$url || !$token) {
            echo json_encode(['error' => 'URL и токен обязательны']); exit;
        }

        // Validate connection
        $info = nbm_request($url . '/api/management/info', $token);
        if (!empty($info['_error'])) {
            $msg = $info['_message'] ?? 'Ошибка подключения';
            if (($info['_code'] ?? 0) === 401) $msg = 'Неверный токен';
            echo json_encode(['error' => $msg]); exit;
        }

        // Register panel URL on server
        nbm_request($url . '/api/management/register', $token, 'POST', [
            'panel_url' => panel_url(),
        ]);

        $id = bin2hex(random_bytes(8));
        $servers = load_servers();
        $servers[] = [
            'id'       => $id,
            'name'     => $name ?: ($info['version'] ? 'NbmSearch v' . $info['version'] : $url),
            'url'      => $url,
            'token'    => $token,
            'added_at' => time(),
        ];
        save_servers($servers);
        echo json_encode(['ok' => true, 'id' => $id, 'info' => $info]);
        break;
    }

    // ── Remove server ─────────────────────────────────────────────────────────
    case 'remove_server': {
        $id = $input['id'] ?? '';
        $servers = load_servers();
        $servers = array_filter($servers, fn($s) => $s['id'] !== $id);
        save_servers($servers);
        echo json_encode(['ok' => true]);
        break;
    }

    // ── Get info from one server ──────────────────────────────────────────────
    case 'get_info': {
        $id = $_GET['id'] ?? $input['id'] ?? '';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $info = nbm_request($server['url'] . '/api/management/info', $server['token']);
        $info['_server'] = ['id' => $server['id'], 'name' => $server['name'], 'url' => $server['url']];
        echo json_encode($info);
        break;
    }

    // ── Get info from all servers (parallel curl_multi) ──────────────────────
    case 'get_all': {
        $servers = load_servers();
        if (empty($servers)) { echo json_encode([]); break; }

        $mh      = curl_multi_init();
        $handles = [];

        foreach ($servers as $s) {
            $ch = curl_init();
            curl_setopt_array($ch, [
                CURLOPT_URL             => $s['url'] . '/api/management/info',
                CURLOPT_RETURNTRANSFER  => true,
                CURLOPT_CONNECTTIMEOUT  => CURL_TIMEOUT,  // TCP connect timeout
                CURLOPT_TIMEOUT         => CURL_TIMEOUT,  // total timeout
                CURLOPT_HTTPHEADER      => [
                    'X-Management-Token: ' . $s['token'],
                    'Accept: application/json',
                ],
            ]);
            curl_multi_add_handle($mh, $ch);
            $handles[$s['id']] = ['ch' => $ch, 'server' => $s];
        }

        // Execute all requests in parallel
        $running = null;
        do {
            $status = curl_multi_exec($mh, $running);
            if ($running > 0) {
                $ms = curl_multi_select($mh, 0.1);
                if ($ms === -1) usleep(10000); // fallback if select() fails
            }
        } while ($running > 0 && $status === CURLM_OK);

        $results = [];
        foreach ($handles as $sid => $item) {
            $ch     = $item['ch'];
            $s      = $item['server'];
            $body   = curl_multi_getcontent($ch);
            $code   = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            $err    = curl_error($ch);
            curl_multi_remove_handle($mh, $ch);
            curl_close($ch);

            if ($err || !$body) {
                $info = ['_error' => true, '_message' => $err ?: 'No response'];
            } else {
                $info = json_decode($body, true) ?? ['_error' => true, '_message' => 'Invalid JSON'];
                if ($code >= 400) $info['_error'] = true;
            }
            $info['_server'] = ['id' => $s['id'], 'name' => $s['name'], 'url' => $s['url']];
            $results[] = $info;
        }
        curl_multi_close($mh);
        echo json_encode($results);
        break;
    }

    // ── Restart server ────────────────────────────────────────────────────────
    case 'restart': {
        $id = $input['id'] ?? '';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/management/restart', $server['token'], 'POST');
        echo json_encode($res);
        break;
    }

    // ── Get search stats from server ──────────────────────────────────────────
    case 'get_search_stats': {
        $id     = $_GET['id'] ?? '';
        $period = $_GET['period'] ?? 'day';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/management/search-stats?period=' . urlencode($period), $server['token']);
        echo json_encode($res);
        break;
    }

    // ── Recent file additions (watchdog) for one server ─────────────────────────
    case 'recent_additions': {
        $id    = $_GET['id'] ?? '';
        $since = $_GET['since'] ?? '0';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/management/recent-additions?since=' . urlencode($since), $server['token']);
        echo json_encode($res);
        break;
    }

    // ── Aggregated stats from ALL servers (parallel curl_multi) ──────────────────
    case 'all_stats': {
        $period  = $_GET['period'] ?? 'day';
        $servers = load_servers();
        if (empty($servers)) { echo json_encode([]); break; }

        $mh = curl_multi_init();
        $handles = [];
        foreach ($servers as $s) {
            $ch = curl_init();
            curl_setopt_array($ch, [
                CURLOPT_URL            => $s['url'] . '/api/management/all-stats?period=' . urlencode($period),
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_CONNECTTIMEOUT => CURL_TIMEOUT,
                CURLOPT_TIMEOUT        => CURL_TIMEOUT,
                CURLOPT_HTTPHEADER     => ['X-Management-Token: ' . $s['token'], 'Accept: application/json'],
            ]);
            curl_multi_add_handle($mh, $ch);
            $handles[$s['id']] = $ch;
        }

        $running = null;
        do {
            $status = curl_multi_exec($mh, $running);
            if ($running > 0) { $ms = curl_multi_select($mh, 0.1); if ($ms === -1) usleep(10000); }
        } while ($running > 0 && $status === CURLM_OK);

        $nameMap = [];
        foreach ($servers as $s) { $nameMap[$s['id']] = $s['name']; }

        $results = [];
        foreach ($handles as $sid => $ch) {
            $body = curl_multi_getcontent($ch);
            $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            $err  = curl_error($ch);
            curl_multi_remove_handle($mh, $ch);
            curl_close($ch);
            if ($err || !$body || $code >= 400) { $d = ['_error' => true]; }
            else { $d = json_decode($body, true); if (!is_array($d)) $d = ['_error' => true]; }
            $d['_server'] = ['id' => $sid, 'name' => $nameMap[$sid] ?? $sid];
            $results[] = $d;
        }
        curl_multi_close($mh);
        echo json_encode($results);
        break;
    }

    // ── Files added timeline ────────────────────────────────────────────────────
    case 'files_stats': {
        $id     = $_GET['id'] ?? '';
        $period = $_GET['period'] ?? 'day';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/management/files-stats?period=' . urlencode($period), $server['token']);
        echo json_encode($res);
        break;
    }

    // ── Active users history ────────────────────────────────────────────────────
    case 'active_stats': {
        $id     = $_GET['id'] ?? '';
        $period = $_GET['period'] ?? 'day';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/management/active-stats?period=' . urlencode($period), $server['token']);
        echo json_encode($res);
        break;
    }

    // ── Active users ──────────────────────────────────────────────────────────
    case 'active_users': {
        $id = $_GET['id'] ?? '';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/active-users', $server['token']);
        echo json_encode($res);
        break;
    }

    // ── Check update ──────────────────────────────────────────────────────────
    case 'check_update': {
        $id = $_GET['id'] ?? $input['id'] ?? '';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/management/update-check', $server['token']);
        echo json_encode($res);
        break;
    }

    // ── Start update ───────────────────────────────────────────────────────────
    case 'start_update': {
        $id           = $input['id'] ?? '';
        $download_url = $input['download_url'] ?? '';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/management/update-start', $server['token'], 'POST',
                           ['download_url' => $download_url]);
        echo json_encode($res);
        break;
    }

    // ── Get update status ─────────────────────────────────────────────────────
    case 'get_update_status': {
        $id = $_GET['id'] ?? $input['id'] ?? '';
        $server = find_server($id);
        if (!$server) { echo json_encode(['error' => 'Server not found']); exit; }
        $res = nbm_request($server['url'] . '/api/management/update-status', $server['token']);
        echo json_encode($res);
        break;
    }

    default:
        http_response_code(400);
        echo json_encode(['error' => 'Unknown action']);
}
