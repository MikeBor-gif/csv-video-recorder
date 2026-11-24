"""Task 4 verification. Default: local CDP only. --live explicitly enables DeepSeek."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--urls", default="urls.txt")
    parser.add_argument("--channel", choices=("chrome", "msedge"))
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", default="magento_results_live.json")
    args = parser.parse_args(argv)
    if not args.live:
        command = [sys.executable, "-m", "pytest",
                   *[str(path.relative_to(ROOT)) for path in sorted((ROOT / "tests").glob("test_magento_cart*.py"))],
                   "-q"]
        env = None
        if args.channel:
            import os
            env = dict(os.environ, MAGENTO_TEST_CHANNEL=args.channel)
        return subprocess.call(command, cwd=ROOT, env=env)
    print("[INFO] LIVE: DeepSeek calls enabled; no automatic paid retries", flush=True)
    command = [sys.executable, str(ROOT / "magento_cart_agent.py"), "--urls", args.urls,
               "--max-steps", str(args.max_steps), "--timeout", str(args.timeout),
               "--output", args.output, "--headless"]
    if args.channel:
        command += ["--channel", args.channel]
    code = subprocess.call(command, cwd=ROOT)
    if code:
        return code
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    rows = json.loads(output.read_text(encoding="utf-8"))
    required = {"url", "platform", "cloudflare", "status", "agent_success", "error", "final_result", "cart", "screenshot"}
    if not all(isinstance(row, dict) and required <= row.keys() for row in rows):
        print("[ERROR] Results JSON does not satisfy the Task 4 contract")
        return 1
    for row in rows:
        print(f"[INFO] {row['url']}: {row['status']} ({row['platform']})")
    successes = sum(row["status"] == "success" for row in rows)
    print(f"[INFO] Completed checkout screenshots: {successes}/{len(rows)}")
    if not successes:
        print("[WARNING] No real Magento checkout verified; this is not full happy-path acceptance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
