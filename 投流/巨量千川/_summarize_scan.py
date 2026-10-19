"""汇总 dry-run 扫描结果 JSON（从文件读，避免管道编码问题）。"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8")

with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)

print("status:", d.get("status"), "| thresholds:", d.get("thresholds"))
for dep_name, dep in d["departments"].items():
    if "error" in dep:
        print(f"\n== {dep_name} == ERROR: {dep['error']}")
        continue
    print(f"\n== {dep_name} == total={dep['total_plans']} qualified={dep['qualified_count']}")
    for p in dep["plans"]:
        mark = "★" if p["qualified"] else " "
        print(
            " %s %-32s creator=%-14s %-5s spent=%-8s roi=%-6s orders=%-4s id=%s"
            % (
                mark,
                p["plan_name"][:30],
                str(p["creator_name"]),
                p["status"],
                p["spent"],
                p["roi"],
                p["orders"],
                p["plan_id"],
            )
        )
