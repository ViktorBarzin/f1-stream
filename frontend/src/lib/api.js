/**
 * API client for the F1 Streams backend.
 * All endpoints are on the same origin, so no CORS issues.
 */

const API_BASE = '';

/**
 * Fetch the F1 race schedule with session statuses.
 * @returns {Promise<{season: string, fetched_at: string, races: Array}>}
 */
export async function fetchSchedule() {
	const res = await fetch(`${API_BASE}/schedule`);
	if (!res.ok) throw new Error(`Schedule fetch failed: ${res.status}`);
	return res.json();
}

/**
 * Fetch available live streams.
 * @returns {Promise<{streams: Array, count: number}>}
 */
export async function fetchStreams() {
	const res = await fetch(`${API_BASE}/streams`);
	if (!res.ok) throw new Error(`Streams fetch failed: ${res.status}`);
	return res.json();
}

/**
 * Encode a string to base64url (UTF-8 safe).
 * Handles non-Latin-1 chars in magnet &dn= params that would break btoa().
 * @param {string} str - The string to encode
 * @returns {string} base64url-encoded string
 */
function toBase64Url(str) {
	const bytes = new TextEncoder().encode(str);
	const binary = String.fromCharCode(...bytes);
	return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * Get the proxied m3u8 URL for HLS playback.
 * @param {string} m3u8Url - The original m3u8 URL
 * @returns {string} The proxy URL
 */
export function getProxyUrl(m3u8Url) {
	const encoded = toBase64Url(m3u8Url);
	return `${API_BASE}/proxy?url=${encoded}`;
}

/**
 * Get the proxied embed URL (strips ad scripts from the embed page).
 * @param {string} embedUrl - The original embed URL
 * @returns {string} The embed proxy URL
 */
export function getEmbedProxyUrl(embedUrl) {
	const encoded = toBase64Url(embedUrl);
	return `${API_BASE}/embed-proxy?url=${encoded}`;
}

/**
 * Mark a stream as actively being watched (enables token refresh).
 * @param {string} url - The stream URL
 * @param {string} [siteKey] - Optional site key
 */
export async function activateStream(url, siteKey = '') {
	const res = await fetch(`${API_BASE}/streams/activate`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ url, site_key: siteKey })
	});
	if (!res.ok) throw new Error(`Activate failed: ${res.status}`);
	return res.json();
}

/**
 * Mark a stream as no longer being watched.
 * @param {string} url - The stream URL
 */
export async function deactivateStream(url) {
	const res = await fetch(`${API_BASE}/streams/deactivate`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ url })
	});
	if (!res.ok) throw new Error(`Deactivate failed: ${res.status}`);
	return res.json();
}

/**
 * Fetch F1 replay posts grouped by event.
 * @returns {Promise<{events: Array, last_updated: string, total_posts: number}>}
 */
export async function fetchReplays() {
	const res = await fetch(`${API_BASE}/api/replays`);
	if (!res.ok) throw new Error(`Replays fetch failed: ${res.status}`);
	return res.json();
}

/**
 * Manually trigger a replay scrape refresh.
 * @returns {Promise<{events: Array, last_updated: string, total_posts: number}>}
 */
export async function refreshReplays() {
	const res = await fetch(`${API_BASE}/api/replays/refresh`, { method: 'POST' });
	if (!res.ok) throw new Error(`Replays refresh failed: ${res.status}`);
	return res.json();
}

/**
 * Get the proxied video URL for inline HTML5 playback.
 * @param {string} videoUrl - The original video URL
 * @returns {string} The proxy video URL
 */
export function getReplayVideoUrl(videoUrl) {
	const encoded = toBase64Url(videoUrl);
	return `${API_BASE}/api/replays/video?url=${encoded}`;
}

/**
 * Get the download URL for a replay video.
 * @param {string} videoUrl - The original video URL
 * @returns {string} The download URL
 */
export function getReplayDownloadUrl(videoUrl) {
	const encoded = toBase64Url(videoUrl);
	return `${API_BASE}/api/replays/download?url=${encoded}`;
}

// --- Torrent streaming API ---

/**
 * Check if TorrServer is available.
 * @returns {Promise<{available: boolean}>}
 */
export async function checkTorrserverStatus() {
	const res = await fetch(`${API_BASE}/api/replays/torrent-status`);
	if (!res.ok) return { available: false };
	return res.json();
}

/**
 * Fetch torrent file listing from a magnet URI.
 * @param {string} magnetUri - The magnet URI
 * @returns {Promise<{hash: string, files: Array<{name: string, length: number, index: number}>}>}
 */
export async function fetchTorrentFiles(magnetUri) {
	const res = await fetch(`${API_BASE}/api/replays/torrent-files`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ magnet: magnetUri })
	});
	if (!res.ok) {
		const text = await res.text().catch(() => '');
		throw new Error(`Torrent files fetch failed: ${res.status} ${text}`);
	}
	return res.json();
}

/**
 * Get the torrent stream URL for inline video playback.
 * @param {string} hash - The torrent info hash
 * @param {number} fileIndex - The file index within the torrent
 * @returns {string} The stream URL
 */
export function getTorrentStreamUrl(hash, fileIndex) {
	return `${API_BASE}/api/replays/torrent-stream?hash=${encodeURIComponent(hash)}&index=${fileIndex}`;
}

/**
 * Stop a torrent stream and clean up resources.
 * @param {string} hash - The torrent info hash
 */
export async function stopTorrentStream(hash) {
	if (!hash) return;
	await fetch(`${API_BASE}/api/replays/torrent-stop`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ hash })
	}).catch(() => {}); // best-effort
}

/**
 * Send a heartbeat to keep a torrent stream alive.
 * @param {string} hash - The torrent info hash
 */
export async function sendTorrentHeartbeat(hash) {
	if (!hash) return;
	await fetch(`${API_BASE}/api/replays/torrent-heartbeat`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ hash })
	}).catch(() => {}); // best-effort
}
