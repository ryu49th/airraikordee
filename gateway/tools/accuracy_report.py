"""Print the dust-cause accuracy table from the users' "ใช่ / ไม่ใช่" answers (for the report).

Run on the Jetson from gateway/:  python3 -m tools.accuracy_report [path/to/cause_log.db]
"""
import os
import sys

from ai.feedback import FeedbackLog


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        from common.config import load_config, writable_dir
        cfg = load_config()
        path = os.path.join(writable_dir(cfg.get("data_dir", "/var/lib/airraikordee"), "data"), "cause_log.db")
    log = FeedbackLog(path)
    print("source   rated  correct  accuracy")
    for source, (rated, correct) in sorted(log.accuracy().items(), key=lambda kv: kv[0] == "all"):
        pct = "%.0f %%" % (100.0 * correct / rated) if rated else "-"
        print("%-8s %5d  %7d  %8s" % (source, rated, correct, pct))
    wrong = log.mistakes()
    if wrong:
        print("\nwrong answers (shown -> really):")
        for shown, really, n in wrong:
            print("  %s -> %s: %d" % (shown or "?", really or "(not given)", n))


if __name__ == "__main__":
    main()
