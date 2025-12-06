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


def get_music_section(
    host: str,
    port: int,
    token: str,
    library_name: str,
    secure: bool,
    ignore_cert_errors: bool,
) -> str | None:
    """Getting the section key for the music library in Plex."""
    api_endpoint = append_token("library/sections", token)
    url = urljoin(f"{get_protocol(secure)}://{host}:{port}", api_endpoint)

    # Sends request.
    r = requests.get(
        url,
        verify=not ignore_cert_errors,
        timeout=10,
    )

    # Parse xml tree and extract music section key.
    tree = ElementTree.fromstring(r.content)
    for child in tree.findall("Directory"):
        if child.get("title") == library_name:
            return child.get("key")


def update_plex(
    host: str,
    port: int,
    token: str,
    library_name: str,
    secure: bool,
    ignore_cert_errors: bool,
) -> requests.Response:
    """Ignore certificate errors if configured to."""
    if ignore_cert_errors:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    """Sends request to the Plex api to start a library refresh.
    """
    # Getting section key and build url.
    section_key = get_music_section(
        host, port, token, library_name, secure, ignore_cert_errors
    )
    api_endpoint = f"library/sections/{section_key}/refresh"
    api_endpoint = append_token(api_endpoint, token)
    url = urljoin(f"{get_protocol(secure)}://{host}:{port}", api_endpoint)

    # Sends request and returns requests object.
    r = requests.get(
        url,
        verify=not ignore_cert_errors,
        timeout=10,
    )
    return r


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
    """Matches files listed by a playlist file."""

    @property
    def subvals(self) -> Sequence[BLOB_TYPE]:
        return [BLOB_TYPE(p) for p in self.pattern]

    def __init__(self, _, pattern: str, __):
        config = beets.config["playlist"]

        # Get the full path to the playlist
        playlist_paths = (
            pattern,
            os.path.abspath(
                os.path.join(
                    config["playlist_dir"].as_filename(),
                    f"{pattern}.m3u",
                )
            ),
        )

        paths = []
        for playlist_path in playlist_paths:
            if not is_m3u_file(playlist_path):
                # This is not am M3U playlist, skip this candidate
                continue

            try:
                f = open(beets.util.syspath(playlist_path), mode="rb")
            except OSError:
                continue

            if config["relative_to"].get() == "library":
                relative_to = beets.config["directory"].as_filename()
            elif config["relative_to"].get() == "playlist":
                relative_to = os.path.dirname(playlist_path)
            else:
                relative_to = config["relative_to"].as_filename()
            relative_to = beets.util.bytestring_path(relative_to)

            for line in f:
                if line[0] == "#":
                    # ignore comments, and extm3u extension
                    continue

                paths.append(
                    beets.util.normpath(os.path.join(relative_to, line.rstrip()))
                )
            f.close()
            break
        super().__init__("path", paths)


class PlexQueryPlugin(BeetsPlugin):
    item_queries = {"plexquery:playlist": PlexPlaylistQuery}

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
                "ignore_cert_errors": False,
            }
        )

        beets.config["plex"]["token"].redact = True
        self.register_listener("database_change", self.listen_for_db_change)

    def item_moved(self, item, source, destination):
        self.changes[source] = destination

    def item_removed(self, item):
        if not os.path.exists(beets.util.syspath(item.path)):
            self.changes[item.path] = None

    def cli_exit(self, lib):
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

    def update_playlist(self, filename, base_dir):
        """Find M3U playlists in the specified directory."""
        changes = 0
        deletions = 0

        with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as tempfp:
            new_playlist = tempfp.name
            with open(filename, mode="rb") as fp:
                for line in fp:
                    original_path = line.rstrip(b"\r\n")

                    # Ensure that path from playlist is absolute
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
                            # Item has been deleted
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

    def listen_for_db_change(self, lib: beets.library.Library, model):
        """Listens for beets db change and register the update for the end"""
        self.register_listener("cli_exit", self.update)

    def update(self, lib: beets.library.Library):
        """When the client exists try to send refresh request to Plex server."""
        self._log.info("Updating Plex library...")

        # Try to send update request.
        try:
            update_plex(
                beets.config["plex"]["host"].get(),
                beets.config["plex"]["port"].get(),
                beets.config["plex"]["token"].get(),
                beets.config["plex"]["library_name"].get(),
                beets.config["plex"]["secure"].get(bool),
                beets.config["plex"]["ignore_cert_errors"].get(bool),
            )
            self._log.info("... started.")

        except requests.exceptions.RequestException:
            self._log.warning("Update failed.")
