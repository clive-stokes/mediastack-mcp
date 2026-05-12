"""Filesystem scanning and path validation for orphan detection.

Provides safe directory traversal within configured media roots,
path normalisation, and comparison helpers for matching filesystem
paths against Jellyfin library entries.
"""

import fnmatch
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Default media file extensions for orphan detection
DEFAULT_MEDIA_EXTENSIONS = frozenset({
    "mp4", "mkv", "avi", "ts", "m4v", "wmv", "flv", "mov", "webm",  # Video
    "m4a", "mp3", "flac", "ogg", "opus", "wma", "wav", "aac",       # Audio
})

# Paths that must never be scanned, regardless of config
_BLOCKED_PATHS = frozenset({
    "/", "/etc", "/bin", "/sbin", "/usr", "/var", "/tmp",
    "/proc", "/sys", "/dev", "/root", "/home",
})


def validate_scan_path(scan_path: str, media_roots: list[str]) -> str | None:
    """Validate a scan path against allowed media roots.

    Returns an error message if the path is invalid, or None if it's safe.
    """
    if not scan_path:
        return "scan_path is required"

    # Resolve to absolute, follow symlinks
    resolved = str(Path(scan_path).resolve())

    # Block dangerous paths
    if resolved in _BLOCKED_PATHS:
        return f"Path '{resolved}' is blocked for security reasons"

    if not media_roots:
        return "No MEDIA_ROOTS configured. Set the MEDIA_ROOTS environment variable."

    # Check the resolved path is under at least one allowed root
    for root in media_roots:
        root_resolved = str(Path(root).resolve())
        if resolved == root_resolved or resolved.startswith(root_resolved + "/"):
            return None

    allowed = ", ".join(media_roots)
    return f"Path '{scan_path}' is outside allowed media roots: {allowed}"


def scan_directory(
    scan_path: str,
    pattern: str | None = None,
    recursive: bool = True,
    limit: int = 500,
    extensions: frozenset[str] | None = None,
) -> list[dict]:
    """Walk a directory and return file metadata.

    Args:
        scan_path: Directory to scan.
        pattern: Optional glob pattern to filter filenames (e.g. "*.mp4").
        recursive: Whether to recurse into subdirectories (default True).
        limit: Maximum files to return (default 500).
        extensions: If provided, only include files with these extensions (lowercase, no dot).

    Returns:
        List of dicts with path, filename, size_bytes, modified, extension.
    """
    results: list[dict] = []
    scan_root = Path(scan_path)

    if not scan_root.is_dir():
        return results

    if recursive:
        walker = scan_root.rglob("*")
    else:
        walker = scan_root.glob("*")

    for entry in walker:
        if not entry.is_file():
            continue

        # Extension filter
        ext = entry.suffix.lstrip(".").lower()
        if extensions is not None and ext not in extensions:
            continue

        # Glob pattern filter (matched against filename only)
        if pattern and not fnmatch.fnmatch(entry.name, pattern):
            continue

        try:
            stat = entry.stat()
        except OSError:
            continue

        results.append({
            "path": str(entry),
            "filename": entry.name,
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc,
            ).isoformat(),
            "extension": ext,
        })

        if len(results) >= limit:
            break

    return results


class PathMapper:
    """Maps paths between container filesystem and Jellyfin's view.

    When the container mounts /volume1/MagnoliaMedia as /media, and
    Jellyfin sees the same content under /data, we need to translate
    between the two for comparison.
    """

    def __init__(self, mappings: dict[str, str]):
        # Sort by longest prefix first for correct matching
        self._mappings = sorted(
            mappings.items(), key=lambda x: len(x[0]), reverse=True,
        )
        # Build reverse mappings (Jellyfin path -> container path)
        self._reverse = sorted(
            [(v, k) for k, v in mappings.items()],
            key=lambda x: len(x[0]), reverse=True,
        )

    def container_to_jellyfin(self, container_path: str) -> str:
        """Convert a container filesystem path to the equivalent Jellyfin path."""
        for prefix, replacement in self._mappings:
            if container_path == prefix or container_path.startswith(prefix + "/"):
                return replacement + container_path[len(prefix):]
        return container_path

    def jellyfin_to_container(self, jellyfin_path: str) -> str:
        """Convert a Jellyfin file path to the equivalent container path."""
        for prefix, replacement in self._reverse:
            if jellyfin_path == prefix or jellyfin_path.startswith(prefix + "/"):
                return replacement + jellyfin_path[len(prefix):]
        return jellyfin_path

    @property
    def has_mappings(self) -> bool:
        return bool(self._mappings)


def build_jellyfin_path_set(
    jellyfin_items: list[dict],
    path_mapper: PathMapper | None = None,
) -> set[str]:
    """Build a set of normalised file paths from Jellyfin library items.

    Each item dict should have a 'paths' key with a list of file paths.
    If a PathMapper is provided, paths are translated to container-space.
    """
    path_set: set[str] = set()
    for item in jellyfin_items:
        for p in item.get("paths", []):
            if path_mapper and path_mapper.has_mappings:
                p = path_mapper.jellyfin_to_container(p)
            # Normalise: no trailing slash, consistent separators
            path_set.add(os.path.normpath(p))
    return path_set


def detect_orphans(
    scan_path: str,
    jellyfin_path_set: set[str],
    extensions: frozenset[str] = DEFAULT_MEDIA_EXTENSIONS,
) -> list[dict]:
    """Walk a directory and find files not present in the Jellyfin path set.

    Returns a list of orphaned file dicts (same format as scan_directory).
    """
    orphans: list[dict] = []
    scan_root = Path(scan_path)

    if not scan_root.is_dir():
        return orphans

    for entry in scan_root.rglob("*"):
        if not entry.is_file():
            continue

        ext = entry.suffix.lstrip(".").lower()
        if ext not in extensions:
            continue

        normalised = os.path.normpath(str(entry))
        if normalised not in jellyfin_path_set:
            try:
                stat = entry.stat()
            except OSError:
                continue
            orphans.append({
                "path": str(entry),
                "filename": entry.name,
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc,
                ).isoformat(),
                "extension": ext,
            })

    return orphans


def detect_ghosts(
    scan_path: str,
    jellyfin_items: list[dict],
    path_mapper: PathMapper | None = None,
) -> list[dict]:
    """Find Jellyfin items whose file paths don't exist on disk.

    These are 'ghost entries' — items in the library pointing to
    missing files.
    """
    ghosts: list[dict] = []

    for item in jellyfin_items:
        for p in item.get("paths", []):
            # Translate Jellyfin path to container path
            container_path = p
            if path_mapper and path_mapper.has_mappings:
                container_path = path_mapper.jellyfin_to_container(p)

            container_path = os.path.normpath(container_path)

            # Only check paths under the scan_path
            if not container_path.startswith(os.path.normpath(scan_path)):
                continue

            if not os.path.exists(container_path):
                ghosts.append({
                    "item_id": item["item_id"],
                    "name": item["name"],
                    "type": item.get("type"),
                    "missing_path": p,
                    "expected_container_path": container_path,
                })

    return ghosts


# -- Title normalisation for Jellyfin matching --

# Patterns stripped during normalisation
_YEAR_PAREN_RE = re.compile(r"\s*\(\d{4}\)\s*")       # (2014), (2018)
_TMDB_TAG_RE = re.compile(r"\s*\{tmdb-\d+\}\s*")       # {tmdb-570701}
_TVDB_TAG_RE = re.compile(r"\s*\{tvdb-\d+\}\s*")       # {tvdb-123456}
_IMDB_TAG_RE = re.compile(r"\s*\{imdb-tt\d+\}\s*")     # {imdb-tt1234567}
_SERIES_SUFFIX_RE = re.compile(
    r":\s*(?:series\s+\d+|specials|\d{4})$", re.IGNORECASE,
)  # : Series 1, : Specials, : 2025


def normalise_title(title: str) -> str:
    """Normalise a title for fuzzy comparison.

    Strips years in parentheses, {tmdb-...}/{tvdb-...} tags, leading
    "The ", apostrophe variants, and collapses whitespace. Returns
    lowercase.
    """
    if not title:
        return ""
    t = title
    t = _YEAR_PAREN_RE.sub("", t)
    t = _TMDB_TAG_RE.sub("", t)
    t = _TVDB_TAG_RE.sub("", t)
    t = _IMDB_TAG_RE.sub("", t)
    # Strip "Series N" / "Specials" suffix (get_iplayer convention)
    t = _SERIES_SUFFIX_RE.sub("", t)
    # Normalise punctuation and whitespace
    t = t.lower().strip()
    t = re.sub(r"^the\s+", "", t)           # Leading "The "
    t = re.sub(r"^bbc\s+", "", t)           # Leading "BBC "
    t = t.replace("\u2019", "'")             # Curly apostrophe → straight
    t = re.sub(r"'s\b", "", t)              # Strip possessive 's (2's → 2)
    t = t.replace("'", "")                   # Remove remaining apostrophes
    t = t.replace(":", "")                   # Remove colons
    t = t.replace("-", " ")                  # Hyphens to spaces
    t = t.replace("?", "")                   # Remove question marks
    t = re.sub(r"\((?!\d{4}\))[^)]*\)", "", t)  # Strip non-year parentheticals
    t = re.sub(r"\s+\d{4}$", "", t)          # Strip trailing bare year (2025)
    t = re.sub(r"\s+", " ", t).strip()       # Collapse whitespace
    return t


class JellyfinNameIndex:
    """In-memory index of Jellyfin items keyed by normalised name.

    Builds once from a batch query, then supports fast lookups for
    classifying ghost history entries as relocated or truly deleted.
    """

    def __init__(self, items: list[dict]):
        # Index by normalised name → list of items
        self._by_name: dict[str, list[dict]] = defaultdict(list)
        for item in items:
            key = normalise_title(item.get("name", ""))
            if key:
                self._by_name[key].append(item)

    def find_by_name(self, name: str) -> list[dict]:
        """Find Jellyfin items matching a normalised name."""
        key = normalise_title(name)
        if not key:
            return []
        return self._by_name.get(key, [])

    def find_match(
        self,
        name: str,
        episode: str | None = None,
        series_num: str | None = None,
        episode_num: str | None = None,
    ) -> dict | None:
        """Find the best Jellyfin match for a get_iplayer ghost entry.

        For series content (episode info present), tries to match on
        series name + episode. For standalone content (movies/specials),
        a name match suffices.

        Returns the matched Jellyfin item dict or None.
        """
        candidates = self.find_by_name(name)
        if not candidates:
            return None

        # For standalone content (no episode info), any match is good
        if not episode and not episode_num:
            return candidates[0]

        # For series content, prefer a Series/show-level match
        # (indicates the show exists in Jellyfin, even if we can't
        # pin down the exact episode)
        series_matches = [c for c in candidates if c.get("type") == "Series"]
        if series_matches:
            return series_matches[0]

        # Also accept Episode-level matches
        episode_matches = [c for c in candidates if c.get("type") == "Episode"]
        if episode_matches:
            return episode_matches[0]

        # Fall back to any match (Movie, MusicVideo, etc.)
        return candidates[0]

    @property
    def item_count(self) -> int:
        return sum(len(v) for v in self._by_name.values())


def classify_ghost(
    ghost: dict,
    jf_index: JellyfinNameIndex,
    path_mapper: PathMapper | None = None,
) -> dict:
    """Classify a single ghost entry as 'relocated' or 'truly_deleted'.

    Enriches the ghost dict with classification and Jellyfin details.
    """
    meta = ghost  # ghost dict already has metadata fields extracted
    name = meta.get("name") or meta.get("title", "")
    episode = meta.get("episode")
    series_num = meta.get("seriesnum")
    episode_num = meta.get("episodenum")

    match = jf_index.find_match(
        name, episode=episode,
        series_num=series_num, episode_num=episode_num,
    )

    if match:
        # Build the current path from the match
        current_path = None
        paths = match.get("paths", [])
        if paths:
            current_path = paths[0]
            if path_mapper and path_mapper.has_mappings:
                current_path = path_mapper.jellyfin_to_container(current_path)

        ghost["classification"] = "relocated"
        ghost["jellyfin_item_id"] = match.get("item_id")
        ghost["jellyfin_name"] = match.get("name")
        ghost["jellyfin_type"] = match.get("type")
        ghost["current_path"] = current_path
    else:
        ghost["classification"] = "truly_deleted"

    return ghost


def group_truly_deleted(entries: list[dict]) -> list[dict]:
    """Group truly_deleted entries by show name for compact output.

    Episodes of the same show are collapsed into a single entry with
    an episode list, preventing hundreds of flat entries.
    """
    groups: dict[str, dict] = {}

    for entry in entries:
        # Group key: the show/programme name (not episode-specific title)
        name = entry.get("name") or entry.get("title", "Unknown")
        key = normalise_title(name)

        if key not in groups:
            groups[key] = {
                "name": name,
                "channel": entry.get("channel"),
                "categories": entry.get("categories"),
                "type": entry.get("type"),
                "episode_count": 0,
                "episodes": [],
                "bbc_urls": [],
                "pids": [],
            }

        group = groups[key]
        group["episode_count"] += 1

        episode = entry.get("episode")
        if episode and episode not in group["episodes"]:
            group["episodes"].append(episode)

        bbc_url = entry.get("bbc_url")
        if bbc_url:
            group["bbc_urls"].append(bbc_url)

        pid = entry.get("pid")
        if pid:
            group["pids"].append(pid)

        # Keep channel/categories from first entry that has them
        if not group["channel"] and entry.get("channel"):
            group["channel"] = entry["channel"]
        if not group["categories"] and entry.get("categories"):
            group["categories"] = entry["categories"]

    # Sort by episode count descending (biggest groups first)
    result = sorted(groups.values(), key=lambda g: g["episode_count"], reverse=True)

    # Clean up: remove empty fields
    for group in result:
        if not group["episodes"]:
            del group["episodes"]
        if not group["bbc_urls"]:
            del group["bbc_urls"]
        if not group["pids"]:
            del group["pids"]
        if not group["channel"]:
            del group["channel"]
        if not group["categories"]:
            del group["categories"]

    return result
