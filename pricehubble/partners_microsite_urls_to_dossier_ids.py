#!/usr/bin/env python3
"""
Read a CSV of Partners microsite URLs, resolve each slug to a dossier id, write a new CSV.

How resolution works (same as the Page Builder SPA):
  POST https://api.pricehubble.com/api/v1/page-builder/link/{slug}/jwt
  → JWT payload contains dossierId / urn

Example short URL:
  https://dash.pricehubble.com/pb/LRDqFH75IpAsTk0O5ik9IAk5
loads with context urn:phi:dossier:9033d994-2c37-401a-9924-2be58acf55d8

Usage:
  python3 partners_microsite_urls_to_dossier_ids.py sites.csv
  python3 partners_microsite_urls_to_dossier_ids.py sites.csv --outfile with_dossiers.csv
  python3 partners_microsite_urls_to_dossier_ids.py sites.csv --url-column Site --limit 5
  python3 partners_microsite_urls_to_dossier_ids.py sites.csv --delay 0.2
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

JWT_URL = "https://api.pricehubble.com/api/v1/page-builder/link/{slug}/jwt"

URL_COLUMN_ALIASES = {
    "site",
    "sites",
    "url",
    "microsite",
    "micrositeurl",
    "microsite_url",
    "microsite link",
    "micrositelink",
    "page",
    "page url",
    "pb url",
    "pb_url",
}


def clean(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def strip_dossier_prefix(v: Any) -> str:
    """Return bare dossier UUID (strip urn:phi:dossier: / dossiers: if present)."""
    s = clean(v)
    if not s:
        return ""
    s = re.sub(r"^urn:phi:dossiers?:", "", s, flags=re.I)
    # also handle accidental full URNs stuffed into dossierId
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        s,
        flags=re.I,
    )
    return m.group(0) if m else s


def pick_url_column(headers: list[str], preferred: Optional[str]) -> str:
    if preferred:
        for h in headers:
            if h == preferred or h.lower() == preferred.lower():
                return h
        raise SystemExit(f"--url-column {preferred!r} not found. Headers: {headers}")

    for h in headers:
        key = re.sub(r"[\s_]+", "", h.lower())
        spaced = h.lower().strip()
        if spaced in URL_COLUMN_ALIASES or key in {re.sub(r"[\s_]+", "", a) for a in URL_COLUMN_ALIASES}:
            return h

    # fallback: first column that looks like a pb URL in the name
    for h in headers:
        if "http" in h.lower() or "/pb/" in h.lower() or "pricehubble" in h.lower():
            return h

    raise SystemExit(
        "Could not detect microsite URL column. Pass --url-column. Headers: " + ", ".join(headers)
    )


def extract_slug(url: str) -> Optional[str]:
    s = clean(url)
    if not s:
        return None
    # allow bare slug
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", s) and "://" not in s and "/" not in s:
        return s
    m = re.search(r"/pb/([^/?#]+)", s, flags=re.I)
    if m:
        return m.group(1)
    return None


def dossier_from_context_url(url: str) -> Optional[str]:
    """If the CSV already has a fully expanded URL with context=, parse it."""
    try:
        qs = parse_qs(urlparse(url).query)
        raw = qs.get("context", [None])[0]
        if not raw:
            return None
        ctx = json.loads(unquote(raw))
        urn = clean(ctx.get("urn") or "")
        if urn:
            did = strip_dossier_prefix(urn)
            if did:
                return did
        did = strip_dossier_prefix(ctx.get("dossierId") or ctx.get("dossier_id") or "")
        return did or None
    except Exception:
        return None


def b64url_decode(part: str) -> bytes:
    pad = "=" * (-len(part) % 4)
    return base64.urlsafe_b64decode(part + pad)


def dossier_from_jwt(token: str) -> tuple[Optional[str], Optional[str], dict]:
    parts = token.split(".")
    if len(parts) < 2:
        return None, None, {}
    try:
        payload = json.loads(b64url_decode(parts[1]).decode("utf-8"))
    except Exception:
        return None, None, {}
    dossier_id = strip_dossier_prefix(payload.get("dossierId") or "")
    urn = clean(payload.get("urn") or "")
    if not dossier_id and urn:
        dossier_id = strip_dossier_prefix(urn)
    if dossier_id and not urn:
        urn = "urn:phi:dossier:" + dossier_id
    elif dossier_id:
        urn = "urn:phi:dossier:" + dossier_id
    return dossier_id or None, urn or None, payload


def resolve_slug(slug: str, timeout: float = 30.0) -> tuple[Optional[str], Optional[str], str]:
    """
    Returns (dossier_id, dossier_urn, note).
    """
    url = JWT_URL.format(slug=slug)
    req = urllib.request.Request(
        url,
        data=b"{}",
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "partners-microsite-urls-to-dossier-ids/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        return None, None, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return None, None, f"request failed: {e}"

    token = clean(body.get("jwt") or body.get("token") or "")
    if not token:
        return None, None, "no jwt in response: " + json.dumps(body)[:200]

    dossier_id, urn, _ = dossier_from_jwt(token)
    if not dossier_id:
        return None, None, "jwt missing dossierId"
    return dossier_id, urn, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve Partners microsite URLs to dossier ids")
    ap.add_argument("csv_path", type=Path, help="Input CSV with a microsite URL column")
    ap.add_argument(
        "--outfile",
        type=Path,
        default=None,
        help="Output CSV (default: <input>_with_dossier_ids.csv)",
    )
    ap.add_argument("--url-column", default=None, help="Column name containing the microsite URL")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N data rows (0 = all)")
    ap.add_argument("--delay", type=float, default=0.15, help="Seconds between API calls")
    ap.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout seconds")
    args = ap.parse_args()

    if not args.csv_path.is_file():
        print(f"File not found: {args.csv_path}", file=sys.stderr)
        return 1

    outfile = args.outfile or args.csv_path.with_name(
        args.csv_path.stem + "_with_dossier_ids.csv"
    )

    with args.csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("CSV has no header row", file=sys.stderr)
            return 1
        headers = list(reader.fieldnames)
        url_col = pick_url_column(headers, args.url_column)
        rows = list(reader)

    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    out_headers = list(headers)
    for extra in ("dossier_id", "dossier_urn", "pb_slug", "resolve_status"):
        if extra not in out_headers:
            out_headers.append(extra)

    ok = 0
    fail = 0
    with outfile.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_headers, extrasaction="ignore")
        writer.writeheader()

        for i, row in enumerate(rows, 1):
            url = clean(row.get(url_col))
            slug = extract_slug(url)
            dossier_id = dossier_from_context_url(url) if url else None
            urn = f"urn:phi:dossier:{dossier_id}" if dossier_id else None
            status = "from_context" if dossier_id else ""

            if not dossier_id:
                if not slug:
                    status = "missing_url_or_slug"
                    fail += 1
                else:
                    dossier_id, urn, status = resolve_slug(slug, timeout=args.timeout)
                    if dossier_id:
                        ok += 1
                    else:
                        fail += 1
                    if args.delay > 0:
                        time.sleep(args.delay)

            out = dict(row)
            dossier_id = strip_dossier_prefix(dossier_id) if dossier_id else ""
            out["dossier_id"] = dossier_id
            out["dossier_urn"] = ("urn:phi:dossier:" + dossier_id) if dossier_id else ""
            out["pb_slug"] = slug or ""
            out["resolve_status"] = status
            writer.writerow(out)

            label = dossier_id or status
            print(f"[{i}/{len(rows)}] {slug or '-'} → {label}", file=sys.stderr)

    print(f"Wrote {outfile} (ok={ok}, fail={fail}, total={len(rows)})", file=sys.stderr)
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
