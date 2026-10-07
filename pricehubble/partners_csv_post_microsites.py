#!/usr/bin/env python3
"""
Read a Partners loan CSV, POST each row to n8n, print TSV for Sheets paste.

Columns out: Name, Street, Site, Result, Notes

Usage:
  python3 partners_csv_post_microsites.py loans.csv
  python3 partners_csv_post_microsites.py loans.csv --dry-run
  python3 partners_csv_post_microsites.py loans.csv --limit 3
  python3 partners_csv_post_microsites.py loans.csv --outfile results.tsv

Example CSV row (header optional):
  Bryan Lynch,Joel Morgan,43971,386000,Conventional,3.375,520000,74.231,Refinancing,
  19315 MAHOGANY DR,Oregon City,OR,97045,1 Unit,Mary,Lynch,mlynch052798@gmail.com,
  831-776-2081,831-776-2081,...,520000,OwnerOccupied
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

DEFAULT_WEBHOOK = "https://pricehubble.app.n8n.cloud/webhook/22df6627-6f77-4a98-8e33-bdbe57a763ad"

# CSV header label (lower) -> canonical key
HEADER_ALIASES = {
    "name": "name",
    "loan officer": "loan_officer",
    "funded date": "funded_date",
    "total loan amt (w/ mip/ff)": "loan_amt",
    "total loan amt": "loan_amt",
    "loan type": "loan_type",
    "note rate": "note_rate",
    "property value": "property_value",
    "ltv": "ltv",
    "loan purpose": "loan_purpose",
    "subject property address": "street",
    "subject property city": "city",
    "subject property state": "state",
    "subject property zip": "post_code",
    "subject property type": "property_type_raw",
    "borrower first name": "first_name",
    "borrower last name": "last_name",
    "borr email": "email",
    "borr cell": "cell",
    "borr home phone": "home_phone",
    "appraised value": "appraised_value",
    "occupancy type": "occupancy",
}

# Positional fallback when no header
POS = {
    "name": 0,
    "loan_officer": 1,
    "funded_date": 2,
    "loan_amt": 3,
    "loan_type": 4,
    "note_rate": 5,
    "property_value": 6,
    "ltv": 7,
    "loan_purpose": 8,
    "street": 9,
    "city": 10,
    "state": 11,
    "post_code": 12,
    "property_type_raw": 13,
    "first_name": 14,
    "last_name": 15,
    "email": 16,
    "cell": 17,
    "home_phone": 18,
    "appraised_value": 29,
}


def clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("-", "N/A", "n/a", "null", "None"):
        return ""
    return s


def to_num(v: Any) -> Optional[float]:
    s = clean(v).replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    if n == 0:
        return None
    return int(n) if n == int(n) else n


def excel_serial_to_iso(v: Any) -> Optional[str]:
    s = clean(v)
    if not s or s == "0":
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    try:
        n = int(float(s))
    except ValueError:
        return None
    if n <= 0:
        return None
    return (date(1899, 12, 30) + timedelta(days=n)).isoformat()


def phone_e164(v: Any) -> Optional[str]:
    s = clean(v)
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else None


def map_property_type(raw: Any) -> str:
    t = clean(raw).lower()
    if "condo" in t or "apartment" in t:
        return "apartment"
    return "house"


def looks_like_header(row: list[str]) -> bool:
    joined = " ".join(clean(c).lower() for c in row[:16])
    return (
        "borrower" in joined
        or "subject property" in joined
        or (clean(row[0]).lower() == "name" and "loan" in joined)
    )


def row_to_dict(row: list[str], header_map: Optional[dict[str, int]]) -> dict[str, str]:
    out: dict[str, str] = {}
    if header_map:
        for key, idx in header_map.items():
            if idx < len(row):
                out[key] = clean(row[idx])
        return out
    for key, idx in POS.items():
        out[key] = clean(row[idx]) if idx < len(row) else ""
    return out


def build_payload(r: dict[str, str]) -> Optional[dict[str, Any]]:
    street = r.get("street", "")
    city = r.get("city", "")
    state = r.get("state", "")
    post = r.get("post_code", "")
    if "-" in post:
        post = post.split("-")[0]
    if len(post) > 5:
        post = post[:5]

    if not street or street.upper() == "TBD":
        return None
    if not city or not state or not post:
        return None

    purchase = to_num(r.get("property_value")) or to_num(r.get("appraised_value"))
    principal = to_num(r.get("loan_amt"))
    rate = to_num(r.get("note_rate"))
    anchor = excel_serial_to_iso(r.get("funded_date"))

    prop: dict[str, Any] = {
        "propertyType": map_property_type(r.get("property_type_raw")),
    }
    if purchase is not None:
        prop["purchasePrice"] = purchase
    if anchor:
        prop["anchorSale"] = anchor

    mort: dict[str, Any] = {}
    if principal is not None:
        mort["outstandingPrincipal"] = principal
    if rate is not None:
        mort["interestRatePct"] = rate

    payload: dict[str, Any] = {
        "street": street,
        "city": city,
        "state": state,
        "postCode": post,
        "firstName": r.get("first_name", ""),
        "lastName": r.get("last_name", ""),
        "email": r.get("email", ""),
        "phoneNumber": phone_e164(r.get("cell") or r.get("home_phone")),
        "property": prop,
    }
    if mort:
        payload["mortgage"] = mort
    return payload


def post_json(url: str, payload: dict, timeout: float = 180.0) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return e.code, parsed
    except Exception as e:
        return 0, {"error": str(e)}


def extract_site(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    for key in (
        "micrositeUrl",
        "microsite_url",
        "pageUrl",
        "page_url",
        "url",
        "site",
    ):
        v = resp.get(key)
        if v:
            return str(v)
    for nest in ("data", "result", "body"):
        inner = resp.get(nest)
        if isinstance(inner, dict):
            s = extract_site(inner)
            if s:
                return s
    return ""


def extract_result(status: int, resp: Any) -> str:
    if isinstance(resp, dict):
        st = resp.get("status")
        if st:
            return str(st)
        if resp.get("error"):
            return "error"
    if 200 <= status < 300:
        return "ok"
    if status == 0:
        return "error"
    return f"http_{status}"


def extract_notes(status: int, resp: Any) -> str:
    if isinstance(resp, dict):
        err = resp.get("error") or resp.get("message")
        if err:
            return str(err)[:200]
        if resp.get("status") == "error" and resp.get("step"):
            return f"step={resp.get('step')}"
    return ""


def tsv_escape(s: str) -> str:
    return (s or "").replace("\t", " ").replace("\n", " ").replace("\r", "")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("csv_path", type=Path, help="Path to loan CSV")
    ap.add_argument("--webhook", default=DEFAULT_WEBHOOK, help="n8n webhook URL")
    ap.add_argument("--dry-run", action="store_true", help="Do not POST")
    ap.add_argument("--limit", type=int, default=0, help="Max rows (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.5, help="Delay between POSTs")
    ap.add_argument("--outfile", type=Path, default=None, help="Write TSV here too")
    ap.add_argument("--payloads-out", type=Path, default=None, help="Write JSON payloads")
    args = ap.parse_args()

    if not args.csv_path.exists():
        print(f"ERROR: file not found: {args.csv_path}", file=sys.stderr)
        return 1

    with args.csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        print("ERROR: empty CSV", file=sys.stderr)
        return 1

    header_map: Optional[dict[str, int]] = None
    start = 0
    if looks_like_header(rows[0]):
        header_map = {}
        for i, col in enumerate(rows[0]):
            key = HEADER_ALIASES.get(clean(col).lower())
            if key:
                header_map[key] = i
        start = 1

    lines_out = ["Name\tStreet\tSite\tResult\tNotes"]
    payloads_dump: list[dict] = []
    processed = 0

    for row in rows[start:]:
        if not row or not any(clean(c) for c in row):
            continue
        if args.limit and processed >= args.limit:
            break

        r = row_to_dict(row, header_map)
        name = (
            f"{r.get('first_name', '')} {r.get('last_name', '')}".strip()
            or r.get("name", "")
        )
        street_disp = ", ".join(
            p
            for p in [
                r.get("street", ""),
                r.get("city", ""),
                f"{r.get('state', '')} {r.get('post_code', '')}".strip(),
            ]
            if p
        )

        payload = build_payload(r)
        processed += 1

        if payload is None:
            lines_out.append(
                "\t".join(
                    tsv_escape(x)
                    for x in [name, street_disp, "", "skipped", "no address"]
                )
            )
            print(f"[{processed}] SKIP (no address): {name}", file=sys.stderr)
            continue

        payloads_dump.append(payload)

        if args.dry_run:
            lines_out.append(
                "\t".join(
                    tsv_escape(x) for x in [name, street_disp, "", "dry_run", ""]
                )
            )
            print(
                f"[{processed}] DRY-RUN: {name} -> {payload['street']}",
                file=sys.stderr,
            )
            continue

        print(f"[{processed}] POST: {name} ...", file=sys.stderr, end=" ", flush=True)
        status, resp = post_json(args.webhook, payload)
        site = extract_site(resp)
        result = extract_result(status, resp)
        notes = extract_notes(status, resp)
        print(f"{result} {site or ''}", file=sys.stderr)
        lines_out.append(
            "\t".join(
                tsv_escape(x) for x in [name, street_disp, site, result, notes]
            )
        )
        if args.sleep > 0:
            time.sleep(args.sleep)

    text = "\n".join(lines_out) + "\n"
    sys.stdout.write(text)

    if args.outfile:
        args.outfile.write_text(text, encoding="utf-8")
        print(f"Wrote {args.outfile}", file=sys.stderr)
    if args.payloads_out:
        args.payloads_out.write_text(
            json.dumps(payloads_dump, indent=2), encoding="utf-8"
        )
        print(
            f"Wrote {len(payloads_dump)} payloads -> {args.payloads_out}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
