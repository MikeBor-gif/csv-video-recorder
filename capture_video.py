"""Read input.csv and record one video per row via qscript.CaptureVideo_Magento.

Usage: python capture_video.py [path/to/input.csv]
"""

import csv
import sys

import qscript

CSV_PATH = "input.csv"


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else CSV_PATH
    # The video folder may be overridden here before any row is processed.
    qscript.PATH_TO_VIDEO_FOLDER = "./video"

    total = 0
    saved = 0
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f, delimiter=";", quotechar='"'):
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
            if path:
                saved += 1
                print(path, flush=True)
            else:
                print(f"FAILED: {values.get('Company Name', '')}", flush=True)

    print(f"Done: {saved}/{total} videos saved", flush=True)
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
