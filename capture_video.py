"""Read input.csv and record one video per row via qscript.CaptureVideo_Magento.

Usage: python capture_video.py [path/to/input.csv]
"""

import csv
import os
import sys

import qscript

CSV_PATH = "input.csv"
CSV_COLUMNS = ["URL", "Company Name", "Language", "Country", "Postcode", "City", "Street", "HouseNo"]


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else CSV_PATH
    # The video folder may be overridden here before any row is processed.
    qscript.PATH_TO_VIDEO_FOLDER = "./video"

    if not os.path.isfile(csv_path):
        print(f"[ERROR] CSV file not found: {os.path.abspath(csv_path)}", flush=True)
        return 1

    total = 0
    saved = 0
    seen_paths = {}
    # utf-8-sig: still UTF-8, but tolerates the BOM that Excel writes ("CSV UTF-8").
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        missing = [column for column in CSV_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            print(f"[ERROR] CSV header must be: {';'.join(CSV_COLUMNS)}", flush=True)
            print(f"[ERROR] missing columns: {', '.join(missing)}; got: {reader.fieldnames}", flush=True)
            return 1

        for row in reader:
            total += 1
            values = {key: (value or "").strip() for key, value in row.items() if key}
            path = qscript.CaptureVideo_Magento(
                url=values.get("URL", ""),
                company_name=values.get("Company Name", ""),
                language=values.get("Language", ""),
                country=values.get("Country", ""),
                postcode=values.get("Postcode", ""),
                city=values.get("City", ""),
                street=values.get("Street", ""),
                house_no=values.get("HouseNo", ""),
            )
            if not path:
                print(f"FAILED: {values.get('Company Name', '')}", flush=True)
                continue
            saved += 1
            print(path, flush=True)
            key = os.path.normcase(path)
            if key in seen_paths:
                print(
                    f"[WARN] row {total} overwrote the video of row {seen_paths[key]}: "
                    f"same Company Name -> {path}",
                    flush=True,
                )
            seen_paths[key] = total

    print(f"Done: {saved}/{total} videos saved", flush=True)
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
