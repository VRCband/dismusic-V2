import asyncio
import async_timeout
import typing

import wavelink
from wavelink import (
    LavalinkException,
    LoadTrackError,
    SoundCloudTrack,
    YouTubeMusicTrack,
    YouTubePlaylist,
    YouTubeTrack,
)
from wavelink.ext import spotify
from wavelink.ext.spotify import SpotifyTrack

import discord
from discord import app_commands
from discord.ext import commands

from ._classes import Provider
from .errors import MustBeSameChannel
from .paginator import Paginator
from .player import DisPlayer


class Music(commands.Cog):
    """Music commands converted to slash commands (discord.py v2 app_commands)."""

    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        self.bot.loop.create_task(self.start_nodes())

    def get_nodes(self):
        return sorted(wavelink.NodePool._nodes.values(), key=lambda n: len(n.players))

    async def _ensure_voice_for_user(
        self, interaction: discord.Interaction
    ) -> typing.Tuple[typing.Optional[DisPlayer], typing.Optional[discord.Member]]:
        """Return (player, member) or send an ephemeral error and return (None, None)."""
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Command must be used in a guild.", ephemeral=True
            )
            return None, None

        vc = member.voice
        if not vc:
            await interaction.response.send_message(
                "You must be in a voice channel to use this command.", ephemeral=True
            )
            return None, None

        player = interaction.guild.voice_client
        return player, member

    async def _connect(self, interaction: discord.Interaction) -> typing.Optional[DisPlayer]:
        """Callable connect helper; returns the connected DisPlayer or None."""
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

        # Defer so we can follow up with messages
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

    async def play_track_interaction(
        self,
        interaction: discord.Interaction,
        query: str,
        provider: typing.Optional[str] = None,
    ):
        """Play logic adapted from original play_track, using interaction for responses."""
        # Defer only if not yet responded
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
        # send initial searching message via followup to allow editing later
        msg = await interaction.followup.send(f"Searching for `{query}` :mag_right:")

        track_provider_key = provider if provider else getattr(player, "track_provider", None)

        # detect playlist url-ish
        if track_provider_key == "yt" and "playlist" in query:
            track_provider_key = "ytpl"

        provider_cls = (
            track_providers.get(track_provider_key)
            if track_provider_key
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

        # player.do_next is expected to be awaitable
        if not player.is_playing():
            # ensure do_next is awaited; do_next in player.py must be async
            await player.do_next()

    async def start_nodes(self):
        await self.bot.wait_until_ready()
        spotify_credential = getattr(
            self.bot, "spotify_credentials", {"client_id": "", "client_secret": ""}
        )

        for config in getattr(self.bot, "lavalink_nodes", []):
            try:
                node: wavelink.Node = await wavelink.NodePool.create_node(
                    bot=self.bot,
                    **config,
                    spotify_client=spotify.SpotifyClient(**spotify_credential),
                )
                print(f"[dismusic] INFO - Created node: {node.identifier}")
            except Exception:
                print(
                    f"[dismusic] ERROR - Failed to create node {config.get('host')}:{config.get('port')}"
                )

    # --- Slash command wrappers and group ---

    @app_commands.command(name="connect", description="Connect the player to your voice channel")
    async def connect(self, interaction: discord.Interaction):
        await self._connect(interaction)

    # Play group
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

    # Setup loader for cog (class-based)
    @classmethod
    async def cog_load(cls):
        return

    @classmethod
    async def setup(cls, bot: commands.Bot):
        await bot.add_cog(Music(bot))


# Module-level setup for discord.ext.commands extension loading
async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
