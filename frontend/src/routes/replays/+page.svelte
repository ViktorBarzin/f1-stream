<script>
	import { fetchReplays, refreshReplays, getReplayVideoUrl, getReplayDownloadUrl } from '$lib/api.js';
	import { onMount } from 'svelte';

	let replaysData = $state(null);
	let loading = $state(true);
	let refreshing = $state(false);
	let errorMsg = $state(null);
	let expandedEvents = $state(new Set());
	let activeVideo = $state(null); // { eventIdx, sessionType, postIdx, linkIdx }

	onMount(() => {
		loadReplays();
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
