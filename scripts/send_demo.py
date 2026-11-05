"""Send reproducible simulated scenarios to the running backend.

python scripts/send_demo.py --scenario abnormal
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--device", default="guardian_001")
    parser.add_argument("--scenario", choices=("normal", "abnormal", "poor-signal", "motion"), default="abnormal")
    args = parser.parse_args()

    # 4 normal, 4 abnormal, 4 recovered samples: a repeatable complete event lifecycle.
    readings = {
        "normal": [(75, 98, 1.0, "good")] * 12,
        "abnormal": [(75, 98, 1.0, "good")] * 4 + [(130, 91, 1.0, "good")] * 4 + [(76, 98, 1.0, "good")] * 4,
        "poor-signal": [(75, 98, 1.0, "good")] * 4 + [(130, 91, 1.0, "poor")] * 4 + [(76, 98, 1.0, "good")] * 4,
        "motion": [(75, 98, 1.0, "good")] * 4 + [(125, 98, 3.0, "good")] * 4 + [(76, 98, 1.0, "good")] * 4,
    }[args.scenario]
    start = datetime.now(timezone.utc)
    for index, (hr, spo2, acc, quality) in enumerate(readings):
        payload = {
            "device_id": args.device, "timestamp": (start + timedelta(milliseconds=index * 100)).isoformat(),
            "source": "simulator", "heart_rate": hr, "spo2": spo2,
            "acc_x": 0.0, "acc_y": 0.0, "acc_z": acc,
            "signal_quality": quality, "reading_valid": quality == "good",
        }
        req = Request(args.url.rstrip("/") + "/v1/telemetry", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=10) as response:
            result = json.load(response)
        print(f"{index+1:02d} hr={hr} spo2={spo2} quality={quality} -> {result['signals']} {result['transitions']}")
        time.sleep(0.12)
    print("See /docs and /v1/events?device_id=" + args.device)


if __name__ == "__main__":
    main()
