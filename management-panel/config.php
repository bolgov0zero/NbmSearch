<?php
// ── NbmSearch Management Panel — Configuration ──────────────────────────────

// Panel access password (change after first login)
define('PANEL_PASSWORD', 'Gjgeufq4hfpf!');

// Session lifetime in seconds (30 days)
define('SESSION_LIFETIME', 30 * 24 * 3600);

// Request timeout for NbmSearch servers (seconds)
// For local network 3s is enough; increase if servers are on slow links
define('CURL_TIMEOUT', 3);

// Path to servers list file
define('SERVERS_FILE', __DIR__ . '/servers.json');
