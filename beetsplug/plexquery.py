"""Updates an Plex library whenever the beets library is changed.

Plex Home users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        port: 32400
        token: token
"""

from collections.abc import Sequence

import beets
import requests
from beets.dbcore.query import BLOB_TYPE, InQuery
from beets.plugins import BeetsPlugin
from plexapi.exceptions import NotFound, Unauthorized
from plexapi.server import PlexServer


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


def get_plex_playlist_items_plexapi(
    server: PlexServer,
    playlist_name: str,
) -> list[str]:
    """Fetches item paths for a given Plex playlist using plexapi."""
    try:
        playlist = server.playlist(playlist_name)
        item_paths: list[str] = []
        for item in playlist.items():
            if hasattr(item, "media"):
                for media in item.media:
                    for part in media.parts:
                        full_path = part.file  # This is the server-side filesystem path
                        if full_path:
                            item_paths.append(full_path)
                            beets.ui.print_(f"Plex path found: {full_path}")
        return item_paths
    except NotFound:
        beets.ui.print_(f"Plex playlist '{playlist_name}' not found.")
        return []
    except Exception as e:
        beets.ui.print_(f"Error fetching Plex playlist '{playlist_name}': {e}")
        raise


class PlexPlaylistQuery(InQuery[bytes]):
    """Matches files listed by a Plex playlist."""

    @property
    def subvals(self) -> Sequence[BLOB_TYPE]:
        return [BLOB_TYPE(p.encode("utf-8")) for p in self.playlist_item_paths]

    def __init__(self, _, playlist_name: str, __):
        """
        Initializes the query by fetching items from a Plex playlist.
        The 'pattern' argument here is expected to be the Plex playlist name.
        """
        plex_config = beets.config["plex"]
        host = plex_config["host"].get()
        port = plex_config["port"].get()
        token = plex_config["token"].get()
        secure = plex_config["secure"].get(bool)

        try:
            plex_server = get_plex_server(host, port, token, secure)
            self.playlist_item_paths = get_plex_playlist_items_plexapi(
                plex_server, playlist_name
            )
            super().__init__("path", self.playlist_item_paths)
        except (ValueError, Exception) as e:
            self._log.error(
                f"Error setting up Plex playlist query for '{playlist_name}': {e}",
            )
            self.playlist_item_paths = []
            super().__init__("path", [])


class PlexQueryPlugin(BeetsPlugin):
    item_queries = {"plexquery-playlist": PlexPlaylistQuery}

    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "playlist_dir": ".",
                "relative_to": "playlist",
                "forward_slash": False,
            }
        )

        self.playlist_dir = self.config["playlist_dir"].as_filename()
        self.changes = {}

        if self.config["relative_to"].get() == "library":
            self.relative_to = beets.util.bytestring_path(
                beets.config["directory"].as_filename()
            )
        elif self.config["relative_to"].get() != "playlist":
            self.relative_to = beets.util.bytestring_path(
                self.config["relative_to"].as_filename()
            )
        else:
            self.relative_to = None

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
