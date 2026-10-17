"""Delivery report from the gateway database (success criterion 2).

For each node and message kind, counts the cycles that arrived and the
seq gaps between them, so the share of cycles that reached the gateway
can be read directly.

Usage
  python tools/gateway_stats.py               # data/gateway.db
  python tools/gateway_stats.py --db other.db
"""
import argparse
import sqlite3


def report(db, table, where=""):
    rows = db.execute(f"SELECT node, seq FROM {table} {where} ORDER BY node, seq").fetchall()
    by_node = {}
    for node, seq in rows:
        if seq is not None:
            by_node.setdefault(node, []).append(seq)
    for node, seqs in by_node.items():
        seqs = sorted(set(seqs))
        expected = seqs[-1] - seqs[0] + 1
        missing = sorted(set(range(seqs[0], seqs[-1] + 1)) - set(seqs))
        share = 100 * len(seqs) / expected
        print(f"  {node}: {len(seqs)}/{expected} cycles arrived ({share:.1f} %), "
              f"missing seq: {missing[:15]}{' ...' if len(missing) > 15 else ''}")


def main():
    parser = argparse.ArgumentParser(description="SAgrIoT gateway delivery report")
    parser.add_argument("--db", default="data/gateway.db")
    args = parser.parse_args()
    db = sqlite3.connect(args.db)
    print("readings (cloud mode):")
    report(db, "readings")
    print("edge decisions:")
    report(db, "decisions", "WHERE source = 'edge'")


if __name__ == "__main__":
    main()
