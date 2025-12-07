"""Updates an Plex library whenever the beets library is changed.

Plex Home users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        port: 32400
        token: token
"""

from collections.abc import Sequence
from typing import cast

import beets
import requests
from beets import logging
from beets.dbcore.query import BLOB_TYPE, InQuery
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand
from beets.util import PathBytes
from plexapi.exceptions import NotFound, Unauthorized
from plexapi.library import MusicSection
from plexapi.server import Playlist, PlexServer


def get_protocol(secure: bool) -> str:
    if secure:
        return "https"
    else:
        return "http"


def get_plex_server(
    host: str,
    port: int,
    token: str,
    secure: bool,
) -> PlexServer:
    """Connects to and returns a PlexServer object."""
    baseurl = f"{get_protocol(secure)}://{host}:{port}"

    try:
        server = PlexServer(baseurl, token, timeout=10)
        return server
    except (Unauthorized, requests.exceptions.RequestException) as e:
        raise ValueError(f"Failed to connect to Plex server at '{baseurl}': {e}")


def get_plex_music_library_key(
    server: PlexServer,
    library_name: str,
) -> float:
    """Retrieves the unique key for a Plex music library by its name."""
    try:
        music_library: MusicSection = server.library.section(library_name)
        return music_library.key
    except NotFound:
        raise ValueError(f"Plex music library '{library_name}' not found.")
    except Exception as e:
        raise ValueError(f"Error accessing Plex library '{library_name}': {e}") from e


def filter_playlist_items_by_library(
    playlist: Playlist,
    library_key: float,
):
    """
    Filters items from a Plex playlist, yielding only those belonging
    to the specified Plex library key.
    """
    for item in cast(Playlist, playlist).items():
        if hasattr(item, "librarySectionID") and item.librarySectionID == library_key:
            yield item


class PlexPlaylistQuery(InQuery[bytes]):
    """Matches files listed by a Plex playlist."""

    _log = logging.getLogger("beets.plexquery.PlexPlaylistQuery")

    @property
    def subvals(self) -> Sequence[BLOB_TYPE]:
        return [BLOB_TYPE(p) for p in self.playlist_item_paths]

    def __init__(self, _, playlist_name: str, __):
        """
        Initializes the query by fetching items from a Plex playlist.
        The 'pattern' argument here is expected to be the Plex playlist name.
        """

        try:
            plex_server = get_plex_server(
                beets.config["plex"]["host"].get(),
                beets.config["plex"]["port"].get(),
                beets.config["plex"]["token"].get(),
                beets.config["plex"]["secure"].get(bool),
            )

            library_key = get_plex_music_library_key(
                plex_server,
                beets.config["plex"]["library_name"].get(),
            )

            self.playlist_item_paths = self.get_plex_playlist_items_plexapi(
                plex_server,
                playlist_name,
                beets.config["directory"].as_filename(),
                beets.config["plexquery"]["plex_dir"].get(),
                library_key,
            )
            super().__init__("path", self.playlist_item_paths)
        except (ValueError, Exception) as e:
            self._log.error(
                f"Error setting up Plex playlist query for '{playlist_name}': {e}",
            )
            self.playlist_item_paths = []
            super().__init__("path", [])

    def get_plex_playlist_items_plexapi(
        self,
        server: PlexServer,
        playlist_name: str,
        beets_dir: str,
        plex_dir: str,
        library_key: float,
    ) -> list[PathBytes]:
        """Fetches item paths for a given Plex playlist using plexapi."""
        try:
            try:
                playlist = server.playlist(playlist_name)
            except NotFound:
                self._log.warning(f"Plex playlist '{playlist_name}' not found.")
                return []

            item_paths: list[PathBytes] = []

            for item in filter_playlist_items_by_library(
                cast(Playlist, playlist), library_key
            ):
                if hasattr(item, "media"):
                    for media in item.media:
                        for part in media.parts:
                            full_plex_path = (
                                part.file
                            )  # This is the server-side filesystem path
                            if full_plex_path:
                                translated_path = full_plex_path
                                if (
                                    plex_dir
                                    and beets_dir
                                    and full_plex_path.startswith(plex_dir)
                                ):
                                    translated_path = full_plex_path.replace(
                                        plex_dir, beets_dir, 1
                                    )
                                    self._log.debug(
                                        f"Plex path: {full_plex_path}, Translated to: {translated_path}"
                                    )
                                else:
                                    # If no mapping or path doesn't start with plex_dir, use original Plex path
                                    self._log.debug(
                                        f"Using original Plex path: {full_plex_path}"
                                    )

                                item_paths.append(
                                    beets.util.bytestring_path(translated_path)
                                )
            return item_paths
        except Exception as e:
            self._log.error(
                f"Error fetching Plex playlist '{playlist_name}': {e}",
            )
            raise


class PlexQueryPlugin(BeetsPlugin):
    item_queries = {"plexquery-playlist": PlexPlaylistQuery}

    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "plex_dir": "",
            }
        )

        beets.config["plex"].add(
            {
                "host": "localhost",
                "port": 32400,
                "token": "",
                "secure": False,
            }
        )
        beets.config["plex"]["token"].redact = True

    def commands(self):
        cmd = Subcommand("plexquery", help="manage Plex-related queries and tasks")
        cmd.func = self.command_dispatcher
        return [cmd]

    def command_dispatcher(self, lib, opts, args) -> None:
        """Dispatches 'plexquery' subcommands based on arguments."""
        if not args:
            self.help(lib, opts, args)
            return

        subcommand_name = args[0]
        sub_args = args[1:]

        if subcommand_name == "playlists":
            self.list_plex_playlists(lib, opts, sub_args)
        elif subcommand_name == "help":
            self.help(lib, opts, sub_args)
        else:
            self._log.info(f"Error: Unknown 'plexquery' command: {subcommand_name}")
            self._log.info("See 'beet help plexquery' for available commands.")

    def help(self, lib, opts, args) -> None:
        self._log.info("Usage: beet plexquery <command>")
        self._log.info("Commands:")
        self._log.info("  playlists - list available playlists from Plex server")
        self._log.info("\nSee 'beet help plexquery' for more details.")

    def list_plex_playlists(self, lib, opts, args) -> None:
        """Beets CLI handler to list all Plex playlists."""

        try:
            plex_server = get_plex_server(
                beets.config["plex"]["host"].get(),
                beets.config["plex"]["port"].get(),
                beets.config["plex"]["token"].get(),
                beets.config["plex"]["secure"].get(bool),
            )
        except ValueError as e:
            self._log.error(f"Failed to connect to Plex server: {e}")
            return
        except Exception as e:
            self._log.error(
                f"An unexpected error occurred while connecting to Plex: {e}"
            )
            return

        try:
            library_key = get_plex_music_library_key(
                plex_server,
                beets.config["plex"]["library_name"].get(),
            )
        except ValueError as e:
            self._log.error(f"Failed to get Plex library key: {e}")
            return
        except Exception as e:
            self._log.error(
                f"An unexpected error occurred while getting library key: {e}"
            )
            return

        try:
            playlists = plex_server.playlists()
            library_playlists = []

            for playlist in playlists:
                try:
                    if (
                        next(
                            filter_playlist_items_by_library(
                                cast(Playlist, playlist), library_key
                            ),
                            None,
                        )
                        is not None
                    ):
                        library_playlists.append(playlist)
                except Exception as e:
                    self._log.debug(
                        f"Could not inspect items for playlist '{cast(Playlist, playlist).title}': {e}"
                    )

            if not library_playlists:
                self._log.info("No playlists found on Plex server.")
                return

            self._log.info("Plex Playlists:")
            for playlist in sorted(library_playlists, key=lambda p: p.title):
                self._log.info(f"  - {playlist.title} (ID: {playlist.ratingKey})")

        except Exception as e:
            self._log.error(
                f"An unexpected error occurred while fetching/filtering playlists: {e}"
            )
