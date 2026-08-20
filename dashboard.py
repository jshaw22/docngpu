#!/usr/bin/env python3
"""
DigitalOcean GPU availability dashboard.

Reads availability.db (written by gpu_monitor.py) and visualizes which regions
have which GPUs available over time.

Two tabs:
  - Overview: all GPUs on one page (summary + timeline).
  - Per-GPU detail: drill into one size's regions.

Run:
    ./.venv/bin/streamlit run dashboard.py
"""

import os

import pandas as pd
import plotly.express as px
import streamlit as st

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "availability.csv")

# Visualization ceiling: treat 5+ available regions as "fully available" so the
# common low counts (0-3) stay visually distinct instead of washing out.
SCALE_CAP = 5

# Red is reserved for total outages: a poll where NOTHING in the chart was
# available (vs. blank/transparent = poll failed, and white/dark = just this
# row sold out). Zeros in those columns are replaced with the OUTAGE sentinel
# so a colorscale breakpoint can catch them without touching ordinary zeros.
OUTAGE_RED = "#d13438"
OUTAGE = -1

# Greens ramp over [-1, SCALE_CAP]: -1 → red, 0 → white, 1+ → light-to-dark
# green. Values are whole counts, so breakpoints at ±0.5 never split a value.
_SPAN = SCALE_CAP - OUTAGE
OUTAGE_GREENS = [
    (0.0, OUTAGE_RED), (0.5 / _SPAN, OUTAGE_RED),
    (0.5 / _SPAN, "#f7fcf5"), (1.5 / _SPAN, "#f7fcf5"),
    (2.0 / _SPAN, "#c7e9c0"), (1.0, "#00441b"),
]

# Same idea for the binary region timeline: -1 → red, 0 → dark, 1 → green.
OUTAGE_BINARY = [
    (0.0, OUTAGE_RED), (0.25, OUTAGE_RED),
    (0.25, "#2b2b3b"), (0.75, "#2b2b3b"),
    (0.75, "#21c45d"), (1.0, "#21c45d"),
]


def mark_total_outages(pivot):
    """Replace 0 with the OUTAGE sentinel in columns whose maximum is 0 —
    i.e. polls where nothing in this chart was available at all."""
    dead = pivot.max(axis=0) == 0
    out = pivot.copy()
    out.loc[:, dead] = out.loc[:, dead].replace(0, OUTAGE)
    return out

st.set_page_config(page_title="DO GPU Availability", layout="wide")


@st.cache_data(ttl=60)
def load():
    """Return (data_df, failed_ts): real poll rows, and local timestamps of
    failed polls (NO_DATA sentinel rows written when e.g. the cookie expired)."""
    empty = pd.DataFrame(), pd.DatetimeIndex([])
    if not os.path.exists(CSV_PATH):
        return empty
    df = pd.read_csv(CSV_PATH)
    if df.empty:
        return empty
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    # Show times in Pacific (handles PST/PDT automatically).
    df["ts_local"] = df["ts"].dt.tz_convert("America/Los_Angeles")
    df["hour"] = df["ts_local"].dt.hour
    df["date"] = df["ts_local"].dt.date
    # Split off the failed-poll markers before any per-size processing.
    failed = df["size_name"] == "NO_DATA"
    failed_ts = pd.DatetimeIndex(df.loc[failed, "ts_local"].unique()).sort_values()
    df = df[~failed].copy()
    # NO_DATA rows have empty numeric fields, which makes pandas read these
    # columns as float; restore ints so labels don't render as "x8.0".
    if not df.empty:
        df["gpu_count"] = df["gpu_count"].astype(int)
        df["available"] = df["available"].astype(int)
    # Keep only regions where some GPU has ever been available during our
    # polling; the rest are permanent zeros that just add noise. Derived from
    # the data (not hardcoded) so when DO lights up a new GPU region — e.g.
    # mkc1/mem1 for the B300/MI355X spot SKUs — it appears automatically.
    gpu_regions = df.loc[df["available"] == 1, "region_slug"].unique()
    df = df[df["region_slug"].isin(gpu_regions)].copy()
    # Friendly label per size, e.g. "H100 x8". Include the SKU variant suffix
    # ("spot", "lc-spot", ...) — B300 has spot and lc-spot SKUs with the same
    # model+count, which would otherwise collapse into one row.
    variant = df["size_name"].str.extract(r"\dgb-(.+)$", expand=False)
    df["gpu_label"] = (
        df["gpu_model"].str.replace("nvidia_", "", regex=False)
        .str.replace("amd_", "", regex=False)
        .str.upper()
        + " x" + df["gpu_count"].astype(str)
        + variant.map(lambda v: "" if pd.isna(v) else f" ({v})")
    )
    return df, failed_ts


def collapse_no_data_gaps(pivot, failed_ts):
    """Re-key the pivot to one equal-width column per poll (categorical axis),
    collapsing each stretch of consecutive failed polls into a single all-NaN
    column. Outages stay visible as an explicit blank gap, but a 14-hour one
    no longer sprawls across the chart; the label carries the real span."""
    events = sorted(
        [(ts, ts) for ts in pivot.columns] + [(ts, None) for ts in failed_ts]
    )
    cols = {}
    i = 0
    while i < len(events):
        ts, src = events[i]
        if src is not None:
            label = f"{ts:%m-%d %H:%M}"
            column = pivot[src]
            i += 1
        else:
            j = i
            while j < len(events) and events[j][1] is None:
                j += 1
            start, end = events[i][0], events[j - 1][0]
            if j - i == 1:
                label = f"✕ missed {start:%m-%d %H:%M}"
            else:
                end_fmt = "%H:%M" if end.date() == start.date() else "%m-%d %H:%M"
                label = f"✕ {j - i} missed {start:%m-%d %H:%M} → {end:{end_fmt}}"
            column = pd.Series(float("nan"), index=pivot.index)
            i = j
        while label in cols:  # e.g. two polls in the same minute
            label += " "
        cols[label] = column
    return pd.DataFrame(cols, index=pivot.index)


def day_ticks(columns):
    """One tick per calendar day — a categorical axis would otherwise label
    every poll column. Gap columns never get a tick (hover still has them)."""
    tickvals, seen = [], set()
    for lbl in columns:
        if lbl.startswith("✕") or lbl[:5] in seen:
            continue
        seen.add(lbl[:5])
        tickvals.append(lbl)
    return tickvals, [lbl[:5] for lbl in tickvals]


def gpu_family(label):
    """Coarse family for grouping related rows: RTX4000/RTX6000 → RTX,
    MI300X/MI325X/MI350X/MI355X → MI3xx, else the base model (B300, H100, ...)."""
    base = label.split(" x")[0]
    if base.startswith("RTX"):
        return "RTX"
    if base.startswith("MI3"):
        return "MI3xx"
    return base


def family_grouped_order(scores):
    """Row order that keeps GPU families together — and, within a family, each
    base model together (MI350X x1/x8 stay adjacent) — while still leading
    with availability at every level: families ranked by their best member's
    score (ties broken by family total, then name), models within a family the
    same way, and members within a model by score then label.
    `scores` is a Series indexed by gpu_label; returns labels best-first."""
    fams = scores.index.map(gpu_family)
    models = scores.index.map(lambda l: l.split(" x")[0])
    fam_max, fam_sum = scores.groupby(fams).max(), scores.groupby(fams).sum()
    mod_max, mod_sum = scores.groupby(models).max(), scores.groupby(models).sum()

    def key(label):
        fam, model = gpu_family(label), label.split(" x")[0]
        return (
            -fam_max[fam], -fam_sum[fam], fam,
            -mod_max[model], -mod_sum[model], model,
            -scores[label], label,
        )

    return sorted(scores.index, key=key)


df, failed_ts = load()

st.title("🖥️  DigitalOcean GPU Droplet Availability")

if df.empty:
    if len(failed_ts):
        st.error(
            f"All {len(failed_ts)} poll(s) so far failed (expired cookie?). "
            "Refresh the cookie in secrets.env and run ./refresh_cookie.sh."
        )
    else:
        st.warning("No data yet. Run `python3 gpu_monitor.py` first to log a poll.")
    st.stop()

# If polls have failed since the last good one, the "now" numbers are stale.
last_good = df["ts_local"].max()
stale_fails = failed_ts[failed_ts > last_good]
if len(stale_fails):
    st.warning(
        f"⚠️ The last {len(stale_fails)} poll(s) failed — cookie has likely "
        f"expired. Data below is as of {last_good:%Y-%m-%d %H:%M} PT. "
        "Refresh the cookie in secrets.env and run ./refresh_cookie.sh."
    )

n_polls = df["ts"].nunique()
n_regions = df["region_slug"].nunique()
failed_note = f" · {len(failed_ts)} failed poll(s)" if len(failed_ts) else ""
st.caption(
    f"{n_polls} poll(s) · {df['ts_local'].min():%Y-%m-%d %H:%M} → "
    f"{df['ts_local'].max():%Y-%m-%d %H:%M} PT · {n_regions} GPU regions"
    f"{failed_note}"
)

overview_tab, detail_tab = st.tabs(["📊 Overview — all GPUs", "🔍 Per-GPU detail"])

# =========================================================================
# OVERVIEW TAB — every GPU on one page
# =========================================================================
with overview_tab:
    latest_ts = df["ts"].max()
    now = df[df["ts"] == latest_ts]

    # Per GPU: how many regions available right now (+ which ones).
    snap = (
        now.groupby("gpu_label")["available"].sum()
        .reset_index(name="regions_now")
    )
    avail_regions = (
        now[now["available"] == 1]
        .groupby("gpu_label")["region_slug"]
        .apply(lambda s: ", ".join(sorted(s)))
    )
    snap["available_in"] = snap["gpu_label"].map(avail_regions).fillna("")
    # Group families together (B300s, RTXs, MI3xx, ...) while still leading
    # with availability: available families first, ranked by their best member.
    now_order = family_grouped_order(snap.set_index("gpu_label")["regions_now"])
    snap = snap.set_index("gpu_label").loc[now_order].reset_index()

    n_types_avail = int((snap["regions_now"] > 0).sum())
    total_combos = int(now["available"].sum())

    c1, c2 = st.columns(2)
    c1.metric("GPU types available now", f"{n_types_avail} / {snap.shape[0]}")
    c2.metric("Total GPU+region combos available", total_combos)

    # Glanceable bar: regions available now, per GPU.
    st.subheader("Available right now")
    fig_now = px.bar(
        snap, x="regions_now", y="gpu_label", orientation="h",
        text="available_in",
        labels=dict(regions_now="# regions available", gpu_label="GPU"),
        range_x=[0, SCALE_CAP],
    )
    # Region names start inside the green bar; constraintext="none" lets a long
    # list overflow past a short bar instead of being shrunk to nothing.
    fig_now.update_traces(
        marker_color="#21c45d", textposition="inside",
        insidetextanchor="start", constraintext="none", textfont_color="black",
    )
    # Plotly draws the first y category at the bottom; reverse so the best
    # family block sits on top.
    fig_now.update_yaxes(categoryorder="array",
                         categoryarray=list(reversed(now_order)))
    st.plotly_chart(fig_now, use_container_width=True)

    # Total combos over time: one point per poll, gaps where polls failed so
    # an outage reads as a hole in the line rather than a flat interpolation.
    st.subheader("Total GPU+region combos over time")
    totals = df.groupby("ts_local")["available"].sum()
    totals = pd.concat(
        [totals, pd.Series(float("nan"), index=failed_ts)]
    ).sort_index()
    fig_total = px.line(
        x=totals.index, y=totals.values,
        labels=dict(x="Time (PT)", y="# combos available"),
    )
    fig_total.update_traces(
        line_color="#21c45d", line_width=2,
        hovertemplate="Time=%{x|%m-%d %H:%M}<br># combos=%{y}<extra></extra>",
    )
    fig_total.update_yaxes(rangemode="tozero")
    st.plotly_chart(fig_total, use_container_width=True)
    st.caption(
        "Sum of every GPU size × region pair available at each poll — the "
        "same number as the metric above, tracked over time. Line breaks = "
        "failed polls."
    )

    # Timeline heatmap: GPU (rows) x time (cols), color = # regions available.
    st.subheader("Availability over time — all GPUs")
    grid = (
        df.groupby(["gpu_label", "ts_local"])["available"].sum().reset_index()
    )
    pivot = grid.pivot(index="gpu_label", columns="ts_local", values="available")
    # Same family-grouped ordering as the bar chart, scored by all-time
    # availability so the rows line up conceptually.
    pivot = pivot.loc[family_grouped_order(pivot.sum(axis=1))]
    pivot = collapse_no_data_gaps(pivot, failed_ts)
    fig_time = px.imshow(
        mark_total_outages(pivot),
        color_continuous_scale=OUTAGE_GREENS, zmin=OUTAGE, zmax=SCALE_CAP,
        aspect="auto",
        labels=dict(x="Time (PT)", y="GPU", color="# regions"),
    )
    tickvals, ticktext = day_ticks(pivot.columns)
    fig_time.update_xaxes(side="top", tickvals=tickvals, ticktext=ticktext,
                          tickangle=45)
    # Hover shows the real count (0), not the -1 sentinel; keep the sentinel
    # off the colorbar ticks too.
    fig_time.update_traces(
        customdata=pivot.values,
        hovertemplate="Time=%{x}<br>GPU=%{y}<br># regions=%{customdata}"
                      "<extra></extra>",
    )
    fig_time.update_coloraxes(colorbar_tickvals=list(range(SCALE_CAP + 1)))
    st.plotly_chart(fig_time, use_container_width=True)
    st.caption(
        f"Each cell = how many of the {n_regions} GPU regions had that GPU "
        f"available at that poll (color capped at {SCALE_CAP}+). Greener = more "
        "widely available; white = that GPU sold out; red column = nothing "
        "available anywhere. Blank ✕ columns = failed polls, collapsed to one "
        "column per outage however long it ran (hover for the span). Fills "
        "in hourly."
    )

# =========================================================================
# DETAIL TAB — one GPU, region-level
# =========================================================================
with detail_tab:
    sizes = sorted(df["gpu_label"].unique())
    default_size = "H100 x8" if "H100 x8" in sizes else sizes[0]
    size = st.selectbox("GPU size", sizes, index=sizes.index(default_size))
    sdf = df[df["gpu_label"] == size].copy()
    size_name = sdf["size_name"].iloc[0]
    price = sdf["price_per_hour"].iloc[0]
    st.caption(f"`{size_name}` · ${price}/hr")

    now_sel = df[df["ts"] == df["ts"].max()]
    now_sel = now_sel[now_sel["gpu_label"] == size]
    avail_now = now_sel[now_sel["available"] == 1]["region_name"].tolist()
    if avail_now:
        st.success(f"**{size}** is available now in: {', '.join(avail_now)}")
    else:
        st.error(f"**{size}** is not available in any region right now.")

    # Region x poll-time timeline.
    st.subheader(f"Availability timeline — {size}")
    pivot = (
        sdf.pivot_table(index="region_slug", columns="ts_local",
                        values="available", aggfunc="max").sort_index()
    )
    pivot = collapse_no_data_gaps(pivot, failed_ts)
    if pivot.shape[1] >= 1:
        fig = px.imshow(
            mark_total_outages(pivot),
            color_continuous_scale=OUTAGE_BINARY, zmin=OUTAGE, zmax=1,
            aspect="auto",
            labels=dict(x="Time (PT)", y="Region", color="Available"),
        )
        fig.update_coloraxes(showscale=False)
        tickvals, ticktext = day_ticks(pivot.columns)
        fig.update_xaxes(side="top", tickvals=tickvals, ticktext=ticktext,
                         tickangle=45)
        fig.update_traces(
            customdata=pivot.values,
            hovertemplate="Time=%{x}<br>Region=%{y}<br>Available=%{customdata}"
                          "<extra></extra>",
        )
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Green = available, dark = sold out, red = sold out in "
                   "every region at once, blank ✕ = failed polls (one column "
                   "per outage, however long). Each column is one poll.")

    # Hour-of-day pattern.
    st.subheader(f"Availability by hour of day — {size}")
    st.caption("Share of polls where the GPU was available, per region per hour.")
    hod = sdf.groupby(["region_slug", "hour"])["available"].mean().reset_index()
    if not hod.empty:
        hod_pivot = hod.pivot(index="region_slug", columns="hour",
                              values="available").sort_index()
        fig2 = px.imshow(
            hod_pivot, color_continuous_scale="Greens", aspect="auto",
            labels=dict(x="Hour of day (PT)", y="Region", color="% available"),
            zmin=0, zmax=1,
        )
        st.plotly_chart(fig2, use_container_width=True)

    # Overall % by region.
    st.subheader(f"Overall availability — {size}")
    by_region = (
        sdf.groupby("region_slug")["available"].mean()
        .sort_values(ascending=False).reset_index()
    )
    by_region["pct"] = (by_region["available"] * 100).round(1)
    fig3 = px.bar(by_region, x="region_slug", y="pct",
                  labels=dict(region_slug="Region", pct="% of polls available"))
    fig3.update_yaxes(range=[0, 100])
    st.plotly_chart(fig3, use_container_width=True)
