"""Updates an Plex library whenever the beets library is changed.

Plex Home users enter the Plex Token to enable updating.
Put something like the following in your config.yaml to configure:
    plex:
        host: localhost
        port: 32400
        token: token
"""

import os
from pathlib import Path

import beets
from beets import dbcore, logging, plugins, ui, util
from plexapi import exceptions
from plexapi.audio import Track
from plexapi.library import LibrarySection
from plexapi.media import Media, MediaPart
from plexapi.playlist import Playlist
from plexapi.server import PlexServer

from beetsplug import utils as utils


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
    plex = PlexServer(baseurl, token, timeout=10)
    return plex


def get_plex_library_section_key(
    plex: PlexServer,
    library_name: str,
) -> int:
    """Retrieves the unique key for a library by its name."""

    try:
        section = plex.library.section(library_name)
        if not isinstance(section, LibrarySection):
            raise utils.ValueError(f"Library '{library_name}' section is invalid.")

        key = section.key
        if not isinstance(key, int) or isinstance(key, bool):
            raise utils.ValueError(
                f"Library '{library_name} section.key '{key}' is invalid."
            )

        return key
    except exceptions.NotFound as e:
        raise utils.NotFound(f"Library '{library_name}' not found.") from e
    except utils.ValueError:
        raise
    except Exception as e:
        raise utils.UnhandledError(
            f"An unexpected error occurred attempting to access Plex library '{library_name}': {e}"
        ) from e


def get_plex_playlists(
    plex: PlexServer,
    library_section_key: int,
) -> list[Playlist]:
    """Retrieves a playlist by its name."""

    try:
        valid_playlists: list[Playlist] = []
        playlists = plex.playlists(
            playlistType="audio", sectionId=library_section_key, sort="title:asc"
        )
        for playlist in playlists:
            if not isinstance(playlist, Playlist):
                raise utils.ValueError(
                    "Playlist from library  server.playlists() is invalid."
                )

            valid_playlists.append(playlist)
        return valid_playlists
    except utils.ValueError:
        raise
    except Exception as e:
        raise utils.UnhandledError(
            f"An unexpected error occurred attempting to retrieve Playlists: {e}"
        ) from e


def get_plex_playlist(
    plex: PlexServer,
    playlist_name: str,
    library_section_key: int,
) -> Playlist:
    """Retrieves a playlist by its name."""

    try:
        playlists = get_plex_playlists(plex, library_section_key)
        playlists_guids = [p.guid for p in playlists]
        playlist = plex.playlist(playlist_name)
        if not isinstance(playlist, Playlist):
            raise utils.ValueError(f"Playlist '{playlist_name}' is invalid.")
        if playlist.guid not in playlists_guids:
            raise utils.ValueError(
                f"Playlist '{playlist_name}' (GUID: {playlist.guid}) is not associated with library section key '{library_section_key}' based on GUID comparison."
            )
        return playlist
    except exceptions.NotFound as e:
        raise utils.NotFound(f"Playlist '{playlist_name}' not found.") from e
    except utils.ValueError:
        raise
    except Exception as e:
        raise utils.UnhandledError(
            f"An unexpected error occurred attempting to access Playlist '{playlist_name}': {e}"
        ) from e


def get_plex_playlist_tracks(
    plex: PlexServer,
    playlist_name: str,
    library_section_key: int,
) -> list[Track]:
    """Retrieves track items for a playlist using its name."""

    try:
        playlist = get_plex_playlist(plex, playlist_name, library_section_key)
        playlist_items = playlist.items()

        if not isinstance(playlist_items, list):
            raise utils.ValueError(
                f"Playlist '{playlist_name}' playlist.items is invalid."
            )

        tracks: list[Track] = []
        for item in playlist_items:
            if not isinstance(item, Track):
                raise utils.ValueError(
                    f"Playlist '{playlist_name}' item '{item}' is not a valid Track."
                )
            tracks.append(item)

        return tracks

    except (utils.NotFound, utils.ValueError):
        raise
    except Exception as e:
        raise utils.UnhandledError(
            f"An unexpected error occurred attempting to access Items in Playlist '{playlist_name}': {e}"
        ) from e


def get_beets_paths_from_plex_tracks(
    tracks: list[Track],
    beets_dir: str,
    plex_dir: str,
    logger: logging.Logger,
) -> list[str]:
    """Converts Plex tracks to beets-compatible paths."""

    plex_paths: list[str] = []

    for track in tracks:
        medias = track.media
        if not isinstance(medias, list):
            raise utils.ValueError(f"Track '{track.guid}' .media is invalid.")

        for media in medias:
            if not isinstance(media, Media):
                raise utils.ValueError(f"Track '{track.guid}' media is invalid.")

            parts = media.parts
            if not isinstance(parts, list):
                raise utils.ValueError(f"Track '{track.guid}' media.parts is invalid.")

            for part in parts:
                if not isinstance(part, MediaPart):
                    raise utils.ValueError(
                        f"Track '{track.guid}' media.parts item is invalid."
                    )

                file = part.file
                if not isinstance(file, str):
                    raise utils.ValueError(
                        f"Track '{track.guid}' media.parts.file is invalid."
                    )

                plex_paths.append(file)

    track_paths: list[str] = []

    for plex_path in plex_paths:
        translated_path = os.fspath(plex_path)
        decoded_plex_dir = os.fspath(plex_dir)
        decoded_beets_dir = os.fspath(beets_dir)

        if (
            decoded_plex_dir
            and decoded_beets_dir
            and translated_path.startswith(decoded_plex_dir)
        ):
            translated_path = translated_path.replace(
                decoded_plex_dir, decoded_beets_dir, 1
            )
            logger.debug(f"Plex path: {plex_path!r} -> {translated_path!r}")
        else:
            # If no mapping or path doesn't start with plex_dir, use original Plex path
            logger.debug(f"Plex path: {plex_path!r}")

        if not os.path.exists(translated_path):
            raise utils.NotFound(
                f"Translated path '{translated_path!r}' does not exist on the filesystem. Skipping."
            )

        track_paths.append(translated_path)

    return track_paths


class PlexPlaylistItemQuery(dbcore.query.InQuery):
    """Matches files listed by a Plex playlist."""

    _log = logging.getLogger("beets.plexquery.PlexPlaylistQuery")

    def __init__(self, field_name: str, pattern: str, fast: bool = True):
        """
        Initializes the query by fetching items from a Plex playlist.
        The 'pattern' argument here is expected to be the Plex playlist name.
        """

        self.track_paths: list[Path] = []

        try:
            plex = get_plex_server(
                beets.config["plex"]["host"].get(),
                beets.config["plex"]["port"].get(),
                beets.config["plex"]["token"].get(),
                beets.config["plex"]["secure"].get(bool),
            )

            library_section_key = get_plex_library_section_key(
                plex,
                beets.config["plex"]["library_name"].get(),
            )

            playlist_name = pattern
            tracks = get_plex_playlist_tracks(
                plex,
                playlist_name,
                library_section_key,
            )

            self.track_paths = [
                Path(p).expanduser().resolve()
                for p in get_beets_paths_from_plex_tracks(
                    tracks,
                    beets.config["directory"].as_filename(),
                    beets.config["plexquery"]["plex_dir"].get(),
                    self._log,
                )
            ]

            for path_obj in self.track_paths:
                if not path_obj.exists():
                    self._log.warning(
                        f"Path '{str(path_obj)!r}' resolved by pathlib does not exist."
                    )

        except utils.NotFound as e:
            self._log.warning(
                f"NotFound exemption attempting to build PlexPlaylistItemQuery: {e}"
            )
        except utils.ValueError as e:
            self._log.error(
                f"ValueError exemption attempting to build PlexPlaylistItemQuery: {e}"
            )
        except utils.UnhandledError as e:
            self._log.error(
                f"UnhandledError exemption attempting to build PlexPlaylistItemQuery: {e}"
            )
        except Exception as e:
            self._log.error(
                f"An unexpected error occurred attempting to build PlexPlaylistItemQuery': {e}",
            )

        super().__init__(
            "path",
            [util.bytestring_path(p) for p in self.track_paths],
        )


class PlexQueryPlugin(plugins.BeetsPlugin):
    item_queries = {"plexquery-playlist": PlexPlaylistItemQuery}
    album_queries = {"plexquery-playlist": PlexPlaylistItemQuery}

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
        cmd = ui.Subcommand("plexquery", help="manage Plex-related queries and tasks")
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
            ui.print_(f"Unknown 'plexquery' command: {subcommand_name}\n")
            self.help(lib, opts, sub_args)

    def help(self, lib, opts, args) -> None:
        ui.print_("Usage: beet plexquery <command>\n")
        ui.print_("Commands:")
        ui.print_("  help      - show this help message")
        ui.print_("  playlists - list available playlists from Plex server")
        ui.print_("\nSee 'beet help plexquery' for more details")

    def list_plex_playlists(self, lib, opts, args) -> None:
        """Beets CLI handler to list all Plex playlists."""

        try:
            plex = get_plex_server(
                beets.config["plex"]["host"].get(),
                beets.config["plex"]["port"].get(),
                beets.config["plex"]["token"].get(),
                beets.config["plex"]["secure"].get(bool),
            )

            library_section_key = get_plex_library_section_key(
                plex,
                beets.config["plex"]["library_name"].get(),
            )

            playlists = get_plex_playlists(plex, library_section_key)
            for playlist in playlists:
                ui.print_(playlist.title)

        except utils.NotFound as e:
            self._log.warning(
                f"NotFound exemption attempting to build PlexQueryPlugin: {e}"
            )
        except utils.ValueError as e:
            self._log.error(
                f"ValueError exemption attempting to build PlexQueryPlugin: {e}"
            )
        except utils.UnhandledError as e:
            self._log.error(
                f"UnhandledError exemption attempting to build PlexQueryPlugin: {e}"
            )
        except Exception as e:
            self._log.error(
                f"An unexpected error occurred attempting to build PlexQueryPlugin': {e}",
            )
