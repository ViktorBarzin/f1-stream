<script>
	import { fetchStreams, fetchSchedule, getProxyUrl, activateStream, deactivateStream } from '$lib/api.js';
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/state';

	let Hls = $state(null);

	// Query params
	let sessionType = $derived(page.url?.searchParams?.get('session') || '');
	let roundNumber = $derived(page.url?.searchParams?.get('round') || '');

	// State
	let streamsData = $state(null);
	let scheduleData = $state(null);
	let loading = $state(true);
	let errorMsg = $state(null);
	let activeFilter = $state('all'); // 'all', 'live', 'embed', 'm3u8'

	// Multi-stream player state
	let players = $state([]);
	const MAX_PLAYERS = 4;

	// Current session info from schedule
	let currentRace = $derived.by(() => {
		if (!scheduleData?.races || !roundNumber) return null;
		return scheduleData.races.find(r => r.round === parseInt(roundNumber));
	});

	let currentSession = $derived.by(() => {
		if (!currentRace || !sessionType) return null;
		return currentRace.sessions.find(s => s.type === sessionType);
	});

	let layoutClass = $derived.by(() => {
		const count = players.length;
		if (count <= 1) return 'grid-cols-1';
		return 'grid-cols-2';
	});

	// Group streams by normalized content name
	let groupedStreams = $derived.by(() => {
		if (!streamsData?.streams) return [];

		const groups = new Map();

		for (const stream of streamsData.streams) {
			const key = normalizeStreamName(stream.title || stream.site_name || 'Unknown');

			if (!groups.has(key)) {
				groups.set(key, {
					name: prettifyGroupName(stream.title || stream.site_name || 'Unknown'),
					streams: [],
					hasLive: false,
					hasM3u8: false,
					category: categorizeStream(stream),
				});
			}

			const group = groups.get(key);
			group.streams.push(stream);
			if (stream.is_live) group.hasLive = true;
			if (stream.stream_type === 'm3u8') group.hasM3u8 = true;
		}

		// Sort: live first, then m3u8, then by stream count
		return Array.from(groups.values()).sort((a, b) => {
			if (a.hasM3u8 !== b.hasM3u8) return b.hasM3u8 - a.hasM3u8;
			if (a.hasLive !== b.hasLive) return b.hasLive - a.hasLive;
			if (a.category !== b.category) return categoryOrder(a.category) - categoryOrder(b.category);
			return b.streams.length - a.streams.length;
		});
	});

	let filteredGroups = $derived.by(() => {
		if (activeFilter === 'all') return groupedStreams;
		return groupedStreams.filter(g => {
			if (activeFilter === 'live') return g.hasLive;
			if (activeFilter === 'm3u8') return g.hasM3u8;
			if (activeFilter === 'embed') return g.streams.some(s => s.stream_type === 'embed');
			return true;
		});
	});

	function normalizeStreamName(name) {
		return name
			.toLowerCase()
			.replace(/\s*\(.*?\)\s*/g, '')     // strip (English), (1 viewers), etc.
			.replace(/\s*#\d+\s*/g, '')         // strip #2, #3
			.replace(/\s*-\s*\d+\s*$/, '')      // strip - 1, - 2
			.replace(/\[24\/7\]\s*/g, '')        // strip [24/7]
			.replace(/\bpractice\s*\d+/g, 'practice')
			.replace(/\bfp\d+/g, 'practice')
			.replace(/\bhd\b|\bsd\b/gi, '')
			.replace(/\bstream\b/gi, '')
			.replace(/[^a-z0-9]+/g, ' ')
			.trim();
	}

	function prettifyGroupName(name) {
		return name
			.replace(/\s*\(English\)\s*/g, '')
			.replace(/\s*\(\d+ viewers?\)\s*/g, '')
			.replace(/\s*#\d+\s*$/, '')
			.replace(/\s*-\s*\d+\s*$/, '')
			.replace(/\[24\/7\]\s*/, '')
			.trim();
	}

	function categorizeStream(stream) {
		const t = (stream.title || '').toLowerCase();
		const s = (stream.site_key || '').toLowerCase();
		if (s === 'demo') return 'demo';
		if (s === 'fallback') return 'fallback';
		if (s === 'discord') return 'community';
		if (t.includes('sky sports f1') || t.includes('sky f1')) return 'channel';
		if (t.includes('dazn f1') || t.includes('dazn')) return 'channel';
		if (t.includes('grand prix') || t.includes('practice') || t.includes('qualifying') || t.includes('race')) return 'event';
		return 'other';
	}

	function categoryOrder(cat) {
		const order = { event: 0, channel: 1, other: 2, community: 3, fallback: 4, demo: 5 };
		return order[cat] ?? 3;
	}

	function categoryLabel(cat) {
		return { event: 'Race Weekend', channel: '24/7 Channels', other: 'Streams', community: 'Community', fallback: 'Fallback Sites', demo: 'Test Streams' }[cat] ?? 'Other';
	}

	function categoryIcon(cat) {
		return { event: '🏁', channel: '📺', other: '🔗', community: '💬', fallback: '🔄', demo: '🧪' }[cat] ?? '📡';
	}

	function sourceColor(siteKey) {
		const colors = {
			streamed: 'bg-purple-600/20 text-purple-300 border-purple-500/30',
			daddylive: 'bg-green-600/20 text-green-300 border-green-500/30',
			timstreams: 'bg-cyan-600/20 text-cyan-300 border-cyan-500/30',
			ppv: 'bg-orange-600/20 text-orange-300 border-orange-500/30',
			pitsport: 'bg-yellow-600/20 text-yellow-300 border-yellow-500/30',
			aceztrims: 'bg-pink-600/20 text-pink-300 border-pink-500/30',
			fallback: 'bg-gray-600/20 text-gray-300 border-gray-500/30',
			discord: 'bg-indigo-600/20 text-indigo-300 border-indigo-500/30',
			demo: 'bg-gray-600/20 text-gray-400 border-gray-500/30',
		};
		return colors[siteKey] || 'bg-gray-600/20 text-gray-300 border-gray-500/30';
	}

	onMount(async () => {
		const hlsModule = await import('hls.js');
		Hls = hlsModule.default;
		loadData();
		document.addEventListener('fullscreenchange', onFullscreenChange);

		window.open = function (...args) {
			console.warn('[f1-stream] Blocked window.open:', args[0]);
			return null;
		};
		document.addEventListener('click', (e) => {
			const link = e.target?.closest?.('a[target="_blank"]');
			if (link) { e.preventDefault(); e.stopPropagation(); }
		}, true);

		window['__onGCastApiAvailable'] = (isAvailable) => { if (isAvailable) initCast(); };
		if (window.chrome?.cast) initCast();
	});

	onDestroy(() => {
		for (const player of players) cleanupPlayer(player);
		if (typeof document !== 'undefined') document.removeEventListener('fullscreenchange', onFullscreenChange);
	});

	async function loadData() {
		loading = true;
		errorMsg = null;
		try {
			const [streamsResult, scheduleResult] = await Promise.all([fetchStreams(), fetchSchedule()]);
			streamsData = streamsResult;
			scheduleData = scheduleResult;
			if (players.length === 0 && streamsData?.streams?.length > 0) {
				playStream(streamsData.streams[0]);
			}
		} catch (e) { errorMsg = e.message; }
		finally { loading = false; }
	}

	function cleanupPlayer(player) {
		if (player.hls) { player.hls.destroy(); player.hls = null; }
		if (player.originalUrl) deactivateStream(player.originalUrl).catch(() => {});
		if (player.controlsTimer) clearTimeout(player.controlsTimer);
	}

	function removePlayer(index) {
		cleanupPlayer(players[index]);
		players = players.filter((_, i) => i !== index);
	}

	function isStreamActive(url) { return players.some(p => p.originalUrl === url); }

	function playStream(stream) {
		const streamUrl = stream.stream_type === 'embed' ? stream.embed_url : stream.url;
		if (isStreamActive(streamUrl)) return;
		if (players.length >= MAX_PLAYERS) removePlayer(players.length - 1);

		if (stream.stream_type === 'embed') {
			players = [...players, {
				id: Date.now(), proxyUrl: '', originalUrl: stream.embed_url,
				embedUrl: stream.embed_url, streamType: 'embed',
				siteKey: stream.site_key || '', siteName: stream.site_name || 'Unknown',
				quality: stream.quality || '', isPlaying: true, isMuted: false,
				volume: 1, showControls: true, error: null, videoEl: null,
				containerEl: null, hls: null, controlsTimer: null,
			}];
			return;
		}

		if (!Hls) return;
		const proxyUrl = getProxyUrl(stream.url);
		players = [...players, {
			id: Date.now(), proxyUrl, originalUrl: stream.url, embedUrl: '',
			streamType: 'm3u8', siteKey: stream.site_key || '',
			siteName: stream.site_name || 'Unknown', quality: stream.quality || '',
			isPlaying: false, isMuted: false, volume: 1, showControls: true,
			error: null, videoEl: null, containerEl: null, hls: null, controlsTimer: null,
		}];
		activateStream(stream.url, stream.site_key || '').catch(() => {});
		requestAnimationFrame(() => { requestAnimationFrame(() => { initPlayer(players.length - 1); }); });
	}

	function initPlayer(index) {
		const player = players[index];
		if (!player || !player.videoEl) return;
		if (Hls.isSupported()) {
			const hlsInstance = new Hls({ enableWorker: true, lowLatencyMode: true, backBufferLength: 90 });
			hlsInstance.loadSource(player.proxyUrl);
			hlsInstance.attachMedia(player.videoEl);
			hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
				player.videoEl.play().catch(() => {});
				players[index] = { ...player, isPlaying: true, hls: hlsInstance };
			});
			hlsInstance.on(Hls.Events.ERROR, (event, data) => {
				if (data.fatal) {
					if (data.type === Hls.ErrorTypes.NETWORK_ERROR) { players[index] = { ...players[index], error: `Network error` }; hlsInstance.startLoad(); }
					else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) { players[index] = { ...players[index], error: `Media error` }; hlsInstance.recoverMediaError(); }
					else { players[index] = { ...players[index], error: `Fatal error` }; removePlayer(index); }
				}
			});
			player.hls = hlsInstance;
		} else if (player.videoEl.canPlayType('application/vnd.apple.mpegurl')) {
			player.videoEl.src = player.proxyUrl;
			player.videoEl.addEventListener('loadedmetadata', () => { player.videoEl.play().catch(() => {}); players[index] = { ...player, isPlaying: true }; });
		}
	}

	function togglePlay(index) { const p = players[index]; if (!p?.videoEl) return; if (p.videoEl.paused) { p.videoEl.play().catch(() => {}); players[index] = { ...p, isPlaying: true }; } else { p.videoEl.pause(); players[index] = { ...p, isPlaying: false }; } }
	function toggleMute(index) { const p = players[index]; if (!p?.videoEl) return; p.videoEl.muted = !p.isMuted; players[index] = { ...p, isMuted: !p.isMuted }; }
	function setVolume(index, e) { const p = players[index]; if (!p?.videoEl) return; const v = parseFloat(e.target.value); p.videoEl.volume = v; p.videoEl.muted = v === 0; players[index] = { ...p, volume: v, isMuted: v === 0 }; }
	function toggleFullscreen(index) { const p = players[index]; if (!p?.containerEl) return; if (!document.fullscreenElement) p.containerEl.requestFullscreen().catch(() => {}); else document.exitFullscreen().catch(() => {}); }

	let isFullscreen = $state(false);
	function onFullscreenChange() { isFullscreen = !!document.fullscreenElement; }
	function onPlayerMouseMove(index) {
		const p = players[index]; if (!p) return;
		if (p.controlsTimer) clearTimeout(p.controlsTimer);
		players[index] = { ...p, showControls: true };
		const timer = setTimeout(() => { if (players[index]?.isPlaying) players[index] = { ...players[index], showControls: false }; }, 3000);
		players[index] = { ...players[index], controlsTimer: timer };
	}

	let castAvailable = $state(false);
	function initCast() { if (typeof window === 'undefined' || !window.chrome?.cast) return; cast.framework.CastContext.getInstance().setOptions({ receiverApplicationId: chrome.cast.media.DEFAULT_MEDIA_RECEIVER_APP_ID, autoJoinPolicy: chrome.cast.AutoJoinPolicy.ORIGIN_SCOPED }); castAvailable = true; }
	function castStream(index) {
		const p = players[index]; if (!p || !castAvailable) return;
		const session = cast.framework.CastContext.getInstance().getCurrentSession();
		if (!session) { cast.framework.CastContext.getInstance().requestSession().then(() => castStream(index)).catch(() => {}); return; }
		const url = new URL(p.proxyUrl, window.location.origin).href;
		const info = new chrome.cast.media.MediaInfo(url, 'application/x-mpegURL');
		info.streamType = chrome.cast.media.StreamType.LIVE;
		info.metadata = new chrome.cast.media.GenericMediaMetadata();
		info.metadata.title = p.siteName + (p.quality ? ` (${p.quality})` : '');
		session.loadMedia(new chrome.cast.media.LoadRequest(info)).catch(() => {});
	}
</script>

<svelte:head>
	<title>F1 Stream - Watch{currentRace ? ` - ${currentRace.race_name}` : ''}</title>
	<script src="https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1"></script>
</svelte:head>

<div class="max-w-7xl mx-auto px-4 py-6">
	<!-- Header -->
	{#if currentRace && currentSession}
		<div class="mb-6">
			<p class="text-f1-text-muted text-sm uppercase tracking-wider">
				Round {currentRace.round} &middot; {currentSession.name}
			</p>
			<h1 class="text-2xl font-bold text-white">{currentRace.race_name}</h1>
			<p class="text-f1-text-muted text-sm">{currentRace.circuit} &middot; {currentRace.country}</p>
		</div>
	{:else}
		<h1 class="text-2xl font-bold text-white mb-6">Watch</h1>
	{/if}

	<!-- Players Grid -->
	{#if players.length > 0}
		<div class="grid {layoutClass} gap-2 mb-6">
			{#each players as player, i (player.id)}
				<div class="bg-black rounded-lg overflow-hidden relative group" bind:this={player.containerEl} onmousemove={() => onPlayerMouseMove(i)} role="region" aria-label="Player {i + 1}">
					<div class="absolute top-2 left-2 z-10 bg-black/60 rounded px-2 py-0.5 text-xs text-white">
						{player.siteName}{#if player.quality} &middot; {player.quality}{/if}
					</div>
					<button onclick={() => removePlayer(i)} class="absolute top-2 right-2 z-10 bg-black/60 rounded-full w-6 h-6 flex items-center justify-center text-white hover:text-f1-red transition-colors" aria-label="Close">
						<svg class="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
					</button>

					{#if player.streamType === 'embed'}
						<iframe src={player.embedUrl} class="w-full aspect-video bg-black" allow="autoplay; encrypted-media; fullscreen; picture-in-picture" allowfullscreen frameborder="0" scrolling="yes" title="{player.siteName}"></iframe>
					{:else}
						<video bind:this={player.videoEl} class="w-full aspect-video bg-black" playsinline></video>
					{/if}

					{#if player.streamType !== 'embed'}
						<div class="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/80 to-transparent px-3 py-2 transition-opacity duration-300 {player.showControls ? 'opacity-100' : 'opacity-0'}">
							<div class="flex items-center gap-2">
								<button onclick={() => togglePlay(i)} class="text-white hover:text-f1-red transition-colors">
									{#if player.isPlaying}<svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M6 4h4v16H6V4zm8 0h4v16h-4V4z"/></svg>{:else}<svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>{/if}
								</button>
								<button onclick={() => toggleMute(i)} class="text-white hover:text-f1-red transition-colors">
									{#if player.isMuted}<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>{:else}<svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>{/if}
								</button>
								<input type="range" min="0" max="1" step="0.05" value={player.volume} oninput={(e) => setVolume(i, e)} class="w-16 h-1 accent-f1-red" />
								<div class="flex-1"></div>
								{#if castAvailable}<button onclick={() => castStream(i)} class="text-white hover:text-f1-red transition-colors"><svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M1 18v3h3c0-1.66-1.34-3-3-3zm0-4v2c2.76 0 5 2.24 5 5h2c0-3.87-3.13-7-7-7zm0-4v2c4.97 0 9 4.03 9 9h2c0-6.08-4.93-11-11-11zm20-7H3c-1.1 0-2 .9-2 2v3h2V5h18v14h-7v2h7c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2z"/></svg></button>{/if}
								<button onclick={() => toggleFullscreen(i)} class="text-white hover:text-f1-red transition-colors"><svg class="w-4 h-4" fill="currentColor" viewBox="0 0 24 24"><path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg></button>
							</div>
						</div>
					{/if}

					{#if player.error}
						<div class="absolute bottom-12 left-2 right-2 bg-red-900/80 rounded px-2 py-1 text-xs text-red-300">{player.error}</div>
					{/if}
				</div>
			{/each}
		</div>
	{/if}

	<!-- Stream List -->
	{#if loading}
		<div class="flex items-center justify-center py-20">
			<div class="w-8 h-8 border-2 border-f1-red border-t-transparent rounded-full animate-spin"></div>
			<span class="ml-3 text-f1-text-muted">Loading streams...</span>
		</div>
	{:else if errorMsg}
		<div class="bg-red-900/30 border border-red-700 rounded-lg p-4 text-center">
			<p class="text-red-300">Failed to load streams: {errorMsg}</p>
			<button onclick={loadData} class="mt-2 px-4 py-1 bg-f1-red text-white rounded text-sm">Retry</button>
		</div>
	{:else if streamsData}
		<!-- Controls bar -->
		<div class="flex items-center justify-between mb-4">
			<div class="flex items-center gap-2">
				<h2 class="text-lg font-semibold text-white">Streams</h2>
				<span class="text-f1-text-muted text-sm">({streamsData.count})</span>
			</div>
			<div class="flex items-center gap-3">
				<!-- Filter pills -->
				<div class="flex gap-1">
					{#each [['all', 'All'], ['live', 'Live'], ['m3u8', 'Direct'], ['embed', 'Embed']] as [key, label]}
						<button
							onclick={() => activeFilter = key}
							class="px-2.5 py-1 rounded-full text-xs font-medium transition-colors {activeFilter === key ? 'bg-f1-red text-white' : 'bg-f1-surface text-f1-text-muted hover:text-white'}"
						>{label}</button>
					{/each}
				</div>
				{#if players.length > 0}
					<span class="text-xs text-f1-text-muted">{players.length}/{MAX_PLAYERS}</span>
				{/if}
				<button onclick={loadData} class="text-xs text-f1-text-muted hover:text-white transition-colors uppercase tracking-wider">Refresh</button>
			</div>
		</div>

		{#if filteredGroups.length === 0}
			<div class="bg-f1-surface border border-f1-border rounded-lg p-8 text-center">
				<p class="text-f1-text-muted">No streams available.</p>
				<a href="/" class="inline-block mt-4 px-4 py-2 bg-f1-surface-hover border border-f1-border rounded text-sm text-white hover:border-f1-red transition-colors">View Schedule</a>
			</div>
		{:else}
			<!-- Grouped by category -->
			{@const categories = [...new Set(filteredGroups.map(g => g.category))]}
			{#each categories as cat}
				{@const catGroups = filteredGroups.filter(g => g.category === cat)}
				<div class="mb-6">
					<div class="flex items-center gap-2 mb-3">
						<span class="text-base">{categoryIcon(cat)}</span>
						<h3 class="text-sm font-semibold text-f1-text-muted uppercase tracking-wider">{categoryLabel(cat)}</h3>
						<div class="flex-1 h-px bg-f1-border"></div>
						<span class="text-xs text-f1-text-muted">{catGroups.reduce((n, g) => n + g.streams.length, 0)} sources</span>
					</div>

					<div class="grid gap-3 {catGroups.length > 2 ? 'sm:grid-cols-2 lg:grid-cols-3' : catGroups.length === 2 ? 'sm:grid-cols-2' : ''}">
						{#each catGroups as group}
							<div class="bg-f1-surface border border-f1-border rounded-lg overflow-hidden hover:border-f1-border transition-colors">
								<!-- Group header -->
								<div class="px-4 py-3 border-b border-f1-border">
									<div class="flex items-center justify-between">
										<h4 class="text-sm font-medium text-white truncate">{group.name}</h4>
										<div class="flex items-center gap-1.5 shrink-0 ml-2">
											{#if group.hasM3u8}
												<span class="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded bg-green-600/20 text-green-400 border border-green-500/30">HLS</span>
											{/if}
											{#if group.hasLive}
												<span class="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded bg-f1-red/20 text-f1-red border border-f1-red/30">Live</span>
											{/if}
										</div>
									</div>
									<p class="text-xs text-f1-text-muted mt-0.5">{group.streams.length} source{group.streams.length !== 1 ? 's' : ''}</p>
								</div>

								<!-- Sources list -->
								<div class="divide-y divide-f1-border">
									{#each group.streams as stream}
										{@const active = isStreamActive(stream.stream_type === 'embed' ? stream.embed_url : stream.url)}
										<div class="px-4 py-2 flex items-center gap-3 {active ? 'bg-f1-red/5' : 'hover:bg-f1-surface-hover'} transition-colors">
											<div class="flex-1 min-w-0">
												<div class="flex items-center gap-1.5 flex-wrap">
													<span class="text-[10px] font-medium px-1.5 py-0.5 rounded border {sourceColor(stream.site_key)}">{stream.site_name}</span>
													{#if stream.stream_type === 'm3u8'}
														<span class="text-[9px] text-green-400">direct</span>
													{/if}
													{#if stream.quality}
														<span class="text-[10px] text-f1-text-muted">{stream.quality}</span>
													{/if}
													{#if active}
														<span class="text-[9px] font-bold text-green-400">PLAYING</span>
													{/if}
												</div>
											</div>
											{#if !active}
												<button
													onclick={() => playStream(stream)}
													class="px-3 py-1 rounded text-xs font-medium transition-colors {stream.stream_type === 'm3u8' ? 'bg-green-600 hover:bg-green-700 text-white' : 'bg-f1-surface-hover hover:bg-f1-red text-f1-text-muted hover:text-white border border-f1-border hover:border-f1-red'}"
												>
													{stream.stream_type === 'm3u8' ? 'Play' : 'Watch'}
												</button>
											{/if}
										</div>
									{/each}
								</div>
							</div>
						{/each}
					</div>
				</div>
			{/each}
		{/if}
	{/if}
</div>
