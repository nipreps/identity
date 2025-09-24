#!/usr/bin/env python3
"""
Create a 16:9 collage of all members' GitHub avatars for an organization.

Usage:
  export GITHUB_TOKEN=ghp_xxx
  python members_collage.py --org nipreps --out nipreps_collage.png --width 3840 --height 2160

Requires: requests pillow
"""
import argparse
import io
import math
import os
import sys
import time
from pathlib import Path
from typing import List, Tuple, Optional
import requests
from PIL import Image

API_BASE = "https://api.github.com"

def _session_with_retries(total_retries: int = 5, backoff: float = 0.5) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/vnd.github+json",
        "User-Agent": "nipreps-collage/1.0",
    })
    s.total_retries = total_retries
    s.backoff = backoff
    return s

def _get(session: requests.Session, url: str, token: str, params=None):
    # Simple manual retry loop
    for attempt in range(session.total_retries + 1):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = session.get(url, headers=headers, params=params, timeout=30)
        if r.status_code in (200, 201):
            return r
        # Rate limit handling
        if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(r.headers.get("X-RateLimit-Reset", "0"))
            sleep_s = max(0, reset - int(time.time()) + 1)
            time.sleep(sleep_s)
            continue
        # Retry on 5xx or transient network issues
        if r.status_code >= 500 and attempt < session.total_retries:
            time.sleep(session.backoff * (2 ** attempt))
            continue
        # Otherwise, give up
        r.raise_for_status()
    raise RuntimeError("Unreachable retry logic")

def list_org_members(org: str, token: str, include_only_public: bool = False) -> List[dict]:
    """
    Returns a list of user objects.
    Note: If your token lacks appropriate org read permissions,
    GitHub may return only public members.
    """
    # Endpoint: List organization members (requires proper auth) or public members
    url = f"{API_BASE}/orgs/{org}/members"
    if include_only_public:
        url = f"{API_BASE}/orgs/{org}/public_members"

    session = _session_with_retries()
    members = []
    params = {"per_page": 100}
    while True:
        r = _get(session, url, token, params=params)
        chunk = r.json()
        if not isinstance(chunk, list):
            raise ValueError(f"Unexpected response: {chunk}")
        members.extend(chunk)

        # Pagination via Link header
        link = r.headers.get("Link", "")
        next_url = None
        if link:
            parts = link.split(",")
            for part in parts:
                seg = part.strip()
                if 'rel="next"' in seg:
                    next_url = seg[seg.find("<")+1: seg.find(">")]
                    break
        if next_url:
            url = next_url
            params = None  # next_url already has params
        else:
            break
    return members

def download_avatar(session: requests.Session, login: str, avatar_url: str, dest_dir: Path, token: str) -> Optional[Path]:
    """
    Downloads a user's avatar. Returns path or None on failure.
    We append `s=512` to request a reasonably large square.
    """
    try:
        url = avatar_url
        # GitHub supports size param for avatars up to 460-ish; 512 often works via redirects.
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}s=512"
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = session.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        ext = ".png"
        ctype = r.headers.get("Content-Type", "")
        if "jpeg" in ctype:
            ext = ".jpg"
        elif "png" in ctype:
            ext = ".png"
        elif "gif" in ctype:
            ext = ".gif"
        out_path = dest_dir / f"{login}{ext}"
        out_path.write_bytes(r.content)
        return out_path
    except Exception as e:
        print(f"[WARN] Failed to download avatar for {login}: {e}", file=sys.stderr)
        return None

def best_grid(n: int, aspect_w: int = 16, aspect_h: int = 9) -> Tuple[int, int]:
    """
    Find cols, rows for n images whose cols/rows approximates aspect_w/aspect_h
    with cols*rows >= n and minimal difference to target aspect.
    """
    target = aspect_w / aspect_h
    # Start near sqrt-based estimate
    rows_est = max(1, int(math.sqrt(n / target)))
    candidates = []
    # Explore a small neighborhood for robustness
    for rows in range(max(1, rows_est - 5), rows_est + 6):
        cols = math.ceil(n / rows)
        diff = abs((cols / rows) - target)
        area = cols * rows
        candidates.append((diff, area, cols, rows))
    # Prefer closest aspect, then smaller grid area (less empty slots), then fewer rows
    _, _, best_cols, best_rows = sorted(candidates, key=lambda x: (x[0], x[1], x[3]))[0]
    return best_cols, best_rows

def make_collage(image_paths: List[Path],
                 out_path: Path,
                 width: int = 3840,
                 height: int = 2160,
                 background=(255, 255, 255)) -> None:
    """
    Assemble images into a centered grid filling a width×height canvas.
    Thumbnails are square, preserving aspect (letterboxed if needed).
    """
    n = len(image_paths)
    if n == 0:
        raise ValueError("No images to compose.")

    cols, rows = best_grid(n, 16, 9)

    # Compute square cell size that fits within canvas
    cell = min(width // cols, height // rows)
    # Compute margins to center the grid
    grid_w = cell * cols
    grid_h = cell * rows
    margin_x = (width - grid_w) // 2
    margin_y = (height - grid_h) // 2

    canvas = Image.new("RGB", (width, height), color=background)

    def open_as_square_thumb(p: Path, size: int) -> Image.Image:
        img = Image.open(p).convert("RGB")
        # Fit into square while preserving aspect (pad with background if needed)
        img_w, img_h = img.size
        scale = min(size / img_w, size / img_h)
        new_w = max(1, int(img_w * scale))
        new_h = max(1, int(img_h * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        square = Image.new("RGB", (size, size), color=background)
        off_x = (size - new_w) // 2
        off_y = (size - new_h) // 2
        square.paste(img, (off_x, off_y))
        return square

    # Paste in row-major order
    i = 0
    for r in range(rows):
        for c in range(cols):
            if i >= n:
                break
            thumb = open_as_square_thumb(image_paths[i], cell)
            x = margin_x + c * cell
            y = margin_y + r * cell
            canvas.paste(thumb, (x, y))
            i += 1

    # Save as PNG
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG", optimize=True)

def main():
    ap = argparse.ArgumentParser(description="Build a 16:9 collage of GitHub org members' avatars.")
    ap.add_argument("--org", default="nipreps", help="GitHub organization name (default: nipreps)")
    ap.add_argument("--out", default="collage.png", help="Output PNG path")
    ap.add_argument("--width", type=int, default=3840, help="Output width (default: 3840)")
    ap.add_argument("--height", type=int, default=2160, help="Output height (default: 2160)")
    ap.add_argument("--public-only", action="store_true", help="Use public_members endpoint (if you prefer)")
    ap.add_argument("--cache-dir", default=".cache/nipreps-avatars", help="Directory to cache avatar images")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("ERROR: Please set GITHUB_TOKEN with org read permissions.", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Listing members of org '{args.org}'...")
    members = list_org_members(args.org, token, include_only_public=args.public_only)
    print(f"[INFO] Found {len(members)} member(s).")

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    session = _session_with_retries()
    avatar_paths: List[Path] = []
    for m in members:
        login = m.get("login")
        avatar_url = m.get("avatar_url")
        if not login or not avatar_url:
            continue
        out = download_avatar(session, login, avatar_url, cache_dir, token)
        if out:
            avatar_paths.append(out)

    # Sort for deterministic layout
    avatar_paths.sort(key=lambda p: p.stem.lower())

    print(f"[INFO] Building collage ({len(avatar_paths)} avatars) at {args.width}x{args.height}...")
    make_collage(avatar_paths, Path(args.out), width=args.width, height=args.height)
    print(f"[DONE] Saved: {args.out}")

if __name__ == "__main__":
    main()
