import asyncio
import tempfile
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple, Dict

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


# --- yt-dlp helpers (sync worker + chooser) ---


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

    # If search/playlist returned entries, pick the first
    if "entries" in info and info["entries"]:
        entry = info["entries"][0]
    else:
        entry = info

    # direct 'url' present and not a live stream
    if entry.get("url") and not entry.get("is_live", False):
        return entry["url"], entry

    formats = entry.get("formats") or []
    audio_formats = [f for f in formats if f.get("acodec") != "none"]
    if not audio_formats:
        return None

    best = sorted(audio_formats, key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0), reverse=True)[0]
    return best.get("url"), entry


# --- Music cog ---


class Music(commands.Cog):
    """
    Music cog with:
    - guarded interaction.defer usage
    - yt-dlp fallback using cookie gist raw URL (provided at setup)
    - robust Lavalink node handling and async-timeout fixes
    """

    def __init__(self, bot: commands.Bot, *, cookie_gist_raw_url: Optional[str] = None):
        self.bot: commands.Bot = bot
        self.cookie_gist_raw_url = cookie_gist_raw_url
        self.bot.loop.create_task(self.start_nodes())
        self._ydl_executor = ThreadPoolExecutor(max_workers=2)

    def get_nodes(self):
        return sorted(wavelink.NodePool._nodes.values(), key=lambda n: len(n.players))

    # --- cookie fetching / yt-dlp extraction ---

    async def _fetch_cookies_text(self) -> Optional[str]:
        if not self.cookie_gist_raw_url:
            return None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(self.cookie_gist_raw_url, timeout=10) as resp:
                    if resp.status == 200:
                        return await resp.text()
        except Exception:
            return None
        return None

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

    async def play_via_ytdlp(self, player: DisPlayer, query: str) -> bool:
        cookies_text = await self._fetch_cookies_text()
        try:
            info = await self._extract_with_ytdlp(query, cookies_text)
        except Exception:
            return False

        chosen = _choose_audio_url_from_info(info)
        if not chosen:
            return False

        direct_url, meta = chosen

        # Try to play direct URL via player.play (wavelink handles HTTP streams)
        try:
            res = player.play(direct_url)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            # Fallback: node.play if API/version requires it
            try:
                node = next(iter(wavelink.NodePool._nodes.values()))
                await node.play(player.player_id, direct_url)
            except Exception:
                return False

        # Attach metadata for now-playing display
        try:
            player._source = meta
        except Exception:
            pass

        return True

    # --- internal helpers for interactions/voice checks ---

    async def _ensure_voice_for_user(self, interaction: discord.Interaction) -> Tuple[Optional[DisPlayer], Optional[discord.Member]]:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message("This command must be used in a guild.", ephemeral=True)
            return None, None

        vc = member.voice
        if not vc:
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

    # --- main play workflow with fallback --- #

    async def play_track_interaction(self, interaction: discord.Interaction, query: str, provider: Optional[str] = None):
        # Defer only if not already responded
        if not interaction.response.is_done():
            await interaction.response.defer()

        player: DisPlayer = interaction.guild.voice_client
        if not player:
            await interaction.followup.send("Player is not connected. Use /connect first.")
            return

        # Ensure same voice channel
        member = interaction.user
        if member.voice is None or member.voice.channel.id != player.channel.id:
            await interaction.followup.send("You must be in the same voice channel as the player.", ephemeral=True)
            return

        track_providers = {
            "yt": YouTubeTrack,
            "ytpl": YouTubePlaylist,
            "ytmusic": YouTubeMusicTrack,
            "soundcloud": SoundCloudTrack,
            "spotify": SpotifyTrack,
        }

        query = query.strip("<>")
        msg = await interaction.followup.send(f"Searching for `{query}` :mag_right:")

        track_provider_key = provider if provider else getattr(player, "track_provider", None)
        if track_provider_key == "yt" and "playlist" in query:
            track_provider_key = "ytpl"

        provider_cls = (
            track_providers.get(track_provider_key)
            if track_provider_key
            else track_providers.get(getattr(player, "track_provider", "yt"))
        )

        nodes = self.get_nodes()
        tracks = []

        lavalink_requires_login = False
        for node in nodes:
            try:
                async with async_timeout.timeout(20):
                    tracks = await provider_cls.search(query, node=node)
                    break
            except asyncio.TimeoutError:
                self.bot.dispatch("dismusic_node_fail", node)
                wavelink.NodePool._nodes.pop(node.identifier, None)
                continue
            except (LavalinkException, LoadTrackError) as e:
                txt = str(e).lower()
                if "login" in txt or "requires login" in txt or "private" in txt:
                    lavalink_requires_login = True
                    break
                continue

        if not tracks and not lavalink_requires_login:
            await msg.edit(content="No song/track found with given query.")
            return

        # YT login-required fallback via yt-dlp
        if lavalink_requires_login:
            success = await self.play_via_ytdlp(player, query)
            if success:
                await msg.edit(content="Playing via yt-dlp fallback.")
                return
            else:
                await msg.edit(content="Failed to load track via yt-dlp fallback.")
                return

        # Playlist handling
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

    # --- Lavalink node startup --- #

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

    # --- Slash command wrappers --- #

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

    @play_group.command(name="youtube", description="Play a YouTube track")
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

    @play_group.command(name="ytmusic", description="Play a YouTube Music track")
    @app_commands.describe(query="Search query or URL")
    async def youtubemusic(self, interaction: discord.Interaction, query: str):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return
        if not player:
            player = await self._connect(interaction)
            if not player:
                return
        await self.play_track_interaction(interaction, query, provider="ytmusic")

    @play_group.command(name="soundcloud", description="Play a SoundCloud track")
    @app_commands.describe(query="Search query or URL")
    async def soundcloud(self, interaction: discord.Interaction, query: str):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return
        if not player:
            player = await self._connect(interaction)
            if not player:
                return
        await self.play_track_interaction(interaction, query, provider="soundcloud")

    @play_group.command(name="spotify", description="Play a Spotify track")
    @app_commands.describe(query="Search query or URL")
    async def spotify(self, interaction: discord.Interaction, query: str):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return
        if not player:
            player = await self._connect(interaction)
            if not player:
                return
        await self.play_track_interaction(interaction, query, provider="spotify")

    @app_commands.command(name="volume", description="Set player volume (0-100)")
    @app_commands.describe(vol="Volume percent (0-100)", forced="Force volume greater than 100")
    async def volume(self, interaction: discord.Interaction, vol: int, forced: bool = False):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return

        player = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("Player is not connected.", ephemeral=True)
            return

        if vol < 0:
            await interaction.response.send_message("Volume can't be less than 0", ephemeral=True)
            return

        if vol > 100 and not forced:
            await interaction.response.send_message("Volume can't be greater than 100", ephemeral=True)
            return

        await player.set_volume(vol)
        await interaction.response.send_message(f"Volume set to {vol} :loud_sound:")

    @app_commands.command(name="stop", description="Stop and disconnect the player")
    async def stop(self, interaction: discord.Interaction):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return

        player = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("Player is not connected.", ephemeral=True)
            return

        await player.destroy()
        await interaction.response.send_message("Stopped the player :stop_button:")
        self.bot.dispatch("dismusic_player_stop", player)

    @app_commands.command(name="pause", description="Pause the player")
    async def pause(self, interaction: discord.Interaction):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return

        player = interaction.guild.voice_client
        if player and player.is_playing():
            if player.is_paused():
                await interaction.response.send_message("Player is already paused.", ephemeral=True)
                return

            await player.set_pause(pause=True)
            self.bot.dispatch("dismusic_player_pause", player)
            await interaction.response.send_message("Paused :pause_button:")
            return

        await interaction.response.send_message("Player is not playing anything.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume the player")
    async def resume(self, interaction: discord.Interaction):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return

        player = interaction.guild.voice_client
        if player and player.is_playing():
            if not player.is_paused():
                await interaction.response.send_message("Player is already playing.", ephemeral=True)
                return

            await player.set_pause(pause=False)
            self.bot.dispatch("dismusic_player_resume", player)
            await interaction.response.send_message("Resumed :musical_note:")
            return

        await interaction.response.send_message("Player is not playing anything.", ephemeral=True)

    @app_commands.command(name="skip", description="Skip to the next song in the queue")
    async def skip(self, interaction: discord.Interaction):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return

        player = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("Player is not connected.", ephemeral=True)
            return

        if getattr(player, "loop", None) == "CURRENT":
            player.loop = "NONE"

        await player.stop()
        self.bot.dispatch("dismusic_track_skip", player)
        await interaction.response.send_message("Skipped :track_next:")

    @app_commands.command(name="seek", description="Seek the player forward/backward by seconds")
    @app_commands.describe(seconds="Seconds to advance (positive) or rewind (negative)")
    async def seek(self, interaction: discord.Interaction, seconds: int):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return

        player = interaction.guild.voice_client
        if player and player.is_playing():
            old_position = player.position
            position = old_position + seconds
            track_length = getattr(getattr(player, "source", None), "length", 0)
            if position > track_length:
                await interaction.response.send_message("Can't seek past the end of the track.", ephemeral=True)
                return

            if position < 0:
                position = 0

            await player.seek(position * 1000)
            self.bot.dispatch("dismusic_player_seek", player, old_position, position)
            await interaction.response.send_message(f"Seeked {seconds} seconds :fast_forward:")
            return

        await interaction.response.send_message("Player is not playing anything.", ephemeral=True)

    @app_commands.command(name="loop", description="Set loop mode: NONE, CURRENT, PLAYLIST")
    @app_commands.describe(loop_type="NONE, CURRENT, or PLAYLIST")
    async def loop(self, interaction: discord.Interaction, loop_type: str = None):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return

        player = interaction.guild.voice_client
        if not player:
            await interaction.response.send_message("Player is not connected.", ephemeral=True)
            return

        result = await player.set_loop(loop_type)
        await interaction.response.send_message(f"Loop has been set to {result} :repeat:")

    @app_commands.command(name="queue", description="Show the player queue")
    async def queue(self, interaction: discord.Interaction):
        player, member = await self._ensure_voice_for_user(interaction)
        if player is None and member is None:
            return

        player = interaction.guild.voice_client
        queue_len = len(getattr(getattr(player, "queue", None), "_queue", ()))
        if not player or queue_len < 1:
            await interaction.response.send_message("Nothing is in the queue.", ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer()
        paginator = Paginator(interaction, player)
        await paginator.start()

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

    # --- helper duplicates used by wrappers --- #

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
    cookie_gist_raw_url = "https://gist.githubusercontent.com/Fttristan/002c3a85ca65cb2a80c0927a1cb0da61/raw/1522484d659317167de77995af4caee5194498b4/gistfile1.txt"
    await bot.add_cog(Music(bot, cookie_gist_raw_url=cookie_gist_raw_url))
