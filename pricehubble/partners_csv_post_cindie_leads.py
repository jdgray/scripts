#!/usr/bin/env python3
"""
Read Partners export CSV, POST each row to the CiNDIE / Low Rate webhook-leads API.

CSV columns expected (export-9.30 style):
  Name, Street, Site, dossier_id, cell (or Borr Cell)

Usage:
  python3 partners_csv_post_cindie_leads.py --csv export-9.30.csv --dry-run
  python3 partners_csv_post_cindie_leads.py --csv export-9.30.csv --limit 3
  python3 partners_csv_post_cindie_leads.py export-9.30.csv --outfile results.tsv
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_WEBHOOK = (
    "https://low-rate-co-ai-api-rdqtaf7sdq-uc.a.run.app"
    "/api/pos/webhook-leads/cwh_XPL0iUo3xHKz_sBWeG1Agg"
)
DEFAULT_SECRET = os.environ.get(
    "CINDIE_WEBHOOK_SECRET",
    "cwhsec_vCvGtuClSfRp3Ka-gWBh2SOZmVyztSL6kjCJXj0JUDE",
)
DEFAULT_CSV = Path(
    "/Users/jonathongray/Development/personal/scripts/pricehubble/data/export-9.30.csv"
)

INTRO_TEMPLATE = (
    "Hi {first_name}, it’s CiNDIE. I work with Joel Morgan at Partners Mortgage, "
    "and since we’ve helped with your mortgage in the past, we wanted to give you "
    "an ongoing resource for your home.\n"
    "Your personal property page helps you keep up with your home, neighborhood, "
    "equity and other things worth watching over time. There’s no cost or login required.\n"
    "Start by reviewing your home details and updating anything that’s changed. "
    "Then explore your neighborhood and ask me, "
    "“How does my neighborhood compare to the surrounding market?”\n"
    "Your page: {microsite_url}"
)


def clean(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in clean(full).split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def phone_digits(v: Any) -> str:
    return re.sub(r"\D", "", clean(v))


def phone_e164(v: Any) -> Optional[str]:
    digits = phone_digits(v)
    if not digits:
        return None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if clean(v).startswith("+") and digits:
        return "+" + digits
    return "+" + digits


def strip_dossier_id(v: Any) -> str:
    s = clean(v)
    s = re.sub(r"^urn:phi:dossiers?:", "", s, flags=re.I)
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        s,
        flags=re.I,
    )
    return m.group(0) if m else s


def row_get(row: dict, *names: str) -> str:
    lower = {str(k).lower().strip(): k for k in row.keys()}
    for name in names:
        key = lower.get(name.lower())
        if key is not None:
            return clean(row.get(key))
    return ""


def build_payload(row: dict) -> tuple[Optional[dict], str]:
    name = row_get(row, "Name", "name")
    first, last = split_name(name)
    if not first:
        first = row_get(row, "first_name", "Borrower First Name", "First Name")
        last = row_get(row, "last_name", "Borrower Last Name", "Last Name")

    address = row_get(row, "Street", "street", "Address", "property_address")
    microsite = row_get(row, "Site", "site", "microsite_url", "micrositeUrl", "url")
    dossier_id = strip_dossier_id(
        row_get(row, "dossier_id", "dossierId", "Dossier Id", "dossier_urn", "dossierUrn")
    )
    cell_raw = row_get(row, "cell", "Borr Cell", "phone", "Phone", "Borr Cell")
    phone = phone_e164(cell_raw)
    cell_for_id = phone_digits(cell_raw)
    if phone and phone.startswith("+"):
        # lead_id uses digits without '+'; prefer E.164 digits (with leading 1)
        cell_for_id = phone_digits(phone)

    if not dossier_id:
        return None, "missing dossier_id"
    if not phone or not cell_for_id:
        return None, "missing cell/phone"
    if not microsite:
        return None, "missing microsite url"
    if not first:
        return None, "missing first_name"

    payload = {
        "lead_id": f"{dossier_id}-{cell_for_id}",
        "first_name": first,
        "last_name": last,
        "phone": phone,
        "has_consent": True,
        "intro_message_override": INTRO_TEMPLATE.format(
            first_name=first,
            microsite_url=microsite,
        ),
        "data": {
            "dossier_id": dossier_id,
            "microsite_url": microsite,
            "property_address": address,
        },
    }
    return payload, "ok"


def post_json(url: str, payload: dict, secret: str, timeout: float = 60.0) -> tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Cindie-Webhook-Secret": secret,
            "User-Agent": "partners-csv-post-cindie-leads/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(body) if body else {}
            except json.JSONDecodeError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return e.code, parsed
    except Exception as e:
        return 0, {"error": str(e)}


def tsv_escape(s: str) -> str:
    return (s or "").replace("\t", " ").replace("\n", " ").replace("\r", "")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "csv_path",
        nargs="?",
        type=Path,
        default=None,
        help="Export CSV path (positional; same as --csv)",
    )
    ap.add_argument(
        "--csv",
        dest="csv_opt",
        type=Path,
        default=None,
        help=f"Export CSV path (default: {DEFAULT_CSV})",
    )
    ap.add_argument("--webhook", default=DEFAULT_WEBHOOK, help="CiNDIE webhook URL")
    ap.add_argument(
        "--secret",
        default=DEFAULT_SECRET,
        help="X-Cindie-Webhook-Secret (or env CINDIE_WEBHOOK_SECRET)",
    )
    ap.add_argument("--dry-run", action="store_true", help="Build payloads but do not POST")
    ap.add_argument("--limit", type=int, default=0, help="Max rows to process (0 = all)")
    ap.add_argument("--sleep", type=float, default=0.35, help="Delay between POSTs")
    ap.add_argument("--outfile", type=Path, default=None, help="Write results TSV here")
    ap.add_argument(
        "--dump-json",
        type=Path,
        default=None,
        help="Also write each payload as JSON lines (.jsonl)",
    )
    args = ap.parse_args()
    csv_path = args.csv_opt or args.csv_path or DEFAULT_CSV

    if not csv_path.is_file():
        print(f"File not found: {csv_path}", file=sys.stderr)
        return 1
    if not args.secret:
        print("Missing webhook secret (--secret or CINDIE_WEBHOOK_SECRET)", file=sys.stderr)
        return 1

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    out_lines: list[str] = []
    header = "Name\tPhone\tLeadId\tDossierId\tSite\tHttpStatus\tResult\tNotes"
    out_lines.append(header)
    print(header)

    jsonl_f = args.dump_json.open("w", encoding="utf-8") if args.dump_json else None

    ok = 0
    skip = 0
    fail = 0

    try:
        for i, row in enumerate(rows, 1):
            payload, status = build_payload(row)
            name = row_get(row, "Name", "name") or (
                f"{payload.get('first_name','')} {payload.get('last_name','')}".strip()
                if payload
                else ""
            )

            if not payload:
                skip += 1
                line = "\t".join(
                    [
                        tsv_escape(name),
                        "",
                        "",
                        tsv_escape(row_get(row, "dossier_id")),
                        tsv_escape(row_get(row, "Site", "site")),
                        "",
                        "skipped",
                        tsv_escape(status),
                    ]
                )
                out_lines.append(line)
                print(line)
                continue

            if jsonl_f is not None:
                jsonl_f.write(json.dumps(payload, ensure_ascii=False) + "\n")

            if args.dry_run:
                ok += 1
                line = "\t".join(
                    [
                        tsv_escape(name),
                        tsv_escape(payload["phone"]),
                        tsv_escape(payload["lead_id"]),
                        tsv_escape(payload["data"]["dossier_id"]),
                        tsv_escape(payload["data"]["microsite_url"]),
                        "",
                        "dry_run",
                        "",
                    ]
                )
                out_lines.append(line)
                print(line)
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                print()
                continue

            http_status, resp = post_json(args.webhook, payload, args.secret)
            note = ""
            result = "ok"
            if isinstance(resp, dict):
                note = str(resp.get("error") or resp.get("message") or "")[:200]
                if resp.get("error") or not (200 <= http_status < 300):
                    result = "error"
            if not (200 <= http_status < 300):
                result = "error"
                fail += 1
            else:
                ok += 1

            line = "\t".join(
                [
                    tsv_escape(name),
                    tsv_escape(payload["phone"]),
                    tsv_escape(payload["lead_id"]),
                    tsv_escape(payload["data"]["dossier_id"]),
                    tsv_escape(payload["data"]["microsite_url"]),
                    str(http_status),
                    result,
                    tsv_escape(note),
                ]
            )
            out_lines.append(line)
            print(line)

            if args.sleep > 0 and i < len(rows):
                time.sleep(args.sleep)
    finally:
        if jsonl_f is not None:
            jsonl_f.close()

    if args.outfile:
        args.outfile.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        print(f"Wrote {args.outfile}", file=sys.stderr)

    print(f"done ok={ok} skip={skip} fail={fail} total={len(rows)}", file=sys.stderr)
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
