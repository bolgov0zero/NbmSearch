<?php
// ── NbmSearch Management Panel — Configuration ──────────────────────────────

// Panel access password (change after first login)
define('PANEL_PASSWORD', 'Gjgeufq4hfpf!');

// Session lifetime in seconds (30 days)
define('SESSION_LIFETIME', 30 * 24 * 3600);

// Request timeout for NbmSearch servers (seconds)
define('CURL_TIMEOUT', 5);

// Path to servers list file
define('SERVERS_FILE', __DIR__ . '/servers.json');
