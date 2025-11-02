# music.py
import asyncio
import tempfile
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple, Dict
from urllib.parse import urlparse

import aiohttp
import yt_dlp
import wavelink
from wavelink import (
    LavalinkException,
    LoadTrackError,
    YouTubePlaylist,
    YouTubeTrack,
    YouTubeMusicTrack,
    SoundCloudTrack,
)
from wavelink.ext.spotify import SpotifyTrack

import discord
from discord import app_commands
from discord.ext import commands

import async_timeout

from ._classes import Provider
from .errors import MustBeSameChannel
from .paginator import Paginator
from .player import DisPlayer

# --- CONFIG: Edit this value to point to your gist page (not raw) ---
# Use the gist page URL or just the gist id. Example:
_GIST_PAGE_OR_ID = "https://gist.github.com/Fttristan/002c3a85ca65cb2a80c0927a1cb0da61"
_GIST_FILENAME = "gistfile1.txt"
GIST_API_BASE = "https://api.github.com/gists"


# ---------- helper utilities ----------

def _ydl_extract_sync(url_or_query: str, cookiefile: Optional[str] = None, ytdl_opts: Optional[dict] = None) -> Dict:
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "source_address": "0.0.0.0",
    }
    if ytdl_opts:
        opts.update(ytdl_opts)
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url_or_query, download=False)
    return info


def _choose_audio_url_from_info(info: Dict) -> Optional[Tuple[str, Dict]]:
    if not info:
        return None
    if "entries" in info and info["entries"]:
        entry = info["entries"][0]
    else:
        entry = info
    if entry.get("url") and not entry.get("is_live", False):
        return entry["url"], entry
    formats = entry.get("formats") or []
    audio_formats = [f for f in formats if f.get("acodec") != "none"]
    if not audio_formats:
        return None
    best = sorted(audio_formats, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0), reverse=True)[0]
    return best.get("url"), entry


def _is_url(text: str) -> bool:
    try:
        p = urlparse(text)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


async def get_gist_id_from_url(raw_or_page: str) -> Optional[str]:
    if not raw_or_page:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{8,}", raw_or_page):
        return raw_or_page
    m = re.search(r"/([0-9a-fA-F]{8,})(?:/|$)", raw_or_page)
    if m:
        return m.group(1)
    parts = raw_or_page.rstrip("/").split("/")
    if parts:
        last = parts[-1]
        if re.fullmatch(r"[0-9a-fA-F]{8,}", last):
            return last
    return None


async def get_gist_raw_url(session: aiohttp.ClientSession, gist_page_or_id: str, filename: str = "gistfile1.txt") -> Optional[str]:
    gist_id = await get_gist_id_from_url(gist_page_or_id)
    if not gist_id:
        return None
    api_url = f"{GIST_API_BASE}/{gist_id}"
    try:
        async with session.get(api_url, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None
    files = data.get("files") or {}
    file_entry = files.get(filename)
    if not file_entry:
        if len(files) == 1:
            file_entry = next(iter(files.values()))
        else:
            return None
    return file_entry.get("raw_url")


# ---------- Music cog ----------

class Music(commands.Cog):
    """
    Music cog — uses internal gist config (_GIST_PAGE_OR_ID) to resolve latest raw cookie file.
    Always resolves queries via yt-dlp (search-if-not-url then resolve), caches cookies in memory,
    exposes /refreshcookies to force re-fetch.
    """

    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        # Use the internal constant for gist source
        self.cookie_gist_source: Optional[str] = _GIST_PAGE_OR_ID
        self.cookie_filename: str = _GIST_FILENAME
        self._cached_cookies_text: Optional[str] = None
        self._cached_cookies_fetched_at: Optional[float] = None
        self._ydl_executor = ThreadPoolExecutor(max_workers=2)
        self.bot.loop.create_task(self.start_nodes())

    def get_nodes(self):
        return sorted(wavelink.NodePool._nodes.values(), key=lambda n: len(n.players))

    # --- cookie fetch / cache / refresh (resolves gist page to current raw URL) ---

    async def _fetch_cookies_text(self) -> Optional[str]:
        if self._cached_cookies_text is not None:
            return self._cached_cookies_text
        if not self.cookie_gist_source:
            return None
        try:
            async with aiohttp.ClientSession() as sess:
                candidate = self.cookie_gist_source
                if "gist.githubusercontent.com" not in candidate and "raw" not in candidate:
                    resolved_raw = await get_gist_raw_url(sess, candidate, filename=self.cookie_filename)
                    if not resolved_raw:
                        return None
                    candidate = resolved_raw
                async with sess.get(candidate, timeout=10) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        self._cached_cookies_text = text
                        self._cached_cookies_fetched_at = asyncio.get_event_loop().time()
                        return text
        except Exception:
            return None
        return None

    async def _refresh_cookies(self) -> Tuple[bool, Optional[str]]:
        if not self.cookie_gist_source:
            return False, "No cookie source configured inside the cog"
        try:
            async with aiohttp.ClientSession() as sess:
                candidate = self.cookie_gist_source
                if "gist.githubusercontent.com" not in candidate and "raw" not in candidate:
                    resolved_raw = await get_gist_raw_url(sess, candidate, filename=self.cookie_filename)
                    if not resolved_raw:
                        return False, "Failed to resolve gist raw URL via GitHub API"
                    candidate = resolved_raw
                async with sess.get(candidate, timeout=10) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        self._cached_cookies_text = text
                        self._cached_cookies_fetched_at = asyncio.get_event_loop().time()
                        return True, None
                    return False, f"HTTP {resp.status}"
        except Exception as exc:
            return False, str(exc)

    # --- yt-dlp extraction (runs in thread) ---

    async def _extract_with_ytdlp(self, query: str, cookies_text: Optional[str] = None) -> Optional[Dict]:
        loop = asyncio.get_event_loop()
        tmp_cookie_path = None
        try:
            if cookies_text:
                tf = tempfile.NamedTemporaryFile(delete=False, prefix="ytdlp_cookies_", suffix=".txt")
                tf.write(cookies_text.encode("utf-8"))
                tf.flush()
                tf.close()
                tmp_cookie_path = tf.name
            info = await loop.run_in_executor(self._ydl_executor, _ydl_extract_sync, query, tmp_cookie_path, None)
            return info
        finally:
            if tmp_cookie_path:
                try:
                    os.unlink(tmp_cookie_path)
                except Exception:
                    pass

    async def _resolve_query_to_url_with_ytdlp(self, raw_query: str) -> Optional[Tuple[str, Dict]]:
        cookies = await self._fetch_cookies_text()
        query = raw_query.strip()
        if not _is_url(query):
            ytdlp_query = f"ytsearch:{query}"
        else:
            ytdlp_query = query
        info = None
        try:
            info = await self._extract_with_ytdlp(ytdlp_query, cookies)
        except Exception:
            try:
                info = await self._extract_with_ytdlp(ytdlp_query, None)
            except Exception:
                return None
        if not info:
            return None
        chosen = _choose_audio_url_from_info(info)
        if not chosen:
            if "entries" in info and info["entries"]:
                entry = info["entries"][0]
                url = entry.get("webpage_url") or entry.get("url")
                if url:
                    try:
                        info2 = await self._extract_with_ytdlp(url, cookies)
                        if info2:
                            return _choose_audio_url_from_info(info2)
                    except Exception:
                        pass
            return None
        return chosen

    async def play_direct_url_on_player(self, player: DisPlayer, direct_url: str, meta: Optional[Dict] = None) -> bool:
        try:
            res = player.play(direct_url)
            if asyncio.iscoroutine(res):
                await res
            if meta:
                try:
                    player._source = meta
                except Exception:
                    pass
            return True
        except Exception:
            try:
                node = next(iter(wavelink.NodePool._nodes.values()))
                await node.play(player.player_id, direct_url)
                if meta:
                    try:
                        player._source = meta
                    except Exception:
                        pass
                return True
            except Exception:
                return False

    # --- voice helpers --- #

    async def _ensure_voice_for_user(self, interaction: discord.Interaction) -> Tuple[Optional[DisPlayer], Optional[discord.Member]]:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return None, None
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message("You must be in a voice channel to use this command.", ephemeral=True)
            return None, None
        player = interaction.guild.voice_client
        return player, member

    async def _connect(self, interaction: discord.Interaction) -> Optional[DisPlayer]:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.guild is None:
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return None
        channel = member.voice.channel if member.voice else None
        if channel is None:
            await interaction.response.send_message("You must be in a voice channel.", ephemeral=True)
            return None
        if interaction.guild.voice_client:
            await interaction.response.send_message("Already connected.", ephemeral=True)
            return interaction.guild.voice_client
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            player: DisPlayer = await channel.connect(cls=DisPlayer)
            self.bot.dispatch("dismusic_player_connect", player)
            player.bound_channel = interaction.channel
            player.bot = self.bot
            await interaction.followup.send(f"✅ Connected to `{channel.name}`")
            return player
        except (asyncio.TimeoutError, discord.ClientException):
            await interaction.followup.send("Failed to connect to voice channel.")
            return None

    # --- main play workflow --- #

    async def play_track_interaction(self, interaction: discord.Interaction, query: str, provider: Optional[str] = None):
        if not interaction.response.is_done():
            await interaction.response.defer()
        player: DisPlayer = interaction.guild.voice_client
        if not player:
            await interaction.followup.send("Player is not connected. Use /connect first.")
            return
        member = interaction.user
        if member.voice is None or member.voice.channel.id != player.channel.id:
            await interaction.followup.send("You must be in the same voice channel as the player.", ephemeral=True)
            return
        query = query.strip("<>").strip()
        if not query:
            await interaction.followup.send("Empty query provided.", ephemeral=True)
            return
        msg = await interaction.followup.send(f"Resolving `{query}` with yt-dlp :mag_right:")
        resolved = await self._resolve_query_to_url_with_ytdlp(query)
        if resolved:
            direct_url, meta = resolved
            ok = await self.play_direct_url_on_player(player, direct_url, meta)
            if ok:
                await msg.edit(content=f"Playing `{meta.get('title') or query}` (resolved via yt-dlp).")
                return
            else:
                await msg.edit(content="Resolved with yt-dlp but failed to play the direct stream URL. Trying provider fallback...")
        else:
            await msg.edit(content="yt-dlp failed to resolve the query. Trying provider fallback...")
        track_providers = {
            "yt": YouTubeTrack,
            "ytpl": YouTubePlaylist,
            "ytmusic": YouTubeMusicTrack,
            "soundcloud": SoundCloudTrack,
            "spotify": SpotifyTrack,
        }
        provider_key = provider or getattr(player, "track_provider", None)
        if provider_key == "yt" and "playlist" in query:
            provider_key = "ytpl"
        provider_cls = (
            track_providers.get(provider_key)
            if provider_key
            else track_providers.get(getattr(player, "track_provider", "yt"))
        )
        nodes = self.get_nodes()
        tracks = []
        for node in nodes:
            try:
                async with async_timeout.timeout(20):
                    tracks = await provider_cls.search(query, node=node)
                    break
            except asyncio.TimeoutError:
                self.bot.dispatch("dismusic_node_fail", node)
                wavelink.NodePool._nodes.pop(node.identifier, None)
                continue
            except (LavalinkException, LoadTrackError):
                continue
        if not tracks:
            await msg.edit(content="No song/track found with given query.")
            return
        if isinstance(tracks, YouTubePlaylist):
            tracks = tracks.tracks
            for t in tracks:
                await player.queue.put(t)
            await msg.edit(content=f"Added `{len(tracks)}` songs to queue.")
        else:
            track = tracks[0]
            await msg.edit(content=f"Added `{track.title}` to queue.")
            await player.queue.put(track)
        if not player.is_playing():
            await player.do_next()

    # --- node startup & commands (connect, play group, refreshcookies, etc.) --- #

    async def start_nodes(self):
        await self.bot.wait_until_ready()
        spotify_credential = getattr(self.bot, "spotify_credentials", {"client_id": "", "client_secret": ""})
        for config in getattr(self.bot, "lavalink_nodes", []):
            try:
                node: wavelink.Node = await wavelink.NodePool.create_node(
                    bot=self.bot,
                    **config,
                    spotify_client=wavelink.ext.spotify.SpotifyClient(**spotify_credential),
                )
                print(f"[dismusic] INFO - Created node: {node.identifier}")
            except Exception:
                print(f"[dismusic] ERROR - Failed to create node {config.get('host')}:{config.get('port')}")

    @app_commands.command(name="connect", description="Connect the player to your voice channel")
    async def connect(self, interaction: discord.Interaction):
        await self._connect(interaction)

    play_group = app_commands.Group(name="play", description="Play or add a song to the queue")

    @play_group.command(name="query", description="Play or add a song (default: YouTube)")
    @app_commands.describe(query="Search query or URL")
    async def play(self, interaction: discord.Interaction, query: str):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return
        if not player:
            player = await self._connect(interaction)
            if not player:
                return
        await self.play_track_interaction(interaction, query, provider=None)

    @play_group.command(name="youtube", description="Play a YouTube track (always resolved with yt-dlp)")
    @app_commands.describe(query="Search query or URL")
    async def youtube(self, interaction: discord.Interaction, query: str):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return
        if not player:
            player = await self._connect(interaction)
            if not player:
                return
        await self.play_track_interaction(interaction, query, provider="yt")

    @app_commands.command(name="refreshcookies", description="Refresh yt-dlp cookies from configured gist")
    @app_commands.describe(force="Force fetch even if cache exists")
    async def refreshcookies(self, interaction: discord.Interaction, force: bool = False):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not self.cookie_gist_source:
            await interaction.followup.send("No cookie source configured inside the cog.", ephemeral=True)
            return
        if self._cached_cookies_text and not force:
            ts = self._cached_cookies_fetched_at or 0
            await interaction.followup.send(f"Cookies already cached (fetched at {ts}). Use force=True to re-fetch.", ephemeral=True)
            return
        success, err = await self._refresh_cookies()
        if success:
            await interaction.followup.send("Cookies refreshed and cached in memory.", ephemeral=True)
        else:
            await interaction.followup.send(f"Failed to refresh cookies: {err}", ephemeral=True)

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return
        player = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("Player is not connected.", ephemeral=True)
            return
        await player.invoke_player(interaction)

    async def _ensure_voice_for_user(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return None, None
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message("You must be in a voice channel to use this command.", ephemeral=True)
            return None, None
        player = interaction.guild.voice_client
        return player, member


# Module-level setup
async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
