"""Updates an Plex library whenever the beets library is changed.

Plex Home users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        port: 32400
        token: token
"""

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlencode, urljoin
from xml.etree import ElementTree

import beets
import requests
from beets.dbcore.query import BLOB_TYPE, InQuery
from beets.plugins import BeetsPlugin
from plexapi.exceptions import BadRequest, NotFound, Unauthorized
from plexapi.server import PlexServer


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


def update_plex_library(
    server: PlexServer,
    library_name: str,
) -> None:
    """Sends request to the Plex api to start a library refresh."""
    try:
        music_library = server.library.section(library_name)
        music_library.refresh()
        beets.ui.print_(f"Plex music library '{library_name}' refresh started.")
    except NotFound:
        raise ValueError(f"Plex music library '{library_name}' not found.")
    except BadRequest as e:
        beets.ui.print_(
            f"Failed to refresh Plex library '{library_name}': {e}", fg="red"
        )
        raise
    except requests.exceptions.RequestException as e:
        beets.ui.print_(f"Network error during Plex library refresh: {e}", fg="red")
        raise


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
                            beets.ui.print_(f"Plex path found: {full_path}", fg="blue")
        return item_paths
    except NotFound:
        beets.ui.print_(f"Plex playlist '{playlist_name}' not found.", fg="yellow")
        return []
    except Exception as e:
        beets.ui.print_(
            f"Error fetching Plex playlist '{playlist_name}': {e}", fg="red"
        )
        raise


def append_token(url: str, token: str) -> str:
    """Appends the Plex Home token to the api call if required."""
    if token:
        url += f"?{urlencode({'X-Plex-Token': token})}"
    return url


def get_protocol(secure: bool) -> str:
    if secure:
        return "https"
    else:
        return "http"


def is_m3u_file(path: str) -> bool:
    return Path(path).suffix.lower() in {".m3u", ".m3u8"}


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
            beets.ui.print_(
                f"Error setting up Plex playlist query for '{playlist_name}': {e}",
                fg="red",
            )
            self.playlist_item_paths = []
            super().__init__("path", [])


class PlexQueryPlugin(BeetsPlugin):
    item_queries = {"plexquery-playlist": PlexPlaylistQuery}

    def __init__(self):
        super().__init__()
        self.config.add(
            {
                "auto": False,
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

        if self.config["auto"]:
            self.register_listener("item_moved", self.item_moved)
            self.register_listener("item_removed", self.item_removed)
            self.register_listener("cli_exit", self.cli_exit)

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
        self.register_listener("database_change", self.listen_for_db_change)

    def item_moved(self, item, source, destination) -> None:
        self.changes[source] = destination

    def item_removed(self, item) -> None:
        if not os.path.exists(beets.util.syspath(item.path)):
            self.changes[item.path] = None

    def cli_exit(self, lib: beets.library.Library) -> None:
        for playlist in self.find_playlists():
            self._log.info("Updating playlist: {}", playlist)
            base_dir = beets.util.bytestring_path(
                self.relative_to if self.relative_to else os.path.dirname(playlist)
            )

            try:
                self.update_playlist(playlist, base_dir)
            except beets.util.FilesystemError:
                self._log.error("Failed to update playlist: {}", playlist)

    def find_playlists(self):
        """Find M3U playlists in the playlist directory."""
        playlist_dir = beets.util.syspath(self.playlist_dir)
        try:
            dir_contents = os.listdir(playlist_dir)
        except OSError:
            self._log.warning("Unable to open playlist directory {.playlist_dir}", self)
            return

        for filename in dir_contents:
            if is_m3u_file(filename):
                yield os.path.join(self.playlist_dir, filename)

    def update_playlist(self, filename: str, base_dir: bytes) -> None:
        """Find M3U playlists in the specified directory."""
        changes = 0
        deletions = 0

        with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as tempfp:
            new_playlist = tempfp.name
            with open(filename, mode="rb") as fp:
                for line in fp:
                    original_path = line.rstrip(b"\r\n")

                    is_relative = not os.path.isabs(line)
                    if is_relative:
                        lookup = os.path.join(base_dir, original_path)
                    else:
                        lookup = original_path

                    try:
                        new_path = self.changes[beets.util.normpath(lookup)]
                    except KeyError:
                        if self.config["forward_slash"]:
                            line = beets.util.path_as_posix(line)
                        tempfp.write(line)
                    else:
                        if new_path is None:
                            deletions += 1
                            continue

                        changes += 1
                        if is_relative:
                            new_path = os.path.relpath(new_path, base_dir)
                        line = line.replace(original_path, new_path)
                        if self.config["forward_slash"]:
                            line = beets.util.path_as_posix(line)
                        tempfp.write(line)

        if changes or deletions:
            self._log.info(
                "Updated playlist {} ({} changes, {} deletions)",
                filename,
                changes,
                deletions,
            )
            beets.util.copy(new_playlist, filename, replace=True)
        beets.util.remove(new_playlist)

    def listen_for_db_change(self, lib: beets.library.Library, model) -> None:
        """Listens for beets db change and register the update for the end"""
        self.register_listener("cli_exit", self.update)

    def update(self, lib: beets.library.Library) -> None:
        """When the client exists try to send refresh request to Plex server."""
        self._log.info("Updating Plex library...")

        plex_config = beets.config["plex"]
        host = plex_config["host"].get()
        port = plex_config["port"].get()
        token = plex_config["token"].get()
        secure = plex_config["secure"].get(bool)

        try:
            plex_server = get_plex_server(host, port, token, secure)
            update_plex_library(
                plex_server,
                plex_config["library_name"].get(),
            )
            self._log.info("... started.")

        except (ValueError, Exception) as e:
            self._log.error(f"Plex update failed: {e}")
