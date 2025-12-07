# PlexQuery Plugin

`plexquery` is a plugin which lets you query your [Plex](https://plex.tv/)'s music library.

Firstly, install `beets` with `plexquery` extra:

```console
pip install git+https://github.com/Elliott-Liu/beets-plexquery.git
```

To use it, enable the `plexquery` plugin in your configuration (see
[Using Plugins](https://beets.readthedocs.io/en/stable/plugins.html)). Optionally, configure like this:

```yaml
plex:
    host: "localhost"
    port: 32400
    token: "YOUR_PLEX_TOKEN"

plexquery:
    plex_dir: "/media/Music"
```

## Usage

### Playlist Query

This query type allows you to retrieve items from your Beets library that are present in a specified Plex playlist.

You can reference Plex playlists by their exact name:

```console
beet ls plexquery-playlist:"My Favorite Tracks"
```

Or by their full unique ID:

```console
beet ls plexquery-playlist:"12345" # Example using a Plex playlist ID (ratingKey)
```

A Plex playlist query will use the file paths found in the Plex playlist to
match items in your Beets library. `plexquery-playlist`: submits a regular beets [query](https://beets.readthedocs.io/en/stable/reference/query.html#queries) similar to a [specific fields](https://beets.readthedocs.io/en/stable/reference/query.html#fieldsquery) query.

If you want the list in any particular order, you can use the standard beets query syntax for [sorting](https://beets.readthedocs.io/en/stable/reference/query.html#query-sort):

```console
beet ls plexquery-playlist:"Chill Vibes" artist+ year+
```

Plex playlist queries do not reflect the original order of tracks in the Plex playlist.

## Configuration

The available options under the `plexquery:` section are:

- **plex_dir**: The root path for music files on the Plex server (e.g. if Plex sees `/media/music/artist/album/track.flac`, this should be `/media/music`). This path will be used to replace with your beets `directory`. This option is usually essential if your Plex Media Server and Beets library are on different machines or use different mount points for your music files. If left blank, no path translation will occur. Default: Empty.

The available options under the `plex:` section are:

- **host**: The Plex server name. Default: `localhost`.
- **port**: The Plex server port. Default: `32400`.
- **token**: The Plex Home token. You’ll need to use it when in a Plex Home (see Plex’s own [documentation about tokens](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/)). Default: Empty.
- **library_name**: The name of the Plex library to update. Default: `Music`
- **secure**: Use secure connections to the Plex server. Default: `False`
