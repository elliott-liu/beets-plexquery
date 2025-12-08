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
        for item_index, item in playlist_items:
            if not isinstance(item, Track):
                raise utils.ValueError(
                    f"Playlist '{playlist_name}' playlist.items[{item_index}] '{item}' is invalid."
                )
            tracks.append(item)

        return tracks

    except (utils.NotFound, utils.ValueError):
        raise
    except Exception as e:
        raise utils.UnhandledError(
            f"An unexpected error occurred attempting to access Items in Playlist '{playlist_name}': {e}"
        ) from e


def get_beets_paths_from_tracks(
    tracks: list[Track],
    beets_dir: str,
    plex_dir: str,
    logger: logging.Logger,
) -> list[util.PathBytes]:
    """Converts Plex tracks to beets-compatible paths."""

    plex_paths: list[str] = []

    for track_index, track in tracks:
        medias = track.media
        if not isinstance(medias, list):
            raise utils.ValueError(
                f"Track '{track.guid}' track[{track_index}].media is invalid."
            )

        for media_index, media in medias:
            if not isinstance(media, Media):
                raise utils.ValueError(
                    f"Track '{track.guid}' track[{track_index}].media[{media_index}] is invalid."
                )

            parts = media.parts
            if not isinstance(parts, list):
                raise utils.ValueError(
                    f"Track '{track.guid}' track[{track_index}].media[{media_index}].parts is invalid."
                )

            for part_index, part in parts:
                if not isinstance(part, MediaPart):
                    raise utils.ValueError(
                        f"Track '{track.guid}' track[{track_index}].media[{media_index}].parts[{part_index}] is invalid."
                    )

                file = part.file
                if not isinstance(file, str):
                    raise utils.ValueError(
                        f"Track '{track.guid}' track[{track_index}].media[{media_index}].parts[{part_index}].file is invalid."
                    )

                plex_paths.append(file)

    beets_paths: list[str] = []

    for plex_path in plex_paths:
        # Ensure plex_path is treated as UTF-8 string
        # If plex_path is already a proper unicode string, .decode() will raise an error.
        # So we try to decode it, if it's bytes, otherwise assume it's already unicode.
        try:
            decoded_plex_path = plex_path.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            decoded_plex_path = plex_path

        # Also decode beets_dir and plex_dir for consistent comparison
        try:
            decoded_plex_dir = plex_dir.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            decoded_plex_dir = plex_dir

        try:
            decoded_beets_dir = beets_dir.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            decoded_beets_dir = beets_dir

        translated_path = decoded_plex_path

        if (
            decoded_plex_dir
            and decoded_beets_dir
            and decoded_plex_path.startswith(decoded_plex_dir)
        ):
            translated_path = decoded_plex_path.replace(
                decoded_plex_dir, decoded_beets_dir, 1
            )
            logger.debug(f"Plex path: {plex_path} -> {translated_path}")
        else:
            # If no mapping or path doesn't start with plex_dir, use original Plex path
            logger.debug(f"Plex path: {plex_path}")

        beets_paths.append(translated_path)

    return [beets.util.bytestring_path(path) for path in beets_paths]


class PlexPlaylistItemQuery(dbcore.query.InQuery):
    """Matches files listed by a Plex playlist."""

    _log = logging.getLogger("beets.plexquery.PlexPlaylistQuery")

    @property
    def subvals(self) -> Sequence[dbcore.query.SQLiteType]:
        return [dbcore.query.BLOB_TYPE(p) for p in self.track_paths]

    def __init__(self, _, playlist_name: str, __):
        """
        Initializes the query by fetching items from a Plex playlist.
        The 'pattern' argument here is expected to be the Plex playlist name.
        """

        self.track_paths: list[util.PathBytes] = []

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

            tracks = get_plex_playlist_tracks(
                plex,
                playlist_name,
                library_section_key,
            )

            self.track_paths = get_beets_paths_from_tracks(
                tracks,
                beets.config["directory"].as_filename(),
                beets.config["plexquery"]["plex_dir"].get(),
                self._log,
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

        super().__init__("path", self.track_paths)


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
