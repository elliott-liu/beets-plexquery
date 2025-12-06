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
from beets.util import PathBytes
from plexapi.exceptions import NotFound, Unauthorized
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
        raise ValueError(f"Failed to connect to Plex server at {baseurl}: {e}")


class PlexPlaylistQuery(InQuery[bytes]):
    """Matches files listed by a Plex playlist."""

    @property
    def subvals(self) -> Sequence[BLOB_TYPE]:
        return [BLOB_TYPE(p) for p in self.playlist_item_paths]

    def __init__(self, _, playlist_name: str, __):
        """
        Initializes the query by fetching items from a Plex playlist.
        The 'pattern' argument here is expected to be the Plex playlist name.
        """
        self._log = logging.getLogger("beets")

        try:
            plex_server = get_plex_server(
                beets.config["plex"]["host"].get(),
                beets.config["plex"]["port"].get(),
                beets.config["plex"]["token"].get(),
                beets.config["plex"]["secure"].get(bool),
            )

            self.playlist_item_paths = self.get_plex_playlist_items_plexapi(
                plex_server,
                playlist_name,
                beets.config["directory"].as_filename(),
                beets.config["plexquery"]["plex_dir"].get(),
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
    ) -> list[PathBytes]:
        """Fetches item paths for a given Plex playlist using plexapi."""
        try:
            playlist = server.playlist(playlist_name)
            item_paths: list[PathBytes] = []

            if not playlist:
                self._log.warning(f"Plex playlist '{playlist_name}' not found.")
                return []

            for item in cast(Playlist, playlist).items():
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
        except NotFound:
            self._log.warning(f"Plex playlist '{playlist_name}' not found.")
            return []
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
                "library_name": "Music",
                "secure": False,
            }
        )
        beets.config["plex"]["token"].redact = True
