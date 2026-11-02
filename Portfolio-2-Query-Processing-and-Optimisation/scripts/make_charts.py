"""
Builds the Portfolio 2 charts and evidence images from results/benchmark_results.json
and the saved plan files.
    python scripts/make_charts.py
"""
import json
import os
import subprocess
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHARTS, PLANS, EVID, RES = (os.path.join(ROOT, d) for d in ("charts", "plans", "evidence", "results"))
RENDER = os.path.join(ROOT, "..", "tools", "render_terminal.py")

# Reference palette (dataviz skill): slot-1 blue highlights the best variant; others recede.
BEST, OTHER, INK, MUTED, GRID = "#2a78d6", "#c9c7c0", "#0b0b0b", "#52514e", "#e6e4de"
plt.rcParams.update({"font.family": "Inter", "font.size": 10, "axes.edgecolor": GRID,
                     "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": INK,
                     "axes.spines.top": False, "axes.spines.right": False})

data = json.load(open(os.path.join(RES, "benchmark_results.json")))
exps = [e for e in data["experiments"] if "variants" in e]


def fmt(ms):
    return f"{ms:,.0f} ms" if ms >= 100 else f"{ms:,.1f} ms" if ms >= 1 else f"{ms:.3f} ms"


# ---- Figure 1: small multiples, one panel per experiment ---------------------------
SHORT = {"E1": "Patient history (Q3)", "E2a": "Surname prefix search (Q2)", "E2b": "Surname infix, case-insensitive",
         "E3": "Doctor's day list (Q4)", "E4": "Critical-result worklist (Q5)", "E5": "Warfarin without recent INR (Q8b)",
         "E6": "Top diagnoses per department (Q6)", "E7": "Join strategy, 590k x 590k rows", "E8": "Status counts per doctor"}
fig, axes = plt.subplots(5, 2, figsize=(13, 17))
for ax in axes.flat[len(exps):]:
    ax.set_visible(False)
for ax, e in zip(axes.flat, exps):
    labels = [v["label"] for v in e["variants"]][::-1]
    vals = [v["median_ms"] for v in e["variants"]][::-1]
    best = min(vals)
    lo = 10 ** __import__("math").floor(__import__("math").log10(best) - 0.3)
    ax.barh(range(len(vals)), [v - lo for v in vals], left=lo, height=0.62,
            color=[BEST if v == best else OTHER for v in vals], edgecolor="white", linewidth=2)
    ax.set_xscale("log")
    ax.set_xlim(lo, max(vals) * 12)
    ax.set_yticks(range(len(vals)), labels, fontsize=9)
    for i, v in enumerate(vals):
        ax.text(v * 1.25, i, fmt(v), va="center", fontsize=9, color=INK)
    ax.set_title(f"{e['id']}  {SHORT.get(e['id'], e['title'])}", loc="left", fontsize=11.5,
                 fontweight="bold", color=INK, pad=20)
    ax.text(0, 1.02, e["technique"], transform=ax.transAxes, fontsize=9, color=MUTED, va="bottom")
    ax.grid(axis="x", color=GRID, linewidth=0.8, which="major")
    ax.set_axisbelow(True)
    ax.tick_params(axis="y", length=0)
    ax.set_xlabel("median execution time, ms (log scale) - lower is better", fontsize=8.5)
fig.suptitle("Wayne's Clinic Baltimore - query optimisation experiments (PostgreSQL 16, ~2.6M rows)",
             x=0.01, y=0.995, ha="left", fontsize=14, fontweight="bold", color=INK)
fig.text(0.01, 0.971, "Median of 7 EXPLAIN ANALYZE runs per variant, warm cache. Blue = fastest variant.",
         fontsize=10, color=MUTED)
fig.tight_layout(rect=(0, 0, 1, 0.97), h_pad=3.2, w_pad=3)
fig.savefig(os.path.join(CHARTS, "fig_benchmark_small_multiples.png"), dpi=170)
plt.close(fig)

# ---- Figure 2: speed-up of the adopted optimisation per experiment ------------------
fig, ax = plt.subplots(figsize=(10, 4.6))
rows = []
for e in exps:
    first, best = e["variants"][0]["median_ms"], min(v["median_ms"] for v in e["variants"])
    if e["id"] == "E7":          # E7 compares the planner against forced plans - not a speed-up
        continue
    rows.append((f"{e['id']} {e['title'].split(' (')[0]}", first / best))
rows.sort(key=lambda r: r[1])
ax.barh([r[0] for r in rows], [r[1] for r in rows], color=BEST, height=0.6, edgecolor="white", linewidth=2)
ax.set_xscale("log")
for i, (_, s) in enumerate(rows):
    ax.text(s * 1.08, i, f"{s:,.1f}x", va="center", fontsize=9.5, color=INK)
ax.set_xlim(0.8, max(r[1] for r in rows) * 3)
ax.set_xlabel("speed-up of best variant vs starting point (log scale)")
ax.grid(axis="x", color=GRID, linewidth=0.8, which="major")
ax.set_axisbelow(True)
ax.tick_params(axis="y", length=0)
ax.set_title("How much faster each query became", loc="left", fontsize=12, fontweight="bold", color=INK)
fig.tight_layout()
fig.savefig(os.path.join(CHARTS, "fig_speedup.png"), dpi=170)
plt.close(fig)

# ---- Figure 3: the price of indexes - storage (E4) and write cost (E9) --------------
e4 = next(e for e in data["experiments"] if e["id"] == "E4")
e9 = next(e for e in data["experiments"] if e["id"] == "E9")["write_cost"]
fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 3.8))
sz = e4["index_sizes"]
items = [("lab_result table", sz["lab_result (table)"]), ("partial B-tree (flag='C')", sz["ix_lab_result_critical"]),
         ("BRIN (resulted_at)", sz["ix_lab_result_resulted_brin"])]
a1.barh([i[0] for i in items][::-1], [i[1] / 1024 for i in items][::-1], color=[OTHER, BEST, OTHER],   # adopted partial index in blue
        height=0.6, edgecolor="white", linewidth=2)
a1.set_xscale("log")
for i, (_, b) in enumerate(items[::-1]):
    a1.text(b / 1024 * 1.15, i, f"{b / 1024:,.0f} KB", va="center", fontsize=9.5, color=INK)
a1.set_xlim(5, sz["lab_result (table)"] / 1024 * 8)
a1.set_xlabel("size on disk, KB (log scale)")
a1.set_title("E4  Index size vs the table it indexes", loc="left", fontsize=11, fontweight="bold", color=INK)
labels = [w["label"] for w in e9][::-1]
vals = [w["median_ms"] for w in e9][::-1]
a2.barh(labels, vals, color=[BEST if v == min(vals) else OTHER for v in vals],
        height=0.6, edgecolor="white", linewidth=2)
for i, w in enumerate(e9[::-1]):
    a2.text(w["median_ms"] * 1.02, i, f"{w['median_ms']:,.0f} ms  ({w['index_bytes'] / 1e6:.1f} MB of index)",
            va="center", fontsize=9.5, color=INK)
a2.set_xlim(0, max(vals) * 2.15)
a2.set_xlabel("time to insert 100,000 appointments (ms)")
a2.set_title("E9  Every index is paid for on each INSERT", loc="left", fontsize=11, fontweight="bold", color=INK)
for a in (a1, a2):
    a.grid(axis="x", color=GRID, linewidth=0.8)
    a.set_axisbelow(True)
    a.tick_params(axis="y", length=0)
fig.tight_layout(w_pad=4)
fig.savefig(os.path.join(CHARTS, "fig_index_costs.png"), dpi=170)
plt.close(fig)
print("charts written")

# ---- Evidence images: before/after execution plans ---------------------------------
def plan_file(exp_id, n):
    return next(f for f in sorted(os.listdir(PLANS)) if f.startswith(f"{exp_id}_{n}_"))


def render_plan(exp_id, n, out, max_lines=42):
    src = os.path.join(PLANS, plan_file(exp_id, n))
    text = open(src).read().split("\n")
    head = [l for l in text[:4] if l.startswith("--")]
    plan_start = next(i for i, l in enumerate(text) if "QUERY PLAN" in l or l.lstrip().startswith(("Sort", "Limit", "Hash", "Nested", "Aggregate", "Finalize", "GroupAggregate", "Index", "Seq", "Bitmap", "Gather", "Merge", "Subquery", "WindowAgg", "Result", "Unique", "Incremental", "HashAggregate")))
    body = head + [""] + text[plan_start:]
    if len(body) > max_lines:
        body = body[:max_lines - 4] + ["   ..."] + [l for l in body if "Planning Time" in l or "Execution Time" in l]
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(), "plans_" + plan_file(exp_id, n))   # footer names the real plan file
    open(tmp, "w").write("\n".join(body))
    subprocess.run([sys.executable, RENDER, tmp, os.path.join(EVID, out),
                    "--title", f"psql - EXPLAIN (ANALYZE, BUFFERS) - {plan_file(exp_id, n)}", "--width", "150"],
                   check=True, stdout=subprocess.DEVNULL)
    os.remove(tmp)


for exp_id, n, out in [("E1", 1, "fig_plan_E1_before.png"), ("E1", 2, "fig_plan_E1_after.png"),
                       ("E3", 1, "fig_plan_E3_nonsargable.png"), ("E3", 4, "fig_plan_E3_sargable_index.png"),
                       ("E4", 1, "fig_plan_E4_seqscan.png"), ("E4", 3, "fig_plan_E4_partial_index.png"),
                       ("E5", 1, "fig_plan_E5_correlated.png"), ("E5", 3, "fig_plan_E5_not_exists.png"),
                       ("E8", 2, "fig_plan_E8_index_only.png")]:
    render_plan(exp_id, n, out)

qr = os.path.join(RES, "02_query_results.txt")
for start, end, out, title in [("Q3 ", "Q4 ", "fig_result_Q3_history.png", "Q3 patient history"),
                               ("Q6 ", "Q7 ", "fig_result_Q6_top_diagnoses.png", "Q6 top diagnoses per department"),
                               ("Q8 ", "Q9 ", "fig_result_Q8_warfarin.png", "Q8 warfarin without INR")]:
    subprocess.run([sys.executable, RENDER, qr, os.path.join(EVID, out), "--title", f"psql - waynes_clinic - {title}",
                    "--start", start, "--end", end, "--width", "150"], check=True, stdout=subprocess.DEVNULL)
print("evidence images written")
