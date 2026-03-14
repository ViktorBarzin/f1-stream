<script>
	import {
		fetchReplays, refreshReplays, getReplayVideoUrl, getReplayDownloadUrl,
		checkTorrserverStatus, fetchTorrentFiles, getTorrentStreamUrl, fetchTorrentMediaInfo, getTorrentTranscodeStreamUrl,
		stopTorrentStream, sendTorrentHeartbeat
	} from '$lib/api.js';
	import { onMount } from 'svelte';

	let replaysData = $state(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let errorMsg = $state(null);
	let expandedEvents = $state(new Set());
	let activeVideo = $state(null); // { eventIdx, sessionType, postIdx, linkIdx }

	// Torrent streaming state
	let torrserverAvailable = $state(false);
	let activeTorrentHash = $state(null);
	let torrentStreamLoading = $state(false); // loading magnet metadata
	let torrentFiles = $state(null); // { hash, files: [...] }
	let torrentFilePickerFor = $state(null); // key string identifying which magnet link opened the picker
	let torrentPlayerKey = $state(null); // key string for active torrent player
	let torrentPlayerUrl = $state(null); // stream URL for active torrent player
	let torrentPlayerTranscoded = $state(false); // whether current player is using ffmpeg transcode
	let torrentPlayerInfo = $state(null); // compatibility info for current torrent file
	let torrentBuffering = $state(false); // buffering spinner overlay
	let torrentError = $state(null); // error message for torrent player
	let copiedMagnet = $state(null); // key string for "Copied!" feedback
	let heartbeatInterval = null;
	let bufferTimeoutId = null;
	const TORRENT_CONNECT_TIMEOUT_MS = 120_000;

	onMount(() => {
		loadReplays();
		// Check TorrServer availability
		checkTorrserverStatus()
			.then(data => { torrserverAvailable = data.available; })
			.catch(() => {
				// Retry once after 5s (handles cold-start)
				setTimeout(() => {
					checkTorrserverStatus()
						.then(data => { torrserverAvailable = data.available; })
						.catch(() => {});
				}, 5000);
			});

		// beforeunload handler for torrent cleanup
		const onBeforeUnload = () => {
			if (activeTorrentHash) {
				navigator.sendBeacon(
					'/api/replays/torrent-stop',
					new Blob([JSON.stringify({ hash: activeTorrentHash })], { type: 'application/json' })
				);
			}
		};
		window.addEventListener('beforeunload', onBeforeUnload);

		return () => {
			// Cleanup on SvelteKit navigation
			window.removeEventListener('beforeunload', onBeforeUnload);
			if (heartbeatInterval) clearInterval(heartbeatInterval);
			if (bufferTimeoutId) clearTimeout(bufferTimeoutId);
			// Stop active torrent on component destroy
			if (activeTorrentHash) {
				stopTorrentStream(activeTorrentHash);
			}
		};
	});

	async function loadReplays() {
		loading = true;
		errorMsg = null;
		try {
			replaysData = await fetchReplays();
			// Auto-expand the first event
			if (replaysData?.events?.length > 0) {
				expandedEvents = new Set([0]);
			}
		} catch (e) {
			errorMsg = e.message;
		} finally {
			loading = false;
		}
	}

	async function handleRefresh() {
		refreshing = true;
		try {
			replaysData = await refreshReplays();
			if (replaysData?.events?.length > 0) {
				expandedEvents = new Set([0]);
			}
		} catch (e) {
			errorMsg = e.message;
		} finally {
			refreshing = false;
		}
	}

	function toggleEvent(index) {
		const next = new Set(expandedEvents);
		if (next.has(index)) {
			next.delete(index);
		} else {
			next.add(index);
		}
		expandedEvents = next;
	}

	function playVideo(eventIdx, sessionType, postIdx, linkIdx, link) {
		const key = `${eventIdx}-${sessionType}-${postIdx}-${linkIdx}`;
		if (activeVideo === key) {
			activeVideo = null;
		} else {
			activeVideo = key;
		}
	}

	function isVideoActive(eventIdx, sessionType, postIdx, linkIdx) {
		return activeVideo === `${eventIdx}-${sessionType}-${postIdx}-${linkIdx}`;
	}

	function getVideoSrc(link) {
		const url = link.video_url || link.url;
		return getReplayVideoUrl(url);
	}

	function getDownloadHref(link) {
		const url = link.video_url || link.url;
		return getReplayDownloadUrl(url);
	}

	function formatTimeAgo(utc) {
		const now = Date.now() / 1000;
		const diff = now - utc;
		const hours = Math.floor(diff / 3600);
		if (hours < 1) return 'just now';
		if (hours < 24) return `${hours}h ago`;
		const days = Math.floor(hours / 24);
		return `${days}d ago`;
	}

	function formatLastUpdated(isoStr) {
		if (!isoStr) return '';
		const d = new Date(isoStr);
		const diff = Date.now() - d.getTime();
		const mins = Math.floor(diff / 60000);
		if (mins < 1) return 'just now';
		if (mins < 60) return `${mins}m ago`;
		const hours = Math.floor(mins / 60);
		return `${hours}h ago`;
	}

	// Session type ordering
	const SESSION_ORDER = { 'Race': 0, 'Sprint': 1, 'Sprint Qualifying': 2, 'Qualifying': 3, 'Practice': 4, 'Pre-Race': 5, 'Full Event': 6, 'Other': 7 };
	function sessionOrder(type) {
		return SESSION_ORDER[type] ?? 99;
	}

	// --- Torrent streaming functions ---

	function magnetKey(eventIdx, sessionType, postIdx, linkIdx) {
		return `magnet-${eventIdx}-${sessionType}-${postIdx}-${linkIdx}`;
	}

	async function handleMagnetStream(eventIdx, sessionType, postIdx, linkIdx, magnetUri) {
		const key = magnetKey(eventIdx, sessionType, postIdx, linkIdx);
		if (torrentStreamLoading) return; // prevent double-click

		torrentStreamLoading = true;
		torrentError = null;
		torrentFilePickerFor = key;

		try {
			const data = await fetchTorrentFiles(magnetUri);
			// Store hash immediately for beforeunload cleanup
			activeTorrentHash = data.hash;
			torrentFiles = data;

			// If single video file, skip picker and start streaming directly
			const videoFiles = data.files.filter(f =>
				/\.(mp4|mkv|ts|avi|webm)$/i.test(f.name)
			);
			if (videoFiles.length === 1) {
				startTorrentPlayback(key, data.hash, videoFiles[0].index);
			}
			// Otherwise, the file picker will be shown in the template
		} catch (e) {
			torrentError = e.message || 'Failed to fetch torrent metadata';
			torrentFilePickerFor = null;
		} finally {
			torrentStreamLoading = false;
		}
	}

	async function startTorrentPlayback(key, hash, fileIndex) {
		let mediaInfo = null;
		try {
			mediaInfo = await fetchTorrentMediaInfo(hash, fileIndex);
		} catch (e) {
			console.warn('Failed to fetch torrent media info', e);
		}

		const useTranscode = !!mediaInfo?.transcode_recommended;

		torrentPlayerKey = key;
		torrentPlayerUrl = useTranscode
			? getTorrentTranscodeStreamUrl(hash, fileIndex)
			: getTorrentStreamUrl(hash, fileIndex);
		torrentPlayerTranscoded = useTranscode;
		torrentPlayerInfo = mediaInfo;
		torrentBuffering = true;
		torrentError = null;
		torrentFilePickerFor = null; // close picker

		// Buffer timeout — give up after 120s with no progress
		if (bufferTimeoutId) clearTimeout(bufferTimeoutId);
		bufferTimeoutId = setTimeout(() => {
			if (torrentBuffering) {
				torrentBuffering = false;
				torrentError = 'Could not connect to enough peers. Try again later.';
			}
		}, TORRENT_CONNECT_TIMEOUT_MS);

		// Start heartbeat
		if (heartbeatInterval) clearInterval(heartbeatInterval);
		heartbeatInterval = setInterval(() => {
			if (activeTorrentHash) {
				sendTorrentHeartbeat(activeTorrentHash);
			}
		}, 5 * 60 * 1000); // every 5 min
	}

	function onTorrentCanPlayThrough() {
		torrentBuffering = false;
		if (bufferTimeoutId) clearTimeout(bufferTimeoutId);
	}

	function onTorrentProgress() {
		// Data is arriving — reset timeout counter
		if (bufferTimeoutId) clearTimeout(bufferTimeoutId);
		bufferTimeoutId = setTimeout(() => {
			if (torrentBuffering) {
				torrentBuffering = false;
				torrentError = 'Could not connect to enough peers. Try again later.';
			}
		}, TORRENT_CONNECT_TIMEOUT_MS);
	}

	function onTorrentError() {
		torrentBuffering = false;
		if (bufferTimeoutId) clearTimeout(bufferTimeoutId);
		torrentError = 'Video playback error. The stream may not be available yet.';
	}

	async function closeTorrentPlayer() {
		if (heartbeatInterval) { clearInterval(heartbeatInterval); heartbeatInterval = null; }
		if (bufferTimeoutId) { clearTimeout(bufferTimeoutId); bufferTimeoutId = null; }
		if (activeTorrentHash) {
			await stopTorrentStream(activeTorrentHash);
			activeTorrentHash = null;
		}
		torrentPlayerKey = null;
		torrentPlayerUrl = null;
		torrentPlayerTranscoded = false;
		torrentPlayerInfo = null;
		torrentBuffering = false;
		torrentError = null;
		torrentFiles = null;
		torrentFilePickerFor = null;
	}

	function cancelFilePicker() {
		torrentFilePickerFor = null;
		torrentFiles = null;
		// Don't clear activeTorrentHash here — let the cleanup handle it if needed
		if (activeTorrentHash && !torrentPlayerKey) {
			stopTorrentStream(activeTorrentHash);
			activeTorrentHash = null;
		}
	}

	async function copyMagnet(eventIdx, sessionType, postIdx, linkIdx, magnetUri) {
		const key = magnetKey(eventIdx, sessionType, postIdx, linkIdx);
		try {
			await navigator.clipboard.writeText(magnetUri);
			copiedMagnet = key;
			setTimeout(() => { if (copiedMagnet === key) copiedMagnet = null; }, 2000);
		} catch {
			// Fallback for older browsers
			copiedMagnet = null;
		}
	}

	function formatBytes(bytes) {
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
		if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
		return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
	}

	function getLargestVideoIndex(files) {
		let best = null;
		let bestSize = -1;
		for (const f of files) {
			if (/\.(mp4|mkv|ts|avi|webm)$/i.test(f.name) && f.length > bestSize) {
				best = f.index;
				bestSize = f.length;
			}
		}
		return best;
	}

	function getTranscodeReasonText(info) {
		if (!info?.reasons?.length) return '';
		return info.reasons.join('; ');
	}
</script>

<svelte:head>
	<title>F1 Stream - Replays</title>
</svelte:head>

<div class="max-w-6xl mx-auto px-4 py-6">
	{#if loading}
		<div class="flex items-center justify-center py-20">
			<div class="w-8 h-8 border-2 border-f1-red border-t-transparent rounded-full animate-spin"></div>
			<span class="ml-3 text-f1-text-muted">Loading replays...</span>
		</div>
	{:else if errorMsg}
		<div class="bg-red-900/30 border border-red-700 rounded-lg p-4 text-center">
			<p class="text-red-300">Failed to load replays: {errorMsg}</p>
			<button onclick={loadReplays} class="mt-2 px-4 py-1 bg-f1-red text-white rounded text-sm">Retry</button>
		</div>
	{:else if replaysData}
		<!-- Header -->
		<div class="flex items-center justify-between mb-6">
			<div>
				<h1 class="text-2xl font-bold text-white">Replays</h1>
				<p class="text-f1-text-muted text-sm mt-1">
					{replaysData.total_posts} post{replaysData.total_posts !== 1 ? 's' : ''} from r/MotorsportsReplays
				</p>
			</div>
			<div class="flex items-center gap-3">
				{#if replaysData.last_updated}
					<span class="text-xs text-f1-text-muted">Updated {formatLastUpdated(replaysData.last_updated)}</span>
				{/if}
				<button
					onclick={handleRefresh}
					disabled={refreshing}
					class="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium text-f1-text-muted hover:text-white bg-f1-surface border border-f1-border hover:border-f1-red transition-colors disabled:opacity-50"
				>
					<svg class="w-3.5 h-3.5 {refreshing ? 'animate-spin' : ''}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/>
					</svg>
					{refreshing ? 'Refreshing...' : 'Refresh'}
				</button>
			</div>
		</div>

		{#if replaysData.events.length === 0}
			<div class="bg-f1-surface border border-f1-border rounded-lg p-8 text-center">
				<p class="text-f1-text-muted">No F1 replays found in the last 7 days.</p>
				<p class="text-f1-text-muted text-sm mt-2">Check back after a race weekend.</p>
			</div>
		{:else}
			<div class="space-y-4">
				{#each replaysData.events as event, eventIdx (event.event_name)}
					{@const isExpanded = expandedEvents.has(eventIdx)}
					{@const sessionTypes = Object.keys(event.sessions).sort((a, b) => sessionOrder(a) - sessionOrder(b))}
					{@const totalLinks = Object.values(event.sessions).reduce((sum, posts) => sum + posts.reduce((s, p) => s + p.links.length, 0), 0)}

					<div class="bg-f1-surface border border-f1-border rounded-lg overflow-hidden">
						<!-- Event Header (clickable) -->
						<button
							onclick={() => toggleEvent(eventIdx)}
							class="w-full px-4 py-3 flex items-center justify-between hover:bg-f1-surface-hover transition-colors"
						>
							<div class="flex items-center gap-3 text-left">
								<svg class="w-4 h-4 text-f1-text-muted transition-transform {isExpanded ? 'rotate-90' : ''}" fill="currentColor" viewBox="0 0 24 24">
									<path d="M10 6L8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z"/>
								</svg>
								<div>
									<h2 class="font-semibold text-white">{event.event_name}</h2>
									<p class="text-xs text-f1-text-muted mt-0.5">
										{#if event.event_date}{event.event_date} &middot; {/if}
										{sessionTypes.length} session{sessionTypes.length !== 1 ? 's' : ''} &middot;
										{totalLinks} link{totalLinks !== 1 ? 's' : ''}
									</p>
								</div>
							</div>
						</button>

						<!-- Event Content -->
						{#if isExpanded}
							<div class="border-t border-f1-border">
								{#each sessionTypes as sessionType}
									{@const posts = event.sessions[sessionType]}
									<div class="border-b border-f1-border last:border-b-0">
										<!-- Session Header -->
										<div class="px-4 py-2 bg-f1-bg/50">
											<h3 class="text-sm font-medium text-f1-text-muted uppercase tracking-wider">{sessionType}</h3>
										</div>

										<!-- Posts in this session -->
										{#each posts as post, postIdx}
											<div class="px-4 py-3 {postIdx > 0 ? 'border-t border-f1-border/50' : ''}">
												<!-- Post title & meta -->
												<div class="flex items-start justify-between gap-2 mb-2">
													<div class="flex-1 min-w-0">
														<p class="text-sm text-white">{post.title}</p>
														<div class="flex items-center gap-2 mt-1">
															<span class="text-xs text-f1-text-muted">{formatTimeAgo(post.created_utc)}</span>
															{#if post.flair}
																<span class="text-[10px] px-1.5 py-0.5 rounded bg-f1-red/20 text-f1-red border border-f1-red/30">{post.flair}</span>
															{/if}
														</div>
													</div>
													<a href={post.reddit_url} target="_blank" rel="noopener" class="shrink-0 text-xs text-f1-text-muted hover:text-white transition-colors">
														Reddit
													</a>
												</div>

												<!-- Links -->
												<div class="flex flex-wrap gap-2">
													{#each post.links as link, linkIdx}
														{#if link.link_type === 'video'}
															<div class="flex items-center gap-1">
																<button
																	onclick={() => playVideo(eventIdx, sessionType, postIdx, linkIdx, link)}
																	class="flex items-center gap-1 px-2.5 py-1 rounded text-xs font-medium bg-green-600/20 text-green-300 border border-green-500/30 hover:bg-green-600/30 transition-colors"
																>
																	<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
																	{link.label}
																</button>
																<a
																	href={getDownloadHref(link)}
																	class="flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-blue-600/20 text-blue-300 border border-blue-500/30 hover:bg-blue-600/30 transition-colors"
																	title="Download"
																>
																	<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z"/></svg>
																</a>
															</div>
														{:else if link.link_type === 'embed'}
															<a
																href={link.url}
																target="_blank"
																rel="noopener"
																class="flex items-center gap-1 px-2.5 py-1 rounded text-xs font-medium bg-purple-600/20 text-purple-300 border border-purple-500/30 hover:bg-purple-600/30 transition-colors"
															>
																<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M21 3H3c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16H3V5h18v14zM9 8l7 4-7 4V8z"/></svg>
																{link.label}
															</a>
														{:else if link.link_type === 'magnet'}
															<div class="flex items-center gap-1">
																{#if torrserverAvailable}
																	<button
																		onclick={() => handleMagnetStream(eventIdx, sessionType, postIdx, linkIdx, link.url)}
																		disabled={torrentStreamLoading}
																		class="flex items-center gap-1 px-2.5 py-1 rounded text-xs font-medium bg-orange-600/20 text-orange-300 border border-orange-500/30 hover:bg-orange-600/30 transition-colors disabled:opacity-50"
																	>
																		{#if torrentStreamLoading && torrentFilePickerFor === magnetKey(eventIdx, sessionType, postIdx, linkIdx)}
																			<svg class="w-3 h-3 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"/></svg>
																		{:else}
																			<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
																		{/if}
																		Stream
																	</button>
																{:else}
																	<span
																		class="flex items-center gap-1 px-2.5 py-1 rounded text-xs font-medium bg-orange-600/10 text-orange-400/50 border border-orange-500/20 cursor-not-allowed"
																		title="TorrServer not configured"
																	>
																		<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
																		Stream
																	</span>
																{/if}
																<button
																	onclick={() => copyMagnet(eventIdx, sessionType, postIdx, linkIdx, link.url)}
																	class="flex items-center gap-1 px-2 py-1 rounded text-xs font-medium bg-orange-600/20 text-orange-300 border border-orange-500/30 hover:bg-orange-600/30 transition-colors"
																	title="Copy magnet link"
																>
																	{#if copiedMagnet === magnetKey(eventIdx, sessionType, postIdx, linkIdx)}
																		✓ Copied
																	{:else}
																		<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M16 1H4c-1.1 0-2 .9-2 2v14h2V3h12V1zm3 4H8c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h11c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H8V7h11v14z"/></svg>
																		Copy
																	{/if}
																</button>
																<span class="text-[10px] text-orange-400/70 ml-0.5 max-w-[120px] truncate" title={link.label}>
																	{link.label}
																</span>
															</div>
														{:else}
															<a
																href={link.url}
																target="_blank"
																rel="noopener"
																class="flex items-center gap-1 px-2.5 py-1 rounded text-xs font-medium bg-gray-600/20 text-gray-300 border border-gray-500/30 hover:bg-gray-600/30 transition-colors"
															>
																<svg class="w-3 h-3" fill="currentColor" viewBox="0 0 24 24"><path d="M19 19H5V5h7V3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2v-7h-2v7zM14 3v2h3.59l-9.83 9.83 1.41 1.41L19 6.41V10h2V3h-7z"/></svg>
																{link.label}
															</a>
														{/if}
													{/each}
												</div>

												<!-- Torrent file picker -->
												{#each post.links as link, linkIdx}
													{#if link.link_type === 'magnet' && torrentFilePickerFor === magnetKey(eventIdx, sessionType, postIdx, linkIdx) && torrentFiles}
														{@const defaultIdx = getLargestVideoIndex(torrentFiles.files)}
														<div class="mt-3 bg-f1-bg border border-orange-500/30 rounded-lg p-3">
															<div class="flex items-center justify-between mb-2">
																<h4 class="text-sm font-medium text-orange-300">Select a file to stream</h4>
																<button onclick={cancelFilePicker} class="text-xs text-f1-text-muted hover:text-white">✕ Cancel</button>
															</div>
															<div class="space-y-1 max-h-[200px] overflow-y-auto" role="listbox" aria-label="Torrent files">
																{#each torrentFiles.files as file}
																	<button
																		onclick={() => startTorrentPlayback(magnetKey(eventIdx, sessionType, postIdx, linkIdx), torrentFiles.hash, file.index)}
																		class="w-full flex items-center justify-between px-3 py-1.5 rounded text-xs hover:bg-f1-surface-hover transition-colors text-left {file.index === defaultIdx ? 'bg-orange-600/10 border border-orange-500/20' : ''}"
																		role="option"
																		aria-selected={file.index === defaultIdx}
																	>
																		<span class="truncate flex-1 text-f1-text {file.index === defaultIdx ? 'text-orange-300 font-medium' : ''}">{file.name}</span>
																		<span class="shrink-0 ml-2 text-f1-text-muted">{formatBytes(file.length)}</span>
																	</button>
																{/each}
															</div>
														</div>
													{/if}
												{/each}

												<!-- Torrent video player -->
												{#each post.links as link, linkIdx}
													{#if link.link_type === 'magnet' && torrentPlayerKey === magnetKey(eventIdx, sessionType, postIdx, linkIdx)}
														<div class="mt-3 rounded-lg overflow-hidden bg-black relative">
															{#if torrentBuffering}
																<div class="absolute inset-0 flex flex-col items-center justify-center bg-black/80 z-10">
																	<div class="w-8 h-8 border-2 border-orange-500 border-t-transparent rounded-full animate-spin"></div>
																	<p class="text-sm text-orange-300 mt-3">Connecting to torrent swarm...</p>
																</div>
															{/if}
															{#if torrentError}
																<div class="absolute inset-0 flex flex-col items-center justify-center bg-black/80 z-10">
																	<p class="text-sm text-red-400">{torrentError}</p>
																	<button onclick={closeTorrentPlayer} class="mt-2 px-3 py-1 text-xs bg-f1-surface border border-f1-border rounded hover:bg-f1-surface-hover">Close</button>
																</div>
															{/if}
															<video
																src={torrentPlayerUrl}
																controls
																class="w-full max-h-[500px]"
																playsinline
																oncanplaythrough={onTorrentCanPlayThrough}
																onprogress={onTorrentProgress}
																onerror={onTorrentError}
															>
																<track kind="captions" />
															</video>
															{#if torrentPlayerTranscoded}
																<div class="px-3 py-2 text-xs bg-amber-950/80 text-amber-200 border-t border-amber-600/30">
																	<div class="font-medium">Audio compatibility mode enabled</div>
																	<div class="mt-1 text-amber-200/80">
																		This replay is being transcoded for browser-safe audio playback.
																		{#if torrentPlayerInfo}
																			<span class="block mt-1">{getTranscodeReasonText(torrentPlayerInfo)}</span>
																		{/if}
																	</div>
																</div>
															{/if}
															<div class="flex justify-end p-1 bg-f1-surface">
																<button onclick={closeTorrentPlayer} class="text-xs text-f1-text-muted hover:text-white px-2 py-0.5">✕ Close player</button>
															</div>
														</div>
													{/if}
												{/each}

												<!-- Inline video player -->
												{#each post.links as link, linkIdx}
													{#if link.link_type === 'video' && isVideoActive(eventIdx, sessionType, postIdx, linkIdx)}
														<div class="mt-3 rounded-lg overflow-hidden bg-black">
															<video
																src={getVideoSrc(link)}
																controls
																class="w-full max-h-[500px]"
																playsinline
																autoplay
															>
																<track kind="captions" />
															</video>
														</div>
													{/if}
												{/each}
											</div>
										{/each}
									</div>
								{/each}
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}
	{/if}
</div>
