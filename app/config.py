"""Configuration and service auto-discovery from environment variables."""

import os
from dataclasses import dataclass, field


@dataclass
class ServiceConfig:
    """Config for a single *arr-style service."""
    name: str
    url: str
    api_key: str


@dataclass
class CredentialConfig:
    """Config for services using username/password."""
    name: str
    url: str
    username: str
    password: str


@dataclass
class Config:
    """MediaStack configuration — auto-discovered from environment."""

    # Database
    db_url: str = ""

    # Polling intervals (seconds)
    poll_interval: int = 300       # 5 min for events
    storage_interval: int = 3600   # 1 hr for storage
    library_interval: int = 21600  # 6 hrs for library snapshots

    # Discovered services
    arr_services: dict[str, ServiceConfig] = field(default_factory=dict)
    credential_services: dict[str, CredentialConfig] = field(default_factory=dict)

    # Prowlarr Newznab category mapping — overridable via PROWLARR_CATEGORIES_<TYPE>
    prowlarr_categories: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()

        # Database URL
        cfg.db_url = os.environ.get(
            "MEDIASTACK_DB_URL",
            f"postgresql://{os.environ.get('DB_MEDIASTACK_USER', 'mediastack')}"
            f":{os.environ.get('DB_MEDIASTACK_PASS', '')}"
            f"@{os.environ.get('POSTGRES_HOST', 'postgres')}"
            f":{os.environ.get('POSTGRES_PORT', '5432')}"
            f"/{os.environ.get('DB_MEDIASTACK_NAME', 'mediastack')}",
        )

        # Polling intervals
        cfg.poll_interval = int(os.environ.get("MEDIASTACK_POLL_INTERVAL", "300"))
        cfg.storage_interval = int(os.environ.get("MEDIASTACK_STORAGE_INTERVAL", "3600"))
        cfg.library_interval = int(os.environ.get("MEDIASTACK_LIBRARY_INTERVAL", "21600"))

        # Auto-discover *arr services (URL + API_KEY pairs)
        # URL vars are set in docker-compose env; API keys come from .docker.env
        arr_pairs = [
            ("sonarr", "SONARR_URL", "SONARR_API_KEY"),
            ("radarr", "RADARR_URL", "RADARR_API_KEY"),
            ("lidarr", "LIDARR_URL", "LIDARR_API_KEY"),
            ("prowlarr", "PROWLARR_URL", "PROWLARR_API_KEY"),
            ("bazarr", "BAZARR_URL", "BAZARR_API_KEY"),
            ("seerr", "SEERR_URL", "SEERR_API_KEY"),
            ("jellyfin", "JELLYFIN_URL", "JELLYFIN_API_KEY"),
            ("audiobookshelf", "AUDIOBOOKSHELF_URL", "AUDIOBOOKSHELF_API_KEY"),
        ]
        for name, url_var, key_var in arr_pairs:
            url = os.environ.get(url_var)
            key = os.environ.get(key_var)
            if url and key:
                cfg.arr_services[name] = ServiceConfig(name=name, url=url.rstrip("/"), api_key=key)

        # Prowlarr Newznab category defaults; override with PROWLARR_CATEGORIES_<TYPE>
        default_prowlarr_cats = {
            "movie": [2000],
            "tv": [5000],
            "music": [3000],
            "book": [7000, 8000],
        }
        for media_type, default in default_prowlarr_cats.items():
            raw = os.environ.get(f"PROWLARR_CATEGORIES_{media_type.upper()}")
            if raw:
                try:
                    cfg.prowlarr_categories[media_type] = [
                        int(x.strip()) for x in raw.split(",") if x.strip()
                    ]
                except ValueError:
                    cfg.prowlarr_categories[media_type] = default
            else:
                cfg.prowlarr_categories[media_type] = default

        # Credential-based services
        sab_url = os.environ.get("SABNZBD_URL")
        sab_key = os.environ.get("SABNZBD_API_KEY")
        if sab_url and sab_key:
            cfg.credential_services["sabnzbd"] = CredentialConfig(
                name="sabnzbd", url=sab_url.rstrip("/"), username="", password=sab_key,
            )

        # qBittorrent — NASgnolia uses QBITTORRENT_USERNAME / QBITTORRENT_PASSWORD
        qbt_url = os.environ.get("QBITTORRENT_URL")
        qbt_user = os.environ.get("QBITTORRENT_USERNAME") or os.environ.get("QBITTORRENT_USER")
        qbt_pass = os.environ.get("QBITTORRENT_PASSWORD") or os.environ.get("QBITTORRENT_PASS")
        if qbt_url and qbt_user and qbt_pass:
            cfg.credential_services["qbittorrent"] = CredentialConfig(
                name="qbittorrent", url=qbt_url.rstrip("/"), username=qbt_user, password=qbt_pass,
            )

        # Dispatcharr — JWT auth (username/password)
        disp_url = os.environ.get("DISPATCHARR_URL")
        disp_user = os.environ.get("DISPATCHARR_USERNAME")
        disp_pass = os.environ.get("DISPATCHARR_PASSWORD")
        if disp_url and disp_user and disp_pass:
            cfg.credential_services["dispatcharr"] = CredentialConfig(
                name="dispatcharr", url=disp_url.rstrip("/"), username=disp_user, password=disp_pass,
            )

        # Boxarr — no auth, URL only
        boxarr_url = os.environ.get("BOXARR_URL")
        if boxarr_url:
            cfg.credential_services["boxarr"] = CredentialConfig(
                name="boxarr", url=boxarr_url.rstrip("/"), username="", password="",
            )

        # Suggestarr — JWT auth (username/password)
        sug_url = os.environ.get("SUGGESTARR_URL")
        sug_user = os.environ.get("SUGGESTARR_USERNAME")
        sug_pass = os.environ.get("SUGGESTARR_PASSWORD")
        if sug_url and sug_user and sug_pass:
            cfg.credential_services["suggestarr"] = CredentialConfig(
                name="suggestarr", url=sug_url.rstrip("/"), username=sug_user, password=sug_pass,
            )

        return cfg

    def describe(self) -> str:
        """Human-readable summary of discovered services."""
        active = list(self.arr_services.keys()) + list(self.credential_services.keys())
        if not active:
            return "No services configured."
        return f"Active services: {', '.join(sorted(active))}"
