"""Read input.csv and record one video per row via qscript.CaptureVideo_Magento.

Usage:
    python capture_video.py [path/to/input.csv] [--server URL] [--view-seconds N]

With --server, every successfully recorded video is uploaded to web_service.py
(video_client.UploadVideo) and previewed in Chromium (video_client.PreviewVideo).
A failed upload or preview is logged and the batch continues with the next row.
"""

import argparse
import csv
import os
import sys

import qscript

CSV_PATH = "input.csv"
CSV_COLUMNS = ["URL", "Company Name", "Language", "Country", "Postcode", "City", "Street", "HouseNo"]
DEFAULT_VIEW_SECONDS = 10.0


def _non_negative_seconds(raw):
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}")
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0 (0 = keep open until Enter)")
    return value


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Record one browser-automation video per CSV row; optionally upload and preview it."
    )
    parser.add_argument("csv_path", nargs="?", default=CSV_PATH,
                        help=f"input CSV (default: {CSV_PATH})")
    parser.add_argument("--server", metavar="URL",
                        help="web service base URL, e.g. http://127.0.0.1:8000; "
                             "enables upload + preview after each video")
    parser.add_argument("--view-seconds", type=_non_negative_seconds, default=DEFAULT_VIEW_SECONDS,
                        metavar="N",
                        help="preview time in seconds; 0 keeps the browser open until Enter "
                             f"(default: {DEFAULT_VIEW_SECONDS:g})")
    return parser.parse_args(argv)


def _upload_and_preview(video_client, server_url, view_seconds, path, company_name):
    """Upload the video and preview it; return True if the upload succeeded."""
    try:
        video_url = video_client.UploadVideo(server_url, path, company_name)
    except Exception as exc:
        print(f"[ERROR] [{company_name}] upload failed: {exc}", flush=True)
        return False
    print(f"[INFO] [{company_name}] uploaded: {video_url}", flush=True)

    try:
        video_client.PreviewVideo(video_url, view_seconds, company_name=company_name)
    except Exception as exc:
        print(f"[ERROR] [{company_name}] preview failed: {exc}", flush=True)
    return True


def main(argv=None):
    args = _parse_args(argv)
    csv_path = args.csv_path
    # The video folder may be overridden here before any row is processed.
    qscript.PATH_TO_VIDEO_FOLDER = "./video"

    video_client = None
    if args.server:
        import video_client  # lazy: requests is needed only with --server

        print(f"[INFO] upload + preview enabled: server={args.server}, "
              f"view_seconds={args.view_seconds:g}", flush=True)

    if not os.path.isfile(csv_path):
        print(f"[ERROR] CSV file not found: {os.path.abspath(csv_path)}", flush=True)
        return 1

    total = 0
    saved = 0
    uploaded = 0
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
            company_name = values.get("Company Name", "")
            path = qscript.CaptureVideo_Magento(
                url=values.get("URL", ""),
                company_name=company_name,
                language=values.get("Language", ""),
                country=values.get("Country", ""),
                postcode=values.get("Postcode", ""),
                city=values.get("City", ""),
                street=values.get("Street", ""),
                house_no=values.get("HouseNo", ""),
            )
            if not path:
                print(f"FAILED: {company_name}", flush=True)
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

            if video_client is not None and _upload_and_preview(
                video_client, args.server, args.view_seconds, path, company_name
            ):
                uploaded += 1

    summary = f"Done: {saved}/{total} videos saved"
    if video_client is not None:
        summary += f", {uploaded} uploaded"
    print(summary, flush=True)
    return 0 if saved else 1


if __name__ == "__main__":
    sys.exit(main())
