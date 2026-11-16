"""Company CSV/API smoke test; --live explicitly enables bounded paid enrichment."""

import argparse
import csv
import io
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
EXTRACTED = ("status", "impressum_url", "legal_name", "postal_address", "handelsregister",
             "vat_number", "phone", "email", "responsible_person", "raw_json")


def check_import(session, base, content, expected_rows):
    response = session.post(base + "/upload_companies", files={"file": ("companies.csv", content)}, timeout=30)
    response.raise_for_status()
    counts = response.json()
    assert counts["total_rows"] == expected_rows
    assert counts["imported"] + counts["updated"] == expected_rows
    print("[PASS] import", counts)
    return counts


def read_company(session, base, name):
    response = session.get(base + "/companies/" + quote(name, safe=""), timeout=30)
    response.raise_for_status()
    return response.json()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--live", action="store_true", help="allow up to 2 API attempts for each sample company")
    args = parser.parse_args(argv)
    base = args.server.rstrip("/")
    if args.live and base not in ("http://127.0.0.1:8000", "http://localhost:8000"):
        parser.error("--live uses the local Docker stack on port 8000")
    content = (ROOT / "companies.csv").read_bytes()
    names = [r["Company Name"] for r in csv.DictReader(io.StringIO(content.decode("utf-8-sig")), delimiter=";")]
    try:
        with requests.Session() as session:
            form = session.get(base + "/upload_companies", timeout=30)
            form.raise_for_status()
            assert 'multipart/form-data' in form.text and 'name="file"' in form.text
            print("[PASS] upload form")
            check_import(session, base, content, len(names))
            before = {name: read_company(session, base, name) for name in names}
            counts = check_import(session, base, content, len(names))
            assert counts["imported"] == 0 and counts["updated"] == len(names)
            for name in names:
                after = read_company(session, base, name)
                assert after["id"] == before[name]["id"]
                assert all(after[k] == before[name][k] for k in EXTRACTED)
            print("[PASS] re-import preserves enrichment and IDs")
            listing = session.get(base + "/companies", timeout=30)
            listing.raise_for_status()
            assert isinstance(listing.json(), list)
            if args.live:
                for name in names:
                    # Never force a repeat of an already successful extraction.
                    result = subprocess.run(["docker", "compose", "exec", "-T", "web", "python",
                                             "enrich_companies.py", "--company", name, "--limit", "1"],
                                            cwd=str(ROOT), timeout=180)
                    print("[LIVE]", name, "exit=", result.returncode)
                    if result.returncode:
                        print("[FIX:verify] enrichment failed; stopping live check without additional attempts")
                        result.check_returncode()
                rows = [read_company(session, base, name) for name in names]
                assert all(row["status"] in (0, 2, 3) and row["impressum_url"] for row in rows), \
                    "live extraction incomplete; inspect statuses without automatically retrying"
                print("[PASS] live extraction statuses", [(r["company_name"], r["status"]) for r in rows])
        print("ALL COMPANY CHECKS PASSED")
        return 0
    except (AssertionError, requests.RequestException, subprocess.SubprocessError) as exc:
        print("[FAIL]", type(exc).__name__, str(exc) if isinstance(exc, AssertionError) else "request/command failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
