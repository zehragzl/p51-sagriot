"""Edge vs cloud decisions, from the gateway database.

For every cycle (node, seq) the node sends a reading and its own decision;
the gateway forwards the reading to the cloud and logs the cloud's
decision. seq restarts at 0 when the node reboots, so messages are paired
by seq AND arrival time (edge within 10 s, cloud within 60 s of the reading). This script pairs the two decisions of each cycle and reports
  - how many cycles have both, only edge, only cloud
  - agreement on `warn` and `now`
  - gateway round trip: reading arrived -> cloud_decision arrived (ms)

Usage
  python gateway/compare.py                     # data/gateway.db
  python gateway/compare.py --csv data/compare.csv
  python gateway/compare.py --minutes 30         # only the last 30 minutes
"""
import argparse
import csv
import sqlite3
import statistics
import time

QUERY = """
SELECT r.node, r.seq, r.received_ms,
       e.warn, e.t_cross_min, e.vwc_pred_3h,
       c.model, c.warn, c.t_cross_min, c.vwc_pred_3h, c.received_ms
FROM readings r
LEFT JOIN decisions e ON e.source = 'edge'  AND e.node = r.node AND e.seq = r.seq
     AND e.received_ms BETWEEN r.received_ms - 10000 AND r.received_ms + 10000
LEFT JOIN decisions c ON c.source = 'cloud' AND c.node = r.node AND c.seq = r.seq
     AND c.received_ms BETWEEN r.received_ms AND r.received_ms + 60000
WHERE r.received_ms >= ?
ORDER BY r.received_ms
"""
COLUMNS = ["node", "seq", "reading_ms", "edge_warn", "edge_t_cross", "edge_pred",
           "cloud_model", "cloud_warn", "cloud_t_cross", "cloud_pred", "cloud_ms"]


def main():
    parser = argparse.ArgumentParser(description="SAgrIoT edge vs cloud comparison")
    parser.add_argument("--db", default="data/gateway.db")
    parser.add_argument("--csv", default=None, help="also write every paired cycle")
    parser.add_argument("--minutes", type=float, default=None,
                        help="only readings from the last N minutes")
    args = parser.parse_args()

    since = 0
    if args.minutes:
        since = int((time.time() - args.minutes * 60) * 1000)
    rows = sqlite3.connect(args.db).execute(QUERY, (since,)).fetchall()
    both = [r for r in rows if r[3] is not None and r[7] is not None]
    only_edge = sum(1 for r in rows if r[3] is not None and r[7] is None)
    only_cloud = sum(1 for r in rows if r[3] is None and r[7] is not None)

    print(f"readings: {len(rows)}  |  both decisions: {len(both)}  |  "
          f"edge only: {only_edge}  |  cloud only: {only_cloud}")
    if both:
        agree = sum(1 for r in both if bool(r[3]) == bool(r[7]))
        print(f"warn agreement: {agree}/{len(both)} ({100 * agree / len(both):.1f} %)"
              f"  [cloud model: {', '.join(sorted({r[6] for r in both}))}]")
        rtt = [r[10] - r[2] for r in both if r[10] is not None]
        if rtt:
            print(f"gateway round trip (reading -> cloud_decision): "
                  f"median {statistics.median(rtt):.0f} ms, min {min(rtt)}, max {max(rtt)}")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(COLUMNS)
            w.writerows(rows)
        print(f"written: {args.csv}")


if __name__ == "__main__":
    main()
