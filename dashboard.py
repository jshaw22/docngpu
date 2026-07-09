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

# DO only offers GPU droplets in these regions; the rest are always unavailable
# and just add noise / flatten the color gradient. We filter to these at display
# time (the collector still logs all regions, so we're covered if DO adds one).
GPU_REGIONS = ["nyc2", "sfo3", "atl1", "ric1", "tor1", "ams3"]

# Visualization ceiling: treat 5+ available regions as "fully available" so the
# common low counts (0-3) stay visually distinct instead of washing out.
SCALE_CAP = 5

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
    # Keep only the regions DO actually offers GPUs in.
    df = df[df["region_slug"].isin(GPU_REGIONS)].copy()
    # Friendly label per size, e.g. "H100 x8"
    df["gpu_label"] = (
        df["gpu_model"].str.replace("nvidia_", "", regex=False)
        .str.replace("amd_", "", regex=False)
        .str.upper()
        + " x" + df["gpu_count"].astype(str)
    )
    return df, failed_ts


def with_no_data_gaps(pivot, failed_ts):
    """Insert failed-poll timestamps as all-NaN columns so heatmaps show an
    explicit blank gap instead of silently skipping the time span."""
    if len(failed_ts) == 0:
        return pivot
    return pivot.reindex(columns=pivot.columns.union(failed_ts).sort_values())


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
failed_note = f" · {len(failed_ts)} failed poll(s)" if len(failed_ts) else ""
st.caption(
    f"{n_polls} poll(s) · {df['ts_local'].min():%Y-%m-%d %H:%M} → "
    f"{df['ts_local'].max():%Y-%m-%d %H:%M} PT · {len(GPU_REGIONS)} GPU regions"
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
    snap = snap.sort_values("regions_now", ascending=False)

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
    fig_now.update_yaxes(categoryorder="total ascending")
    st.plotly_chart(fig_now, use_container_width=True)

    # Timeline heatmap: GPU (rows) x time (cols), color = # regions available.
    st.subheader("Availability over time — all GPUs")
    grid = (
        df.groupby(["gpu_label", "ts_local"])["available"].sum().reset_index()
    )
    pivot = grid.pivot(index="gpu_label", columns="ts_local", values="available")
    # Order rows so the most-available GPUs sit at the top.
    order = pivot.sum(axis=1).sort_values(ascending=False).index
    pivot = pivot.loc[order]
    pivot = with_no_data_gaps(pivot, failed_ts)
    fig_time = px.imshow(
        pivot,
        color_continuous_scale="Greens", zmin=0, zmax=SCALE_CAP, aspect="auto",
        labels=dict(x="Time (PT)", y="GPU", color="# regions"),
    )
    fig_time.update_xaxes(side="top")
    st.plotly_chart(fig_time, use_container_width=True)
    st.caption(
        f"Each cell = how many of the {len(GPU_REGIONS)} GPU regions had that GPU "
        f"available at that poll (color capped at {SCALE_CAP}+). Greener = more "
        "widely available; white/empty = none. Blank (transparent) columns = "
        "poll failed, no data. Fills in hourly."
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
    pivot = with_no_data_gaps(pivot, failed_ts)
    if pivot.shape[1] >= 1:
        fig = px.imshow(
            pivot, color_continuous_scale=[(0, "#2b2b3b"), (1, "#21c45d")],
            aspect="auto",
            labels=dict(x="Time (PT)", y="Region", color="Available"),
        )
        fig.update_coloraxes(showscale=False)
        fig.update_xaxes(side="top")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Green = available, dark = sold out, blank = poll failed "
                   "(no data). Each column is one poll.")

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
