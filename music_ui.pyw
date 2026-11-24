#!/usr/bin/env python3
"""Local desktop UI for the music downloader.

This wraps the existing search/download approach in a Tkinter app. Use it only
for music you are authorized to download.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    CRYPTO_IMPORT_ERROR = None
except Exception as exc:
    AES = None
    pad = None
    CRYPTO_IMPORT_ERROR = exc


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SAVE_DIR = SCRIPT_DIR / "downloads"
NE_COOKIE_FILE = SCRIPT_DIR / "netease_cookie.txt"
QQ_COOKIE_FILE = SCRIPT_DIR / "qq_cookie.txt"
CHUNK_SIZE = 64 * 1024

FFMPEG_CANDIDATES = [
    os.environ.get("FFMPEG"),
    shutil.which("ffmpeg"),
]

NE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
    "Referer": "https://music.163.com/",
    "Content-Type": "application/x-www-form-urlencoded",
}
NE_SEARCH_API = "https://music.163.com/api/search/get"
NE_PLAYER_API = "https://music.163.com/weapi/song/enhance/player/url"
NE_PLAYLIST_API = "https://music.163.com/weapi/v3/playlist/detail"
NE_PUBLIC_PLAYLIST_APIS = [
    "https://music.163.com/api/playlist/detail",
    "https://music.163.com/api/v6/playlist/detail",
]
NE_WEAPI_KEY = b"0CoJUm6Qyw8W8jud"
NE_WEAPI_IV = b"0102030405060708"
NE_WEAPI_MOD = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7"
    "b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280"
    "104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932"
    "575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935"
    "b3ece0462db0a22b8e7"
)

QQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
    "Referer": "https://y.qq.com/",
    "Origin": "https://y.qq.com",
}
QQ_API = "https://u.y.qq.com/cgi-bin/musicu.fcg"
QQ_LEGACY_PLAYLIST_API = "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
QQ_QUALITY = [
    ("O600", "ogg", "HQ OGG"),
    ("O400", "ogg", "OGG"),
    ("M800", "mp3", "320k MP3"),
    ("M500", "mp3", "128k MP3"),
]


LogFn = Callable[[str, str], None]
ProgressFn = Callable[[float, str], None]


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip().strip(".")
    return cleaned or "unknown"


def song_stem(title: str, artist: str) -> str:
    return safe_filename(f"{title.strip()}-{artist.strip()}".strip("-"))


def split_queries(text: str) -> list[str]:
    parts = re.split(r"[\n\r|;；、]+", text)
    return [part.strip() for part in parts if part.strip()]


def url_candidates(url: str) -> list[str]:
    parsed = urlparse(url.strip())
    values = [
        url.strip(),
        unquote(url.strip()),
        parsed.path,
        parsed.query,
        parsed.fragment,
        unquote(parsed.fragment),
    ]
    if parsed.fragment:
        fragment = parsed.fragment
        if "?" in fragment:
            values.append(fragment.split("?", 1)[1])
    return [value for value in values if value]


def query_value_from_candidates(url: str, keys: tuple[str, ...]) -> str | None:
    for candidate in url_candidates(url):
        query = candidate
        if "?" in query:
            query = query.split("?", 1)[1]
        params = parse_qs(query)
        for key in keys:
            if params.get(key):
                return params[key][0]
    for candidate in url_candidates(url):
        pattern = r"(?:^|[?&#])(?:{})=([A-Za-z0-9_-]+)".format("|".join(keys))
        match = re.search(pattern, candidate)
        if match:
            return match.group(1)
    return None


def resolve_share_url(url: str) -> str | None:
    try:
        response = requests.get(url, headers=QQ_HEADERS, allow_redirects=True, timeout=10)
        response.raise_for_status()
        if response.url and response.url != url:
            return response.url
        for pattern in (
            r"https?://[^\"'<> ]*(?:playlist|taoge|songlist)[^\"'<> ]+",
            r"(?:id|disstid|tid|dirid)=([A-Za-z0-9_-]+)",
        ):
            match = re.search(pattern, response.text)
            if match:
                return match.group(0)
    except Exception:
        return None
    return None


def load_cookie_file(path: Path, required: list[str]) -> dict[str, str] | None:
    if not path.exists():
        return None
    cookies: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cookies[key.strip()] = value.strip()
    if any(not cookies.get(key) for key in required):
        return None
    return cookies


def load_netease_cookies() -> dict[str, str] | None:
    return load_cookie_file(NE_COOKIE_FILE, ["MUSIC_U", "__csrf"])


def load_qq_cookies() -> dict[str, str] | None:
    return load_cookie_file(QQ_COOKIE_FILE, ["uin", "qm_keyst", "qqmusic_key"])


def ensure_cookie_templates() -> None:
    if not NE_COOKIE_FILE.exists():
        NE_COOKIE_FILE.write_text("MUSIC_U=\n__csrf=\n", encoding="utf-8")
    if not QQ_COOKIE_FILE.exists():
        QQ_COOKIE_FILE.write_text(
            "uin=\nqm_keyst=\nqqmusic_key=\neuin=\ntmeLoginType=2\nlogin_type=1\n",
            encoding="utf-8",
        )


def find_existing_song(save_dir: Path, title: str, artist: str) -> Path | None:
    stem = song_stem(title, artist).casefold()
    for path in save_dir.iterdir() if save_dir.exists() else []:
        if path.is_file() and path.stem.casefold() == stem:
            return path
    return None


def extract_ext(url: str, fallback: str = "mp3") -> str:
    clean = url.split("?", 1)[0]
    ext = clean.rsplit(".", 1)[-1].lower() if "." in clean else fallback
    return ext if re.fullmatch(r"[a-z0-9]{2,5}", ext) else fallback


def ffmpeg_path() -> str | None:
    for candidate in FFMPEG_CANDIDATES:
        if candidate and Path(candidate).exists():
            return str(candidate)
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        winget_packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        for candidate in winget_packages.glob("Gyan.FFmpeg_*/ffmpeg-*/bin/ffmpeg.exe"):
            if candidate.is_file():
                return str(candidate)
    return None


def hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    kwargs = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    kwargs["startupinfo"] = startupinfo
    return kwargs


def convert_to_mp3(path: Path) -> Path:
    if path.suffix.lower() == ".mp3":
        return path
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        return path
    target = path.with_suffix(".mp3")
    try:
        subprocess.run(
            [ffmpeg, "-i", str(path), "-q:a", "0", str(target), "-y", "-loglevel", "quiet"],
            check=True,
            **hidden_subprocess_kwargs(),
        )
        if target.exists() and target.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return target
    except Exception:
        return path
    return path


def wait_if_paused(pause_event: threading.Event, stop_event: threading.Event) -> bool:
    while pause_event.is_set() and not stop_event.is_set():
        time.sleep(0.2)
    return stop_event.is_set()


def download_stream(
    url: str,
    filepath: Path,
    headers: dict[str, str],
    cookies: dict[str, str] | None,
    progress_cb: ProgressFn,
    pause_event: threading.Event,
    stop_event: threading.Event,
) -> tuple[Path | None, str | None]:
    tmp_path = filepath.with_suffix(filepath.suffix + ".part")
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with requests.get(url, headers=headers, cookies=cookies, stream=True, timeout=60) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0) or 0)
            done = 0
            with tmp_path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if wait_if_paused(pause_event, stop_event):
                        tmp_path.unlink(missing_ok=True)
                        return None, "已终止"
                    if not chunk:
                        continue
                    file.write(chunk)
                    done += len(chunk)
                    if total:
                        progress_cb(done / total * 100, f"{done // 1024}KB / {total // 1024}KB")
        tmp_path.replace(filepath)
        progress_cb(100, "完成")
        return filepath, None
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        return None, str(exc)


def ne_aes(data: str, key: bytes, iv: bytes) -> str:
    if AES is None or pad is None:
        raise RuntimeError(f"缺少 pycryptodome 依赖：{CRYPTO_IMPORT_ERROR}")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(pad(data.encode(), AES.block_size))).decode()


def ne_rsa(text: str) -> str:
    n = int(NE_WEAPI_MOD, 16)
    value = int(text[::-1].encode().hex(), 16)
    return format(pow(value, 0x10001, n), "0256x")


def ne_encrypt(params: dict) -> dict[str, str]:
    secret = secrets.token_hex(8).encode()
    first = ne_aes(json.dumps(params), NE_WEAPI_KEY, NE_WEAPI_IV)
    second = ne_aes(first, secret, NE_WEAPI_IV)
    return {"params": second, "encSecKey": ne_rsa(secret.decode())}


def netease_search(keyword: str, limit: int = 5) -> list[dict]:
    cookies = load_netease_cookies()
    if not cookies:
        return []
    response = requests.post(
        NE_SEARCH_API,
        data={"s": keyword, "type": 1, "offset": 0, "total": "true", "limit": limit},
        headers=NE_HEADERS,
        cookies=cookies,
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("result", {}).get("songs", [])


def parse_netease_playlist_id(url: str) -> str | None:
    value = query_value_from_candidates(url, ("id",))
    if value and value.isdigit():
        return value
    for candidate in url_candidates(url):
        match = re.search(r"(?:playlist|id)[=/](\d+)", candidate)
        if match:
            return match.group(1)
    return None


def normalize_netease_tracks(tracks: list[dict]) -> list[dict]:
    return [
        {
            "id": track.get("id"),
            "name": track.get("name", ""),
            "artists": track.get("ar") or track.get("artists") or [],
            "album": track.get("al") or track.get("album") or {},
        }
        for track in tracks
        if track.get("id")
    ]


def netease_public_playlist(playlist_id: str, cookies: dict[str, str] | None) -> list[dict]:
    for api in NE_PUBLIC_PLAYLIST_APIS:
        try:
            response = requests.get(
                api,
                params={"id": playlist_id, "n": 1000},
                headers=NE_HEADERS,
                cookies=cookies,
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            tracks = (
                data.get("result", {}).get("tracks")
                or data.get("playlist", {}).get("tracks")
                or []
            )
            if tracks:
                return normalize_netease_tracks(tracks)
        except Exception:
            continue
    return []


def netease_playlist(url: str) -> list[dict]:
    cookies = load_netease_cookies()
    playlist_id = parse_netease_playlist_id(url)
    if not playlist_id:
        return []

    public_tracks = netease_public_playlist(playlist_id, cookies)
    if public_tracks:
        return public_tracks

    if not cookies:
        return []
    if AES is None or pad is None:
        return []
    csrf = cookies.get("__csrf", "")
    payload = ne_encrypt({"id": playlist_id, "n": 1000, "csrf_token": csrf})
    response = requests.post(
        f"{NE_PLAYLIST_API}?csrf_token={csrf}",
        data=payload,
        headers=NE_HEADERS,
        cookies=cookies,
        timeout=20,
    )
    response.raise_for_status()
    tracks = response.json().get("playlist", {}).get("tracks", [])
    return normalize_netease_tracks(tracks)


def netease_artist(song: dict) -> str:
    return " & ".join(artist.get("name", "") for artist in song.get("artists", []))


def netease_download_song(
    song: dict,
    save_dir: Path,
    progress_cb: ProgressFn,
    pause_event: threading.Event,
    stop_event: threading.Event,
) -> tuple[Path | None, str | None]:
    cookies = load_netease_cookies()
    if not cookies:
        return None, "网易云 Cookie 未配置"
    if AES is None or pad is None:
        return None, "NetEase requires pycryptodome: pip install pycryptodome"
    title = song.get("name", "未知")
    artist = netease_artist(song)
    existing = find_existing_song(save_dir, title, artist)
    if existing:
        return existing, "SKIPPED"

    csrf = cookies.get("__csrf", "")
    for bitrate in (999000, 320000, 128000):
        if stop_event.is_set():
            return None, "已终止"
        try:
            payload = ne_encrypt({"ids": [song["id"]], "br": bitrate, "csrf_token": csrf})
            response = requests.post(
                f"{NE_PLAYER_API}?csrf_token={csrf}",
                data=payload,
                headers=NE_HEADERS,
                cookies=cookies,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json().get("data", [{}])
            audio_url = data[0].get("url") if data else None
            if not audio_url:
                continue
            ext = extract_ext(audio_url)
            filepath = save_dir / f"{song_stem(title, artist)}.{ext}"
            path, err = download_stream(
                audio_url,
                filepath,
                {**NE_HEADERS, "Origin": "https://music.163.com"},
                cookies,
                progress_cb,
                pause_event,
                stop_event,
            )
            if path:
                return convert_to_mp3(path), None
            if err == "已终止":
                return None, err
        except Exception:
            continue
    return None, "网易云无法获取播放地址"


def qq_search(keyword: str, limit: int = 5) -> list[dict]:
    cookies = load_qq_cookies()
    if not cookies:
        return []
    uin = cookies.get("uin", "0")
    payload = {
        "comm": {
            "cv": 4747474,
            "ct": 24,
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "platform": "yqq.json",
            "needNewCode": 1,
            "uin": uin,
        },
        "req_1": {
            "method": "DoSearchForQQMusicDesktop",
            "module": "music.search.SearchCgiService",
            "param": {"num_per_page": limit, "page_num": 1, "query": keyword, "search_type": 0},
        },
    }
    response = requests.post(QQ_API, json=payload, headers=QQ_HEADERS, cookies=cookies, timeout=15)
    response.raise_for_status()
    return (
        response.json()
        .get("req_1", {})
        .get("data", {})
        .get("body", {})
        .get("song", {})
        .get("list", [])
    )


def parse_qq_playlist_id(url: str) -> str | None:
    value = query_value_from_candidates(url, ("id", "disstid", "tid", "dirid"))
    if value:
        return value
    for candidate in url_candidates(url):
        for pattern in (
            r"(?:playlist|songlist|taoge)/(?:details/)?([A-Za-z0-9_-]+)",
            r"taoge\.html.*?(?:id|disstid)=([A-Za-z0-9_-]+)",
            r"/playlist/([A-Za-z0-9_-]+)",
        ):
            match = re.search(pattern, candidate)
            if match:
                return match.group(1)
    resolved = resolve_share_url(url)
    if resolved and resolved != url:
        return parse_qq_playlist_id(resolved)
    return None


def normalize_qq_song(song: dict) -> dict | None:
    mid = (
        song.get("songmid")
        or song.get("mid")
        or song.get("song", {}).get("mid")
        or song.get("song", {}).get("songmid")
    )
    name = (
        song.get("songname")
        or song.get("name")
        or song.get("title")
        or song.get("song", {}).get("name")
        or song.get("song", {}).get("title")
        or ""
    )
    singers = song.get("singer") or song.get("singers") or song.get("song", {}).get("singer") or []
    normalized_singers = []
    for singer in singers:
        if isinstance(singer, dict):
            normalized_singers.append({"name": singer.get("name", "")})
        elif isinstance(singer, str):
            normalized_singers.append({"name": singer})
    if not mid:
        return None
    return {
        "mid": mid,
        "name": name,
        "singer": normalized_singers,
        "album": {"name": song.get("albumname") or song.get("album", {}).get("name", "")},
    }


def qq_songlist_from_payload(payload: dict) -> list[dict]:
    data = payload.get("req_1", {}).get("data", {})
    candidates = [
        data.get("songlist"),
        data.get("cdlist", [{}])[0].get("songlist") if data.get("cdlist") else None,
        data.get("dirinfo", {}).get("songlist"),
        data.get("list"),
    ]
    songs = next((candidate for candidate in candidates if candidate), [])
    normalized = []
    for song in songs:
        item = normalize_qq_song(song)
        if item:
            normalized.append(item)
    return normalized


def qq_legacy_playlist(playlist_id: str, cookies: dict[str, str]) -> list[dict]:
    headers = {
        **QQ_HEADERS,
        "Referer": "https://y.qq.com/n/ryqq/playlist/" + playlist_id,
        "Origin": "https://y.qq.com",
    }
    params = {
        "type": 1,
        "json": 1,
        "utf8": 1,
        "onlysong": 0,
        "disstid": playlist_id,
        "format": "json",
        "g_tk": 5381,
        "loginUin": cookies.get("uin", "0"),
        "hostUin": 0,
        "inCharset": "utf8",
        "outCharset": "utf-8",
        "notice": 0,
        "platform": "yqq",
        "needNewCode": 0,
    }
    response = requests.get(
        QQ_LEGACY_PLAYLIST_API,
        params=params,
        headers=headers,
        cookies=cookies,
        timeout=20,
    )
    response.raise_for_status()
    text = response.text.strip()
    if text.startswith("playlistinfoCallback(") and text.endswith(")"):
        text = text[len("playlistinfoCallback(") : -1]
    payload = json.loads(text)
    songs = payload.get("cdlist", [{}])[0].get("songlist", [])
    normalized = []
    for song in songs:
        item = normalize_qq_song(song)
        if item:
            normalized.append(item)
    return normalized


def qq_playlist(url: str) -> list[dict]:
    cookies = load_qq_cookies()
    playlist_id = parse_qq_playlist_id(url)
    if not cookies or not playlist_id:
        return []
    uin = cookies.get("uin", "0")
    payload = {
        "comm": {
            "cv": 4747474,
            "ct": 24,
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "platform": "yqq.json",
            "needNewCode": 1,
            "uin": uin,
        },
        "req_1": {
            "module": "music.srfDissInfo.aiDissInfo",
            "method": "uniform_get_Dissinfo",
            "param": {
                "disstid": playlist_id,
                "enc_host_uin": "",
                "tag": 1,
                "userinfo": 1,
                "orderlist": 1,
            },
        },
    }
    response = requests.post(QQ_API, json=payload, headers=QQ_HEADERS, cookies=cookies, timeout=20)
    response.raise_for_status()
    songs = qq_songlist_from_payload(response.json())
    if songs:
        return songs
    return qq_legacy_playlist(playlist_id, cookies)


def qq_artist(song: dict) -> str:
    return " & ".join(item.get("name", "") for item in song.get("singer", []))


def qq_download_song(
    song: dict,
    save_dir: Path,
    progress_cb: ProgressFn,
    pause_event: threading.Event,
    stop_event: threading.Event,
) -> tuple[Path | None, str | None]:
    cookies = load_qq_cookies()
    if not cookies:
        return None, "QQ 音乐 Cookie 未配置"
    title = song.get("name", "未知")
    artist = qq_artist(song)
    existing = find_existing_song(save_dir, title, artist)
    if existing:
        return existing, "SKIPPED"

    mid = song.get("mid", "")
    uin = cookies.get("uin", "0")
    guid = str(secrets.randbelow(9_000_000_000) + 1_000_000_000)
    for prefix, ext, _label in QQ_QUALITY:
        if stop_event.is_set():
            return None, "已终止"
        filename = f"{prefix}{mid}.{ext}"
        payload = {
            "comm": {
                "cv": 4747474,
                "ct": 24,
                "format": "json",
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "platform": "yqq.json",
                "needNewCode": 1,
                "uin": uin,
            },
            "req_1": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "guid": guid,
                    "songmid": [mid],
                    "songtype": [0],
                    "uin": uin,
                    "loginflag": 1,
                    "platform": "20",
                    "filename": [filename],
                },
            },
        }
        try:
            response = requests.post(QQ_API, json=payload, headers=QQ_HEADERS, cookies=cookies, timeout=15)
            response.raise_for_status()
            info = response.json().get("req_1", {}).get("data", {})
            purl = (info.get("midurlinfo") or [{}])[0].get("purl", "")
            if not purl:
                continue
            sips = [sip for sip in (info.get("sip") or []) if sip] or ["http://dl.stream.qqmusic.qq.com/"]
            for sip in sips:
                filepath = save_dir / f"{song_stem(title, artist)}.{ext}"
                path, err = download_stream(
                    sip + purl,
                    filepath,
                    QQ_HEADERS,
                    cookies,
                    progress_cb,
                    pause_event,
                    stop_event,
                )
                if path:
                    return convert_to_mp3(path), None
                if err == "已终止":
                    return None, err
        except Exception:
            continue
    return None, "QQ 音乐无法获取播放地址"



def _yt_keyword(keyword: str) -> str:
    """Normalize keyword for YouTube search."""
    kw = re.sub(r'[·•|/]', ' ', keyword).strip()
    kw = re.sub(r'\s+', ' ', kw)
    return kw


def _yt_best_url(keyword: str, stop_event: threading.Event) -> str | None:
    """Search YouTube for top 3 results, return URL of highest-view non-long video."""
    if not shutil.which("yt-dlp"):
        return None
    cookie_file = SCRIPT_DIR / "youtube_cookies.txt"
    cmd = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "--dump-json",
        "--no-playlist",
        "--flat-playlist",
        f"ytsearch3:{_yt_keyword(keyword)}",
    ]
    if cookie_file.exists():
        cmd[1:1] = ["--cookies", str(cookie_file)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=20,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        candidates = []
        for line in result.stdout.strip().splitlines():
            if stop_event.is_set():
                return None
            try:
                info = json.loads(line)
                duration   = info.get("duration") or 0
                view_count = info.get("view_count") or 0
                url        = info.get("url") or info.get("webpage_url") or info.get("id")
                if url and duration <= 600:   # skip videos longer than 10 min
                    candidates.append((view_count, url))
            except Exception:
                continue

        if not candidates:
            return None
        # Return URL with highest view count
        candidates.sort(key=lambda x: x[0], reverse=True)
        url = candidates[0][1]
        # If only an ID was returned, build full URL
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"
        return url
    except Exception:
        return None


def ytdlp_download(
    keyword: str,
    save_dir: Path,
    progress_cb: ProgressFn,
    pause_event: threading.Event,
    stop_event: threading.Event,
) -> tuple[Path | None, str | None]:
    if not shutil.which("yt-dlp"):
        return None, "yt-dlp 未安装"
    save_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: find best URL from top 3 results
    best_url = _yt_best_url(keyword, stop_event)
    if stop_event.is_set():
        return None, "已终止"
    if not best_url:
        # fallback to direct search if metadata fetch failed
        best_url = f"ytsearch1:{_yt_keyword(keyword)}"

    output_tmpl = str(save_dir / "%(title)s.%(ext)s")
    command = [
        "yt-dlp",
        "--remote-components", "ejs:github",
        "-x",
        "--audio-format", "mp3",
        "-o", output_tmpl,
        "--no-playlist",
        "--progress",
        best_url,
    ]
    ffmpeg = ffmpeg_path()
    if ffmpeg:
        command[4:4] = ["--ffmpeg-location", str(Path(ffmpeg).parent)]
    # Add cookies if available
    cookie_file = SCRIPT_DIR / "youtube_cookies.txt"
    if cookie_file.exists():
        command[1:1] = ["--cookies", str(cookie_file)]
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        assert process.stdout is not None
        output = ""
        for line in process.stdout:
            output += line
            if wait_if_paused(pause_event, stop_event):
                process.terminate()
                return None, "已终止"
            match = re.search(r"\[download\]\s+([\d.]+)%", line)
            if match:
                progress_cb(float(match.group(1)), "yt-dlp")
        process.wait()
        if process.returncode != 0:
            if "sign in" in output.lower() or "bot" in output.lower():
                return None, "Sign in to confirm you're not a bot"
            return None, "yt-dlp 下载失败"
        mp3s = sorted(save_dir.glob("*.mp3"), key=lambda path: path.stat().st_mtime, reverse=True)
        return (mp3s[0], None) if mp3s else (None, "未找到输出文件")
    except Exception as exc:
        return None, str(exc)


def choose_best_netease(keyword: str) -> dict | None:
    songs = netease_search(keyword, 5)
    return songs[0] if songs else None


def choose_best_qq(keyword: str) -> dict | None:
    songs = qq_search(keyword, 5)
    return songs[0] if songs else None


class MusicDownloaderApp:
    BG      = "#F5F6FA"
    SURFACE = "#FFFFFF"
    BORDER  = "#E2E4ED"
    ACCENT  = "#4B6EF5"
    ACCENT2 = "#EBF0FF"
    TEXT    = "#1A1A2E"
    MUTED   = "#9294A0"
    SUCCESS = "#2ECC71"
    DANGER  = "#E74C3C"
    WARN    = "#F39C12"
    PANEL_W = 300

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Music Downloader")
        self.root.geometry("720x620")
        self.root.minsize(640, 560)
        self.root.configure(bg=self.BG)

        self.queue: queue.Queue = queue.Queue()
        self.stop_event  = threading.Event()
        self.pause_event = threading.Event()
        self.worker = None
        self.running = False
        self._panel_open = False
        self._pending_save_dir: Path | None     = None  # tracks loaded, waiting for user
        self._track_list:   list[dict]          = []
        self._song_vars:    list[tk.BooleanVar] = []
        self._song_rows:    list[tk.Frame]      = []
        self._last_clicked: int                 = -1
        self._fail_count:   int                 = 0

        self.mode_var     = tk.StringVar(value="search")
        self.save_dir_var = tk.StringVar(value=str(DEFAULT_SAVE_DIR))
        self.current_var  = tk.StringVar(value="")
        self.result_var   = tk.StringVar(value="")
        self.progress_var = tk.StringVar(value="")

        self._build_ui()
        self._poll_queue()
        ensure_cookie_templates()
        self.root.after(200, self._check_cookies_on_start)

    def _check_cookies_on_start(self):
        msgs = []
        if not load_netease_cookies():
            msgs.append("NetEase cookie missing/expired")
        if not load_qq_cookies():
            msgs.append("QQ Music cookie missing/expired")
        cookie_file = SCRIPT_DIR / "youtube_cookies.txt"
        if not cookie_file.exists():
            msgs.append("YouTube cookie missing — add youtube_cookies.txt")
        elif cookie_file.stat().st_size == 0:
            msgs.append("YouTube cookie file is empty — replace youtube_cookies.txt")
        elif "Netscape" not in cookie_file.read_text(encoding="utf-8", errors="ignore")[:50]:
            msgs.append("YouTube cookie file is invalid — replace youtube_cookies.txt")
        if msgs:
                    self.result_var.set("  ⚠  " + "  |  ".join(msgs))
                    self._result_label.config(fg=self.WARN)

    def _build_ui(self):
        C   = self
        PAD = dict(padx=24)
        # ensure outer layout exists
        if not hasattr(self, '_main_pane'):
            self._outer = tk.Frame(self.root, bg=C.BG)
            self._outer.pack(fill="both", expand=True)
            self._main_pane = tk.Frame(self._outer, bg=C.BG)
            self._main_pane.pack(side="left", fill="both", expand=True)
            self._panel = tk.Frame(self._outer, bg=C.SURFACE,
                                   highlightthickness=1,
                                   highlightbackground=C.BORDER,
                                   width=0)
            self._panel.pack(side="right", fill="y")
            self._panel.pack_propagate(False)
            self._build_panel()

        # progress bar style
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Blue.Horizontal.TProgressbar",
                        troughcolor=C.BORDER, background=C.ACCENT,
                        bordercolor=C.BORDER, lightcolor=C.ACCENT,
                        darkcolor=C.ACCENT, thickness=6)

        # ── header ──
        root = self._main_pane
        hdr = tk.Frame(root, bg=C.BG)
        hdr.pack(fill="x", pady=(22, 0), **PAD)
        tk.Label(hdr, text="🎵  Music Downloader",
                 bg=C.BG, fg=C.TEXT,
                 font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(hdr, text="NetEase · QQ Music · YouTube  —  falls back automatically",
                 bg=C.BG, fg=C.MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(3, 0))

        # divider
        tk.Frame(root, bg=C.BORDER, height=1).pack(fill="x", pady=(18, 0), **PAD)

        # ── mode tabs ──
        tab_row = tk.Frame(root, bg=C.BG)
        tab_row.pack(fill="x", pady=(16, 0), **PAD)
        self._tab_btns = {}
        for label, val in [("🔍  Search", "search"), ("📋  Playlist", "playlist")]:
            b = tk.Button(tab_row, text=label,
                          font=("Microsoft YaHei UI", 10, "bold"),
                          relief="flat", padx=16, pady=6, cursor="hand2",
                          command=lambda v=val: self._set_mode(v))
            b.pack(side="left", padx=(0, 6))
            self._tab_btns[val] = b

        # ── input card ──
        card = tk.Frame(root, bg=C.SURFACE,
                        highlightthickness=1, highlightbackground=C.BORDER)
        card.pack(fill="x", pady=(12, 0), **PAD)

        # hint area - light gray background
        HINT_BG = "#F5F5F7"
        hint_area = tk.Frame(card, bg=HINT_BG)
        hint_area.pack(fill="x")

        self._hint = tk.Label(hint_area, bg=HINT_BG, fg=C.MUTED,
                              font=("Microsoft YaHei UI", 10),
                              text="Enter a song name or song + artist. Use  |  or new lines for multiple songs.")
        self._hint.pack(anchor="w", padx=12, pady=(8, 0))

        ex1 = tk.Label(hint_area, bg=HINT_BG, fg="#BBBBBB",
                       font=("Microsoft YaHei UI", 9),
                       text="  e.g.  Shape of You · Ed Sheeran")
        ex1.pack(anchor="w", padx=12)
        ex2 = tk.Label(hint_area, bg=HINT_BG, fg="#BBBBBB",
                       font=("Microsoft YaHei UI", 9),
                       text="        稻香 · 周杰伦  |  Blinding Lights · The Weeknd")
        ex2.pack(anchor="w", padx=12, pady=(0, 8))

        # thin divider between hint and input
        tk.Frame(card, bg=C.BORDER, height=1).pack(fill="x")

        # input area - white background with scrollbar
        input_frame = tk.Frame(card, bg=C.SURFACE)
        input_frame.pack(fill="x")
        self.input_text = tk.Text(input_frame, height=4, wrap="word",
                                   bg=C.SURFACE, fg=C.TEXT,
                                   insertbackground=C.TEXT,
                                   relief="flat", bd=0,
                                   font=("Microsoft YaHei UI", 10),
                                   padx=12, pady=8)
        _sb = tk.Scrollbar(input_frame, orient="vertical",
                           command=self.input_text.yview,
                           width=12, relief="flat", bd=0,
                           bg=C.BORDER, troughcolor=C.SURFACE,
                           activebackground=C.MUTED)
        self.input_text.configure(yscrollcommand=_sb.set)
        _sb.pack(side="right", fill="y", pady=4)
        self.input_text.pack(side="left", fill="x", expand=True)
        self.input_text.bind("<Control-Return>", lambda e: self._start())

        self._set_mode("search")

        # ── save path ──
        path_row = tk.Frame(root, bg=C.BG)
        path_row.pack(fill="x", pady=(12, 0), padx=24)
        path_row.columnconfigure(1, weight=1)
        tk.Label(path_row, text="Save to", bg=C.BG, fg=C.MUTED,
                 font=("Microsoft YaHei UI", 9, "bold"),
                 width=7, anchor="w").grid(row=0, column=0, sticky="w")
        tk.Entry(path_row, textvariable=self.save_dir_var,
                 bg=C.SURFACE, fg=C.TEXT, insertbackground=C.TEXT,
                 relief="solid", bd=1, highlightthickness=0,
                 font=("Microsoft YaHei UI", 9)).grid(
                     row=0, column=1, sticky="ew", ipady=5, padx=(6, 0))
        tk.Button(path_row, text="Browse",
                  bg=C.BG, fg=C.ACCENT,
                  font=("Microsoft YaHei UI", 9, "bold"),
                  relief="flat", padx=10, pady=4, cursor="hand2",
                  command=self._browse).grid(row=0, column=2, sticky="e", padx=(8, 0))

        # ── status card ──
        status = tk.Frame(root, bg=C.SURFACE,
                          highlightthickness=1, highlightbackground=C.BORDER)
        status.pack(fill="x", pady=(12, 0), padx=24)
        inner = tk.Frame(status, bg=C.SURFACE)
        inner.pack(fill="x", padx=14, pady=10)

        cur_row = tk.Frame(inner, bg=C.SURFACE)
        cur_row.pack(fill="x")
        tk.Label(cur_row, text="▶", bg=C.SURFACE, fg=C.ACCENT,
                 font=("Segoe UI", 10)).pack(side="left")
        tk.Label(cur_row, textvariable=self.current_var,
                 bg=C.SURFACE, fg=C.TEXT,
                 font=("Microsoft YaHei UI", 10), anchor="w").pack(side="left", padx=(6,0))

        self._result_label = tk.Label(inner, textvariable=self.result_var,
                                       bg=C.SURFACE, fg=C.MUTED,
                                       font=("Microsoft YaHei UI", 10), anchor="w")
        self._result_label.pack(fill="x", pady=(4, 0))

        prog_row = tk.Frame(inner, bg=C.SURFACE)
        prog_row.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(prog_row,
                                         style="Blue.Horizontal.TProgressbar",
                                         maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        tk.Label(prog_row, textvariable=self.progress_var,
                 bg=C.SURFACE, fg=C.MUTED,
                 font=("Segoe UI", 10), width=6, anchor="e").pack(side="left", padx=(8,0))

        # ── buttons ──
        btn_row = tk.Frame(root, bg=C.BG)
        btn_row.pack(fill="x", pady=(14, 20), **PAD)

        self.start_btn = tk.Button(btn_row, text="⬇  Download",
                                    bg=C.ACCENT, fg="#FFFFFF",
                                    font=("Microsoft YaHei UI", 9, "bold"),
                                    relief="flat", padx=18, pady=7,
                                    cursor="hand2",
                                    activebackground=C.ACCENT,
                                    activeforeground="#FFFFFF",
                                    command=self._start)
        self.start_btn.pack(side="left")

        self.pause_btn = tk.Button(btn_row, text="⏸  Pause",
                                    bg=C.SURFACE, fg=C.ACCENT,
                                    font=("Microsoft YaHei UI", 9, "bold"),
                                    relief="flat", padx=18, pady=7,
                                    cursor="hand2", state="disabled",
                                    highlightthickness=1,
                                    highlightbackground=C.BORDER,
                                    activebackground=C.SURFACE,
                                    activeforeground=C.ACCENT,
                                    command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=(10, 0))

        self.stop_btn = tk.Button(btn_row, text="⏹  Stop",
                                   bg=C.SURFACE, fg=C.DANGER,
                                   font=("Microsoft YaHei UI", 9, "bold"),
                                   relief="flat", padx=18, pady=7,
                                   cursor="hand2", state="disabled",
                                   highlightthickness=1,
                                   highlightbackground=C.BORDER,
                                   activebackground=C.SURFACE,
                                   activeforeground=C.DANGER,
                                   command=self._stop)
        self.stop_btn.pack(side="left", padx=(10, 0))

    def _build_panel(self):
        C = self
        p = self._panel

        hdr = tk.Frame(p, bg=C.SURFACE)
        hdr.pack(fill="x", padx=12, pady=(12, 2))
        tk.Label(hdr, text="Track List", bg=C.SURFACE, fg=C.TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self._fail_label = tk.Label(hdr, text="", bg=C.SURFACE, fg=C.DANGER,
                                     font=("Microsoft YaHei UI", 9))
        self._fail_label.pack(side="right")

        tk.Label(p, bg=C.SURFACE, fg=C.MUTED,
                 font=("Microsoft YaHei UI", 8),
                 text="Click / Ctrl+Click / Shift+Click to select\nDouble-click: select from here to end",
                 justify="left").pack(anchor="w", padx=12, pady=(0, 4))

        ctrl = tk.Frame(p, bg=C.SURFACE)
        ctrl.pack(fill="x", padx=12, pady=(0, 6))
        tk.Button(ctrl, text="All", bg=C.ACCENT2, fg=C.ACCENT,
                  font=("Microsoft YaHei UI", 9, "bold"),
                  relief="flat", padx=8, pady=3, cursor="hand2",
                  command=self._select_all).pack(side="left", padx=(0, 6))
        tk.Button(ctrl, text="None", bg=C.ACCENT2, fg=C.ACCENT,
                  font=("Microsoft YaHei UI", 9, "bold"),
                  relief="flat", padx=8, pady=3, cursor="hand2",
                  command=self._select_none).pack(side="left")

        tk.Frame(p, bg=C.BORDER, height=1).pack(fill="x", padx=12, pady=(0, 4))

        list_frame = tk.Frame(p, bg=C.SURFACE)
        list_frame.pack(fill="both", expand=True, padx=12)

        canvas = tk.Canvas(list_frame, bg=C.SURFACE, highlightthickness=0, bd=0)
        sb = tk.Scrollbar(list_frame, orient="vertical", command=canvas.yview,
                          width=10, relief="flat", bd=0,
                          bg=C.BORDER, troughcolor=C.SURFACE)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._list_canvas = canvas
        self._list_inner  = tk.Frame(canvas, bg=C.SURFACE)
        self._canvas_win  = canvas.create_window((0, 0), window=self._list_inner, anchor="nw")
        self._list_inner.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
            lambda e: canvas.itemconfig(self._canvas_win, width=e.width))
        p.bind("<Enter>", lambda e: canvas.bind_all(
            "<MouseWheel>",
            lambda ev: canvas.yview_scroll(-1*(ev.delta//120), "units")))
        p.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))

    def _populate_panel(self, tracks: list[dict]):
        self._track_list   = tracks
        self._song_vars    = []
        self._song_rows    = []
        self._last_clicked = -1
        self._fail_count   = 0
        self._fail_label.config(text="")

        for w in self._list_inner.winfo_children():
            w.destroy()

        C = self
        for i, t in enumerate(tracks):
            var = tk.BooleanVar(value=True)
            self._song_vars.append(var)

            row = tk.Frame(self._list_inner, bg=C.ACCENT2, cursor="hand2")
            row.pack(fill="x", pady=1)
            self._song_rows.append(row)

            num = tk.Label(row, text=f"{i+1}.", bg=C.ACCENT2, fg=C.MUTED,
                           font=("Microsoft YaHei UI", 8), width=3, anchor="e")
            num.pack(side="left", padx=(4, 2))

            display = f"{t['name']}  —  {t['artist']}" if t.get("artist") else t["name"]
            lbl = tk.Label(row, text=display, bg=C.ACCENT2, fg=C.TEXT,
                           font=("Microsoft YaHei UI", 9), anchor="w", cursor="hand2")
            lbl.pack(side="left", fill="x", expand=True, padx=(0, 6), pady=3)

            status_lbl = tk.Label(row, text="", bg=C.ACCENT2,
                                   font=("Segoe UI", 9), width=2)
            status_lbl.pack(side="right", padx=(0, 4))

            t["_row"]    = row
            t["_lbl"]    = lbl
            t["_num"]    = num
            t["_status"] = status_lbl
            t["status"]  = "pending"
            t["path"]    = None

            for widget in [row, num, lbl, status_lbl]:
                widget.bind("<Button-1>",
                    lambda e, idx=i: self._on_row_click(idx, e))
                widget.bind("<Double-Button-1>",
                    lambda e, idx=i: self._on_row_dblclick(idx))

        self._list_inner.update_idletasks()
        self._list_canvas.configure(scrollregion=self._list_canvas.bbox("all"))

    def _update_track_status(self, idx: int, status: str, path=None):
        if idx < 0 or idx >= len(self._track_list):
            return
        t = self._track_list[idx]
        t["status"] = status
        t["path"]   = path
        C = self

        if status == "ok":
            bg, fg = C.SURFACE, C.TEXT
            icon   = "✓"
            icon_c = C.SUCCESS
            if path:
                for w in [t["_row"], t["_lbl"]]:
                    w.bind("<Button-1>", lambda e, p=path: self._open_file(p))
                t["_lbl"].config(cursor="hand2")
                t["_num"].config(cursor="hand2")
        elif status == "failed":
            bg, fg = "#FFF0F0", C.DANGER
            icon, icon_c = "✗", C.DANGER
            self._fail_count += 1
            self._fail_label.config(text=f"{self._fail_count}/{len(self._track_list)} failed")
        elif status == "skipped":
            bg, fg = C.SURFACE, C.MUTED
            icon, icon_c = "–", C.MUTED
            # Try to find existing file and make it clickable
            if path is None and idx >= 0 and idx < len(self._track_list):
                t2 = self._track_list[idx]
                save_dir = Path(self.save_dir_var.get().strip() or DEFAULT_SAVE_DIR)
                name   = t2.get("name", "")
                artist = t2.get("artist", "")
                stem   = song_stem(name, artist)
                for ext in (".mp3", ".flac", ".m4a", ".ogg"):
                    candidate = save_dir / (stem + ext)
                    if candidate.exists():
                        path = candidate
                        t["path"] = path
                        break
            if path:
                for w in [t["_row"], t["_lbl"]]:
                    w.bind("<Button-1>", lambda e, p=path: self._open_file(p))
                t["_lbl"].config(cursor="hand2")
        elif status == "active":
            bg, fg = "#EEF5FF", C.ACCENT
            icon, icon_c = "▶", C.ACCENT
        else:
            return

        t["_row"].config(bg=bg)
        t["_lbl"].config(bg=bg, fg=fg)
        t["_num"].config(bg=bg)
        t["_status"].config(bg=bg, text=icon, fg=icon_c)

    def _open_file(self, path):
        if path and Path(path).exists():
            import subprocess as sp
            sp.Popen(["start", "", str(path)], shell=True)

    def _refresh_row_bg(self, idx: int, checked: bool):
        if idx >= len(self._track_list):
            return
        t  = self._track_list[idx]
        if t.get("status") in ("ok", "failed", "skipped", "active"):
            return
        C  = self
        bg = C.ACCENT2 if checked else C.SURFACE
        fg = C.TEXT    if checked else C.MUTED
        t["_row"].config(bg=bg)
        for w in t["_row"].winfo_children():
            try:
                w.config(bg=bg, fg=fg)
            except tk.TclError:
                pass

    def _on_row_click(self, idx: int, event):
        t = self._track_list[idx]
        if t.get("status") == "ok" and t.get("path"):
            self._open_file(t["path"])
            return
        ctrl  = (event.state & 0x4) != 0
        shift = (event.state & 0x1) != 0
        if shift and self._last_clicked >= 0:
            lo, hi = sorted([self._last_clicked, idx])
            for i in range(lo, hi + 1):
                self._song_vars[i].set(True)
                self._refresh_row_bg(i, True)
        elif ctrl:
            new_val = not self._song_vars[idx].get()
            self._song_vars[idx].set(new_val)
            self._refresh_row_bg(idx, new_val)
            self._last_clicked = idx
        else:
            new_val = not self._song_vars[idx].get()
            self._song_vars[idx].set(new_val)
            self._refresh_row_bg(idx, new_val)
            self._last_clicked = idx

    def _on_row_dblclick(self, idx: int):
        for i in range(len(self._song_vars)):
            val = i >= idx
            self._song_vars[i].set(val)
            self._refresh_row_bg(i, val)
        self._last_clicked = idx

    def _clear_panel(self):
        """Clear track list and reset panel state."""
        self._track_list   = []
        self._song_vars    = []
        self._song_rows    = []
        self._last_clicked = -1
        self._fail_count   = 0
        if hasattr(self, '_fail_label'):
            self._fail_label.config(text="")
        if hasattr(self, '_list_inner'):
            for w in self._list_inner.winfo_children():
                w.destroy()

    def _reset_pending_tracks(self):
        """After stop, reset non-completed tracks back to selected (default) state."""
        C = self
        for i, t in enumerate(self._track_list):
            if t.get("status") in ("active", "pending", None):
                t["status"] = "pending"
                self._song_vars[i].set(True)
                bg, fg = C.ACCENT2, C.TEXT
                t["_row"].config(bg=bg)
                t["_lbl"].config(bg=bg, fg=fg, cursor="hand2")
                t["_num"].config(bg=bg)
                t["_status"].config(bg=bg, text="", fg=fg)

    def _toggle_panel(self, open_: bool):
        if open_:
            self._panel.config(width=self.PANEL_W)
            self._panel_open = True
            self.root.geometry(f"{720 + self.PANEL_W}x620")
        else:
            self._panel.config(width=0)
            self._panel_open = False
            self.root.geometry("720x620")

    def _select_all(self):
        for i, v in enumerate(self._song_vars):
            v.set(True)
            self._refresh_row_bg(i, True)

    def _select_none(self):
        """Deselect all. If called after a stopped run, also resets download history."""
        C = self
        for i, t in enumerate(self._track_list):
            self._song_vars[i].set(False)
            # Reset visual state regardless of download history
            t["status"] = "pending"
            t["path"]   = None
            bg, fg = C.SURFACE, C.MUTED
            t["_row"].config(bg=bg)
            t["_lbl"].config(bg=bg, fg=fg, cursor="hand2")
            t["_num"].config(bg=bg, fg=fg)
            t["_status"].config(bg=bg, text="", fg=fg)
            # Unbind any file-open handlers, restore selection handler
            for w in [t["_row"], t["_lbl"]]:
                idx = self._track_list.index(t)
                w.bind("<Button-1>",
                    lambda e, i=idx: self._on_row_click(i, e))
        self._fail_count = 0
        self._fail_label.config(text="")

    def _set_mode(self, mode: str):
        self.mode_var.set(mode)
        for val, btn in self._tab_btns.items():
            if val == mode:
                btn.config(bg=self.ACCENT, fg="#FFFFFF")
            else:
                btn.config(bg=self.ACCENT2, fg=self.ACCENT)
        self.input_text.delete("1.0", "end")
        if mode == "playlist":
            self._hint.config(text="Paste a playlist URL from NetEase or QQ Music.")
        else:
            self._hint.config(
                text="Enter a song name or song + artist. Use  |  or new lines for multiple songs.")
        # Clear panel and status on mode switch
        self._pending_save_dir = None
        self._clear_panel()
        self.current_var.set("")
        self.result_var.set("")
        if hasattr(self, 'progress'):
            self.progress["value"] = 0
        self.progress_var.set("")
        if self._panel_open:
            self._toggle_panel(False)

    def _browse(self):
        d = filedialog.askdirectory(
            initialdir=self.save_dir_var.get() or str(DEFAULT_SAVE_DIR))
        if d:
            self.save_dir_var.set(d)

    def _start(self):
        if self.running:
            return

        # If tracks already loaded and waiting — start download now
        if self._pending_save_dir is not None and self._track_list:
            self._kick_off(self._track_list, self._pending_save_dir)
            self._pending_save_dir = None
            return

        text = self.input_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Notice", "Please enter a track name or playlist URL.")
            return
        save_dir = Path(self.save_dir_var.get().strip() or DEFAULT_SAVE_DIR)
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            messagebox.showerror("Invalid save path", str(exc))
            return

        if self.mode_var.get() == "playlist":
            self._fetch_and_show(text, save_dir)
        else:
            queries = split_queries(text)
            tracks  = [{"keyword": q, "name": q, "artist": ""} for q in queries]
            self._populate_panel(tracks)
            self._toggle_panel(True)
            self._pending_save_dir = save_dir
            self.result_var.set("Select tracks, then click Download to start.")
            self._result_label.config(fg=self.ACCENT)

    def _fetch_and_show(self, url: str, save_dir: Path):
        """Fetch playlist and show track list — do NOT start download yet."""
        self.current_var.set("Loading playlist...")
        self.result_var.set("")

        def _fetch():
            try:
                if "163.com" in url:
                    songs = netease_playlist(url)
                elif "qq.com" in url:
                    songs = qq_playlist(url)
                else:
                    songs = []
                self.queue.put(("playlist_fetched", songs, save_dir))
            except Exception as exc:
                self.queue.put(("playlist_error", str(exc)))

        threading.Thread(target=_fetch, daemon=True).start()

    def _kick_off(self, tracks: list[dict], save_dir: Path):
        selected = [t for t, v in zip(tracks, self._song_vars) if v.get()]
        if not selected:
            messagebox.showwarning("Notice", "No tracks selected.")
            return
        self.running = True
        self.stop_event.clear()
        self.pause_event.clear()
        self.start_btn.config(state="disabled", bg="#A0AEED", activebackground="#A0AEED")
        self.pause_btn.config(state="normal", text="⏸  Pause")
        self.stop_btn.config(state="normal")
        self.progress["value"] = 0
        self.progress_var.set("")
        self.current_var.set("Starting...")
        self.result_var.set("")
        self.worker = threading.Thread(
            target=self._run_tracks, args=(selected, save_dir), daemon=True)
        self.worker.start()

    def _run_tracks(self, tracks: list[dict], save_dir: Path):
        try:
            total = len(tracks)
            for i, t in enumerate(tracks, 1):
                if self.stop_event.is_set():
                    break
                wait_if_paused(self.pause_event, self.stop_event)
                keyword = t.get("keyword") or f"{t['name']} {t.get('artist','')}".strip()
                idx     = self._track_list.index(t) if t in self._track_list else -1
                self._set_current(
                    f"[{i}/{total}]  {t['name']}" +
                    (f"  —  {t['artist']}" if t.get("artist") else ""))
                self._set_progress(0)
                if idx >= 0:
                    self.queue.put(("track_status", idx, "active", None))

                downloaded = False
                if t.get("id") and not t.get("mid"):
                    self._saving("NetEase")
                    path, err = netease_download_song(
                        t, save_dir, self._set_progress_cb,
                        self.pause_event, self.stop_event)
                    if path or err == "SKIPPED":
                        res = self._handle_result(path, err, t["name"],
                                                  t.get("artist",""), "NetEase")
                        if res and idx >= 0:
                            st = "skipped" if err == "SKIPPED" else "ok"
                            self.queue.put(("track_status", idx, st, path))
                        downloaded = res
                elif t.get("mid"):
                    self._saving("QQ Music")
                    path, err = qq_download_song(
                        t, save_dir, self._set_progress_cb,
                        self.pause_event, self.stop_event)
                    if path or err == "SKIPPED":
                        res = self._handle_result(path, err, t["name"],
                                                  t.get("artist",""), "QQ Music")
                        if res and idx >= 0:
                            st = "skipped" if err == "SKIPPED" else "ok"
                            self.queue.put(("track_status", idx, st, path))
                        downloaded = res

                if not downloaded and not self.stop_event.is_set():
                    path = self._download_by_keyword(keyword, save_dir, idx)
                    if path is None and idx >= 0:
                        self.queue.put(("track_status", idx, "failed", None))
        except Exception as exc:
            self._error(f"Error: {exc}")
        finally:
            self.queue.put(("done", None))

    def _download_by_keyword(self, keyword: str, save_dir: Path, track_idx: int = -1):
        if self.stop_event.is_set():
            return None

        # NetEase
        try:
            song = choose_best_netease(keyword)
            if song:
                title, artist = song.get("name","Unknown"), netease_artist(song)
                self._set_progress(0)
                self._saving("NetEase")
                path, err = netease_download_song(
                    song, save_dir, self._set_progress_cb,
                    self.pause_event, self.stop_event)
                if err == "SKIPPED" or (path and not err):
                    self._handle_result(path, err, title, artist, "NetEase")
                    if track_idx >= 0:
                        st = "skipped" if err == "SKIPPED" else "ok"
                        self.queue.put(("track_status", track_idx, st, path))
                    return path
        except Exception:
            pass

        if self.stop_event.is_set():
            return None

        if not load_netease_cookies():
            self._log("⚠ NetEase cookie expired — update netease_cookie.txt", "warn")
        if not load_qq_cookies():
            self._log("⚠ QQ Music cookie expired — update qq_cookie.txt", "warn")

        # QQ Music
        try:
            song = choose_best_qq(keyword)
            if song:
                title, artist = song.get("name","Unknown"), qq_artist(song)
                self._set_progress(0)
                self._saving("QQ Music")
                path, err = qq_download_song(
                    song, save_dir, self._set_progress_cb,
                    self.pause_event, self.stop_event)
                if err == "SKIPPED" or (path and not err):
                    self._handle_result(path, err, title, artist, "QQ Music")
                    if track_idx >= 0:
                        st = "skipped" if err == "SKIPPED" else "ok"
                        self.queue.put(("track_status", track_idx, st, path))
                    return path
        except Exception:
            pass

        if self.stop_event.is_set():
            return None

        # YouTube
        self._set_progress(0)
        self._saving("YouTube")
        path, err = ytdlp_download(keyword, save_dir, self._set_progress_cb,
                                    self.pause_event, self.stop_event)
        if path:
            self._handle_result(path, None, path.stem, "", "YouTube")
            if track_idx >= 0:
                self.queue.put(("track_status", track_idx, "ok", path))
            return path

        if err and "stopped" not in err.lower():
            if any(k in (err or "").lower() for k in ["sign in", "bot", "netscape", "cookies file"]):
                self._error(f"All platforms failed: {keyword}  —  YouTube cookie invalid or expired, replace youtube_cookies.txt")
            else:
                self._error(f"All platforms failed: {keyword}")
        return None

    def _handle_result(self, path, err, title, artist, source) -> bool:
        if err == "SKIPPED" and path:
            self._log(f"Already exists, skipped: {path.name}", "muted")
            return True
        if path:
            std = path.parent / f"{song_stem(title, artist)}{path.suffix.lower()}"
            if path.resolve() != std.resolve() and path.exists() and not std.exists():
                path.rename(std)
                path = std
            self._log(f"☑  Saved via {source}  ·  {path.name}", "ok")
            return True
        if err and err != "已终止":
            self._log(f"⚠  {err}", "warn")
        return False

    def _saving(self, source: str, filename: str = ""):
        self._log(f"▢  Saving via {source}...", "info")

    def _set_current(self, msg: str):
        self.queue.put(("current", msg))

    def _log(self, msg: str, tag: str = "info"):
        self.queue.put(("result", msg, tag))

    def _error(self, msg: str):
        self.queue.put(("result", msg, "error"))

    def _set_progress(self, value: float):
        self.queue.put(("progress", max(0.0, min(100.0, value))))

    def _set_progress_cb(self, value: float, detail: str = ""):
        self._set_progress(value)

    def _poll_queue(self):
        COLOR_MAP = {
            "ok":    self.SUCCESS,
            "warn":  self.WARN,
            "error": self.DANGER,
            "muted": self.MUTED,
            "info":  self.ACCENT,
        }
        try:
            while True:
                item = self.queue.get_nowait()
                if item[0] == "current":
                    self.current_var.set(item[1])
                elif item[0] == "result":
                    msg = item[1]
                    tag = item[2] if len(item) > 2 else "info"
                    self.result_var.set(msg)
                    self._result_label.config(fg=COLOR_MAP.get(tag, self.MUTED))
                elif item[0] == "progress":
                    self.progress["value"] = item[1]
                    self.progress_var.set(f"{item[1]:.0f}%" if item[1] > 0 else "")
                elif item[0] == "track_status":
                    _, idx, status, path = item
                    self._update_track_status(idx, status, path)
                elif item[0] == "playlist_fetched":
                    _, songs, save_dir = item
                    if not songs:
                        self._error("No songs found. Check the URL or cookie.")
                        self.current_var.set("")
                    else:
                        tracks = []
                        for s in songs:
                            artist = (netease_artist(s)
                                      if (s.get("artists") or s.get("ar"))
                                      else qq_artist(s))
                            tracks.append({**s,
                                           "keyword": f"{s.get('name','')} {artist}".strip(),
                                           "name":    s.get("name", "Unknown"),
                                           "artist":  artist})
                        self._populate_panel(tracks)
                        self._toggle_panel(True)
                        self._pending_save_dir = save_dir
                        self.current_var.set(f"Playlist loaded — {len(tracks)} songs")
                        self.result_var.set("Select tracks, then click Download to start.")
                        self._result_label.config(fg=self.ACCENT)
                elif item[0] == "playlist_error":
                    self._error(f"Failed to load playlist: {item[1]}")
                    self.current_var.set("")
                elif item[0] == "done":
                    self._finish()
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _toggle_pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text="⏸  Pause")
            self._log("Resumed", "info")
        else:
            self.pause_event.set()
            self.pause_btn.config(text="▶  Resume")
            self._log("Paused", "muted")

    def _stop(self):
        self.stop_event.set()
        self.pause_event.clear()
        self._log("Stopping...", "warn")
        # Reset pending tracks back to selected state after stop
        self.root.after(500, self._reset_pending_tracks)

    def _finish(self):
        self.running = False
        self.start_btn.config(state="normal", bg=self.ACCENT,
                               activebackground=self.ACCENT)
        self.pause_btn.config(state="disabled", text="⏸  Pause")
        self.stop_btn.config(state="disabled")
        if self.stop_event.is_set():
            self.current_var.set("Stopped")
            self._log("Stopped", "muted")
            return
        self.progress["value"] = 100
        self.progress_var.set("100%")
        self.current_var.set("Done ✓")
        fails = self._fail_count
        if fails:
            self._log(f"Done — {fails} track(s) failed", "warn")
        else:
            self._log("All done!", "ok")


def main() -> None:
    root = tk.Tk()
    app = MusicDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
