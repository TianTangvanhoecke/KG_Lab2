from __future__ import annotations

import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from knodi_pipeline import (
    prepare_analysis,
    LINE_MACHINE_MAP,
    HP_LINE_MACHINE_MAP,
    MASTERCHAR_DESCRIPTIONS,
    PROCESS_LEVELS,
    STEP_MACHINES,
    resolve_root_fg_ids,
    resolve_root_fg_ids_by_batch_class,
    resolve_root_fg_ids_by_wire_rod_batch,
)


QUALITY_COLORS = {
    "Good": "seagreen",
    "Bad": "crimson",
    "Unknown": "gray",
    "Mixed (some FGs)": "darkorange",
    # HP spool whose chain has no real final-goods spool behind it -- either because it
    # was scrapped somewhere downstream, or because it simply hasn't been consumed/rolled
    # into an FG yet (its "FG Spool ID" is only a placeholder either way, so the two
    # cases can't be told apart from this data alone). Flagged distinctly rather than
    # lumped in with "Unknown", which is reserved for a genuine FG spool that's simply
    # missing this quality tag's value.
    "No FG (Scrapped or Not Yet Consumed)": "black",
}

# Tags with a known 1=OK / 0=not-OK convention; everything else uses a threshold rule.
BINARY_QUALITY_TAGS = {"T_VC_IS", "T_VC_NB"}

# Raw _-BIC_-ZTRMACH code -> friendly display name, for both FG and HP producing lines.
MACHINE_LABELS = {**{v: k for k, v in LINE_MACHINE_MAP.items()}, **{v: k for k, v in HP_LINE_MACHINE_MAP.items()}}

# Machine-choice map per anchor level, for the sidebar "Machine / Equipment / Line"
# multiselect. Levels 1/2 (FG/HP) have friendly line names; deeper levels (patenting,
# drawing, pickling) only have raw SAP machine codes configured, so use those as-is.
LEVEL_MACHINE_MAP = {
    1: LINE_MACHINE_MAP,
    2: HP_LINE_MACHINE_MAP,
    3: {f"Machine {code}" for code in STEP_MACHINES["patenting"]},
    4: {f"Machine {code}" for code in STEP_MACHINES["drawing"]},
    5: {f"Machine {code}" for code in STEP_MACHINES["pickling"]},
}
LEVEL_MACHINE_MAP = {
    level: (choices if isinstance(choices, dict) else {label: label.replace("Machine ", "") for label in choices})
    for level, choices in LEVEL_MACHINE_MAP.items()
}

FEATURE_LABELS = {
    "Parameter Min": "Minimum Coat Weight PV",
    "Parameter Max": "Maximum Coat Weight PV",
    "Parameter Mean": "Mean Coat Weight PV",
    "Parameter Median": "Median Coat Weight PV",
    "Parameter Std": "Coat Weight PV standard deviation",
    "Parameter Range": "Coat Weight PV range",
    "Peak Count": "Coat Weight PV peak count",
    "Peak Maximum": "Maximum Coat Weight PV peak",
}

# Symbol/color/label per traceability level, for the multi-level genealogy chart.
# Level 1 (FG) is already quality-colored in the main traceability chart, so this
# chart instead gives every stage its own fixed, distinct color to encode "stage".
STAGE_INFO = {
    1: {"label": "Final Goods (Rolling)", "symbol": "circle", "color": "seagreen"},
    2: {"label": "Half Product (Coating)", "symbol": "square", "color": "royalblue"},
    3: {"label": "Patenting", "symbol": "diamond", "color": "mediumpurple"},
    4: {"label": "Drawing", "symbol": "hexagon", "color": "teal"},
    5: {"label": "Pickling", "symbol": "triangle-up", "color": "goldenrod"},
    # Synthetic level: not its own queried process step, just the raw wire-rod BATCH
    # identifier already carried on every fetched trace row. Only shown when the user
    # traces all the way down to pickling (level 5).
    6: {"label": "Wire Rod (Raw Material)", "symbol": "star", "color": "firebrick"},
}

# Synthetic sentinel "level" for spool rows that belong to a genuinely different,
# unconfigured product (see knodi_pipeline._annotate_process_levels /
# _other_product_slice) -- e.g. this dashboard is configured for WIPER_ARM_WIRE, but a
# row's MATL_GROUP is something like "FG_AUW" or "WIP_DO" from another product line
# entirely. Grayed out and rendered as its own bottom lane so it's still visible
# (material really was consumed/produced there) without being mistaken for a genuine
# stage of the tracked product.
OTHER_PRODUCT_LEVEL = 99
STAGE_INFO[OTHER_PRODUCT_LEVEL] = {
    "label": "Other product / process (not tracked)", "symbol": "x", "color": "lightgray",
}


def normalize_id(value, suffix_length=None):
    text = str(value).strip()
    return text[-suffix_length:] if suffix_length else text.lstrip("0") or "0"


def parse_id_list(text):
    """Split a free-text box (comma- and/or newline-separated) into a clean list of tokens."""
    if not text:
        return []
    return [token.strip() for token in re.split(r"[,\n]+", text) if token.strip()]


def strip_batch_prefix(value):
    """Batch numbers carry a fixed 2-character zero prefix that isn't meaningful to users."""
    text = str(value).strip()
    return text[2:] if len(text) > 2 else text


def machine_label(code):
    text = str(code).strip()
    return MACHINE_LABELS.get(text, text)


def quality_table(quality, masterchar):
    result = quality[quality["MASTERCHAR"].astype(str).str.strip().eq(masterchar)].copy()
    result["FG Spool ID"] = result["_-BIC_-ZUSERC1"].map(normalize_id)
    result["Quality Value"] = pd.to_numeric(result["SUM_XBAR"], errors="coerce")
    return result[["FG Spool ID", "Quality Value"]].dropna().drop_duplicates("FG Spool ID", keep="last")


def quality_label_from_value(value, rule):
    """Classify a single quality reading as Good/Bad/Unknown per the active rule."""
    if pd.isna(value):
        return "Unknown"
    if rule["type"] == "binary":
        return "Bad" if value == rule["bad_value"] else "Good"
    if rule["direction"] == ">=":
        return "Bad" if value >= rule["threshold"] else "Good"
    return "Bad" if value <= rule["threshold"] else "Good"


def build_visual_tables(tables, masterchar, rule):
    trace = tables["trace_edges"].copy()
    fg_trace = tables["fg_trace"].copy()
    hp = tables["half_products"].copy()
    quality = quality_table(tables["quality"], masterchar)

    fg_times = fg_trace[["FG Spool ID", "Spool Time", "Prod Order", "MATERIAL", "Machine", "BATCH"]].rename(
        columns={
            "Spool Time": "FG Production Time", "Prod Order": "FG Batch",
            "MATERIAL": "FG Material", "Machine": "FG Machine", "BATCH": "Wire Rod Batch",
        }
    ).copy()
    fg_times["FG Spool ID"] = fg_times["FG Spool ID"].map(normalize_id)
    fg_times["FG Material"] = fg_times["FG Material"].map(normalize_id)
    fg_times["FG Batch"] = fg_times["FG Batch"].map(strip_batch_prefix)
    fg_times["FG Machine"] = fg_times["FG Machine"].map(machine_label)
    fg_times = fg_times.dropna(subset=["FG Production Time"]).drop_duplicates("FG Spool ID")

    if "BATCH" not in hp.columns:
        hp = hp.assign(BATCH=pd.NA)
    hp_times = hp[["HP Spool ID", "Spool Time", "HP Batch", "HP Machine", "BATCH"]].rename(
        columns={"Spool Time": "HP Production Time", "BATCH": "HP Wire Rod Batch"}
    ).copy()
    hp_times["HP Spool ID"] = hp_times["HP Spool ID"].map(normalize_id)
    hp_times["HP Production Time"] = pd.to_datetime(hp_times["HP Production Time"], errors="coerce")
    hp_times["HP Batch"] = hp_times["HP Batch"].map(strip_batch_prefix)
    hp_times["HP Machine"] = hp_times["HP Machine"].map(machine_label)

    footprint = tables["footprints"].copy()
    footprint["HP Spool ID"] = footprint["HP Spool ID"].map(normalize_id)
    footprint = footprint.rename(columns={"Parameter Mean": "Mean Coat Weight"})
    footprint = footprint.drop_duplicates("HP Spool ID", keep="last")

    trace["FG Spool ID"] = trace["FG Spool ID"].map(lambda value: normalize_id(value) if pd.notna(value) else pd.NA)
    trace["HP Spool ID"] = trace["HP Spool ID"].map(normalize_id)
    edges = (trace.merge(hp_times, on="HP Spool ID", how="left")
             .merge(footprint, on="HP Spool ID", how="left")
             .merge(fg_times, on="FG Spool ID", how="left")
             .merge(quality, on="FG Spool ID", how="left"))
    # An HP whose chain was scrapped before ever reaching a genuine rolling/FG spool (or
    # simply hasn't been consumed/rolled into one yet) has an "FG Spool ID" that's only
    # a placeholder -- it never matches a row in fg_times, so FG Production Time stays
    # NaN. Flag those distinctly from a real FG spool that simply lacks a value for this
    # particular quality tag ("Unknown"); we can't distinguish scrapped from not-yet-
    # consumed from this data alone, so both are folded into one label.
    edges["Quality Label"] = np.where(
        edges["FG Production Time"].isna(),
        "No FG (Scrapped or Not Yet Consumed)",
        edges["Quality Value"].map(lambda value: quality_label_from_value(value, rule)),
    )
    # HPs not yet consumed by a tracked FG have no FG Production Time; keep them for the footprint view.
    return edges.dropna(subset=["HP Production Time"])


def traceability_figure(edges, quality_tag):
    linked = edges.dropna(subset=["FG Spool ID", "FG Production Time"])

    # FG circles sit on the diagonal (their own time on both axes). HP squares only
    # have a real x (HP production time); their y is a visual rank spread across the
    # FG time range so arrows reveal FIFO vs. out-of-order consumption.
    hp = edges.groupby("HP Spool ID", as_index=False).agg(
        {"HP Production Time": "first", "HP Batch": "first", "HP Machine": "first", "Mean Coat Weight": "first"}
    ).sort_values("HP Production Time").reset_index(drop=True)

    fg = linked.groupby("FG Spool ID", as_index=False).agg(
        {"FG Production Time": "first", "FG Batch": "first", "FG Material": "first", "FG Machine": "first",
         "Quality Value": "first", "Quality Label": "first"}
    )

    # Quality drives the circle color; material is shown as a small inner symbol on top.
    inner_symbols = ["star", "triangle-up", "diamond", "cross", "pentagon", "hexagon", "triangle-down", "x"]
    materials = sorted(fg["FG Material"].dropna().unique())
    material_symbol_map = {material: inner_symbols[i % len(inner_symbols)] for i, material in enumerate(materials)}

    if not fg.empty and len(hp) > 1:
        fg_min, fg_max = fg["FG Production Time"].min(), fg["FG Production Time"].max()
        span = fg_max - fg_min
        if span.total_seconds() > 0:
            hp["HP Plot Y"] = fg_min + (hp.index / (len(hp) - 1)) * span
        else:
            hp["HP Plot Y"] = fg_min
    elif not fg.empty:
        hp["HP Plot Y"] = fg["FG Production Time"].min()
    else:
        hp["HP Plot Y"] = hp["HP Production Time"]

    hp_plot_y = hp.set_index("HP Spool ID")["HP Plot Y"]
    linked = linked.copy()
    linked["HP Plot Y"] = linked["HP Spool ID"].map(hp_plot_y)

    figure = go.Figure()
    for _, edge in linked.iterrows():
        figure.add_annotation(
            x=edge["FG Production Time"], y=edge["FG Production Time"],
            ax=edge["HP Production Time"], ay=edge["HP Plot Y"],
            xref="x", yref="y", axref="x", ayref="y",
            arrowhead=3, arrowsize=1, arrowwidth=1.2,
            arrowcolor="rgba(100,100,100,0.5)", showarrow=True,
        )
    for label, group in fg.groupby("Quality Label"):
        figure.add_trace(go.Scatter(
            x=group["FG Production Time"], y=group["FG Production Time"], mode="markers",
            name=f"FG spool: {label}",
            marker=dict(symbol="circle", size=15, color=QUALITY_COLORS[label], line=dict(color="black", width=0.7)),
            customdata=group[["FG Spool ID", "FG Batch", "FG Material", "FG Machine", "Quality Value"]],
            hovertemplate=f"FG spool: %{{customdata[0]}}<br>FG batch: %{{customdata[1]}}<br>Material: %{{customdata[2]}}<br>Machine: %{{customdata[3]}}<br>{quality_tag}: %{{customdata[4]}}<br>FG production time: %{{x}}<extra></extra>",
        ))
    for material, symbol in material_symbol_map.items():
        group = fg[fg["FG Material"] == material]
        figure.add_trace(go.Scatter(
            x=group["FG Production Time"], y=group["FG Production Time"], mode="markers",
            name=f"Material: {material}", legendgroup="material",
            marker=dict(symbol=symbol, size=6, color="white", line=dict(color="black", width=0.8)),
            hoverinfo="skip",
        ))
    figure.add_trace(go.Scatter(
        x=hp["HP Production Time"], y=hp["HP Plot Y"], mode="markers", name="HP spool",
        marker=dict(symbol="square", size=10, color="royalblue", line=dict(color="black", width=0.5)),
        customdata=hp[["HP Spool ID", "HP Batch", "HP Machine", "Mean Coat Weight"]],
        hovertemplate="HP spool: %{customdata[0]}<br>HP batch: %{customdata[1]}<br>Machine: %{customdata[2]}<br>HP production time: %{x}<br>Mean Coat Weight PV: %{customdata[3]:.2f}<extra></extra>",
    ))
    figure.update_layout(
        title="Traceability flow: HP spools to FG spools",
        xaxis_title="HP production time",
        yaxis_title="FG production time",
        height=700, template="plotly_white", hovermode="closest", dragmode="zoom",
    )
    return figure


def _lane_plot_positions(level_nodes, lane_index, lane_height=0.6):
    """Assign each node a plot-Y: a fixed lane per rendered level (FG lane on top,
    deepest level at the bottom), with nodes spread by production-time rank within
    their own lane so close/overlapping timestamps stay visually distinguishable when
    zoomed in. `lane_index` is the lane's position among the levels actually being
    rendered (not the raw level number), so a sentinel level like the "other product"
    lane still sits directly below the deepest real lane instead of being pushed far
    off-scale.
    """
    ordered = level_nodes.sort_values("Spool Time").reset_index(drop=True).copy()
    lane_base = -lane_index
    count = len(ordered)
    if count > 1:
        ordered["Plot Y"] = lane_base + (ordered.index / (count - 1) - 0.5) * lane_height
    else:
        ordered["Plot Y"] = lane_base
    return ordered



def build_genealogy_tables(edges, tables, max_level):
    """Build one combined node table (Level 1=FG .. up to max_level) and one consumption
    edge table for the multi-level genealogy chart.

    `edges` is the already-merged FG/HP table from build_visual_tables, so the FG<->HP
    link reuses exactly the same (already validated) relationship as the main
    traceability chart. Levels 3+ (patenting/drawing/pickling) come straight from
    `tables` and are chained together using the real _-BIC_-ZCSPLID consumption
    pointer ("Consumes Spool ID") that knodi_pipeline.py now carries through each
    level slice, so arrows represent genuine spool-to-spool consumption rather than
    just "shares the same root FG id".
    """
    empty_nodes = pd.DataFrame(columns=[
        "Spool ID", "Spool Time", "Batch", "Machine", "Level", "Consumes Spool ID",
        "Quality Label", "Quality Value", "Material", "Wire Rod Batch",
    ])
    empty_edges = pd.DataFrame(columns=[
        "Downstream Level", "Downstream Spool ID", "Downstream Time",
        "Upstream Level", "Upstream Spool ID", "Upstream Time",
    ])
    if max_level < 1:
        return empty_nodes, empty_edges

    linked = edges.dropna(subset=["FG Spool ID", "FG Production Time"])
    fg_nodes = linked.groupby("FG Spool ID", as_index=False).agg(
        {"FG Production Time": "first", "FG Batch": "first", "FG Machine": "first",
         "Quality Value": "first", "Quality Label": "first", "FG Material": "first",
         "Wire Rod Batch": "first"}
    ).rename(columns={
        "FG Spool ID": "Spool ID", "FG Production Time": "Spool Time",
        "FG Batch": "Batch", "FG Machine": "Machine", "FG Material": "Material",
    })
    fg_nodes["Level"] = 1
    fg_nodes["Consumes Spool ID"] = pd.NA
    level_frames = [fg_nodes] if not fg_nodes.empty else []

    edge_rows = []
    if max_level >= 2:
        hp_nodes = edges.groupby("HP Spool ID", as_index=False).agg(
            {"HP Production Time": "first", "HP Batch": "first", "HP Machine": "first",
             "HP Wire Rod Batch": "first"}
        ).rename(columns={
            "HP Spool ID": "Spool ID", "HP Production Time": "Spool Time",
            "HP Batch": "Batch", "HP Machine": "Machine", "HP Wire Rod Batch": "Wire Rod Batch",
        })
        hp_nodes["Level"] = 2
        hp_nodes["Quality Label"] = pd.NA
        hp_nodes["Quality Value"] = pd.NA
        hp_nodes["Material"] = pd.NA
        hp_consumes = tables.get("half_products", pd.DataFrame())
        if not hp_consumes.empty and "Consumes Spool ID" in hp_consumes.columns:
            hp_consumes = hp_consumes[["HP Spool ID", "Consumes Spool ID"]].rename(columns={"HP Spool ID": "Spool ID"})
            hp_consumes["Spool ID"] = hp_consumes["Spool ID"].map(normalize_id)
            hp_nodes = hp_nodes.merge(hp_consumes.drop_duplicates("Spool ID"), on="Spool ID", how="left")
        else:
            hp_nodes["Consumes Spool ID"] = pd.NA
        if not hp_nodes.empty:
            level_frames.append(hp_nodes)

        # Level 1 -> 2 (FG -> HP): reuse the already-validated FG/HP linkage from `edges`.
        fg_hp = linked[["FG Spool ID", "FG Production Time", "HP Spool ID", "HP Production Time"]].dropna().rename(columns={
            "FG Spool ID": "Downstream Spool ID", "FG Production Time": "Downstream Time",
            "HP Spool ID": "Upstream Spool ID", "HP Production Time": "Upstream Time",
        }).drop_duplicates()
        fg_hp["Downstream Level"], fg_hp["Upstream Level"] = 1, 2
        edge_rows.append(fg_hp)

    for level, key in ((3, "patenting"), (4, "drawing"), (5, "pickling")):
        if level > max_level:
            break
        raw = tables.get(key, pd.DataFrame())
        if raw.empty:
            continue
        raw = raw.rename(columns={"Prod Order": "Batch", "BATCH": "Wire Rod Batch"})
        # Defensive: tolerate a stale/legacy table shape (e.g. before "Consumes Spool ID"
        # was added) instead of raising, so an out-of-date cached result just shows nodes
        # without deeper-level consumption arrows rather than crashing the whole chart.
        for required_column in ("Spool ID", "Spool Time", "Batch", "Machine", "Consumes Spool ID", "Wire Rod Batch"):
            if required_column not in raw.columns:
                raw[required_column] = pd.NA
        df = raw[["Spool ID", "Spool Time", "Batch", "Machine", "Consumes Spool ID", "Wire Rod Batch"]].copy()
        df["Spool ID"] = df["Spool ID"].map(normalize_id)
        df["Consumes Spool ID"] = df["Consumes Spool ID"].map(lambda v: normalize_id(v) if pd.notna(v) else pd.NA)
        df["Batch"] = df["Batch"].map(strip_batch_prefix)
        df["Machine"] = df["Machine"].map(machine_label)
        df["Material"] = pd.NA
        df["Level"] = level
        df["Quality Label"] = pd.NA
        df["Quality Value"] = pd.NA
        df["Product Group"] = pd.NA
        df = df.dropna(subset=["Spool Time"]).drop_duplicates("Spool ID")
        if not df.empty:
            level_frames.append(df)

    # Rows that genuinely belong to a different, unconfigured product (see
    # knodi_pipeline._other_product_slice) -- always surfaced (independent of
    # max_level) whenever a resolved chain happened to pull one in, so the user can
    # still see the link instead of it silently vanishing or being mislabeled as one
    # of this product's own levels.
    other_raw = tables.get("other_product", pd.DataFrame())
    if not other_raw.empty:
        other_raw = other_raw.rename(columns={"Prod Order": "Batch", "BATCH": "Wire Rod Batch"})
        for required_column in ("Spool ID", "Spool Time", "Batch", "Machine", "Consumes Spool ID", "Wire Rod Batch", "Process Step"):
            if required_column not in other_raw.columns:
                other_raw[required_column] = pd.NA
        other_df = other_raw[[
            "Spool ID", "Spool Time", "Batch", "Machine", "Consumes Spool ID", "Wire Rod Batch", "Process Step",
        ]].copy()
        other_df["Spool ID"] = other_df["Spool ID"].map(normalize_id)
        other_df["Consumes Spool ID"] = other_df["Consumes Spool ID"].map(lambda v: normalize_id(v) if pd.notna(v) else pd.NA)
        other_df["Batch"] = other_df["Batch"].map(strip_batch_prefix)
        other_df["Machine"] = other_df["Machine"].map(machine_label)
        other_df["Material"] = pd.NA
        other_df["Level"] = OTHER_PRODUCT_LEVEL
        other_df["Quality Label"] = pd.NA
        other_df["Quality Value"] = pd.NA
        other_df = other_df.rename(columns={"Process Step": "Product Group"})
        other_df = other_df.dropna(subset=["Spool Time"]).drop_duplicates("Spool ID")
        if not other_df.empty:
            level_frames.append(other_df)

    nodes = pd.concat(level_frames, ignore_index=True) if level_frames else empty_nodes
    if "Product Group" not in nodes.columns:
        nodes["Product Group"] = pd.NA
    nodes["Spool Time"] = pd.to_datetime(nodes["Spool Time"], errors="coerce")
    nodes = nodes.dropna(subset=["Spool Time"])

    # Level 2 -> 3 -> 4 -> 5 (and same-level re-processing, e.g. re-patenting): real
    # _-BIC_-ZCSPLID consumption chain within ZPP_M06. A spool's "Consumes Spool ID"
    # parent is not always exactly one level deeper -- material can be reprocessed
    # through the SAME step more than once (e.g. a patenting spool consumed again to
    # produce a second patenting spool before moving on), so match against any node at
    # the same level or deeper, not just level+1. This still avoids matching a
    # shallower level (consumption never flows backwards towards FG) and avoids the
    # documented ID-collision risk between unrelated levels by requiring
    # Upstream Level >= Downstream Level. "Other product" nodes don't have a genuine
    # level of their own, so that ordering check is skipped whenever either side of the
    # candidate edge is the OTHER_PRODUCT_LEVEL sentinel -- any direction is allowed.
    trackable = nodes["Level"].between(2, max_level) | nodes["Level"].eq(OTHER_PRODUCT_LEVEL)
    consumable = nodes[trackable & nodes["Consumes Spool ID"].notna()]
    if not consumable.empty:
        match_pool = nodes[trackable]
        merged = consumable[["Spool ID", "Spool Time", "Level", "Consumes Spool ID"]].rename(columns={
            "Spool ID": "Downstream Spool ID", "Spool Time": "Downstream Time", "Level": "Downstream Level",
        }).merge(
            match_pool[["Spool ID", "Spool Time", "Level"]].rename(columns={
                "Spool ID": "Upstream Spool ID", "Spool Time": "Upstream Time", "Level": "Upstream Level",
            }),
            left_on="Consumes Spool ID", right_on="Upstream Spool ID", how="inner",
        )
        involves_other = merged["Downstream Level"].eq(OTHER_PRODUCT_LEVEL) | merged["Upstream Level"].eq(OTHER_PRODUCT_LEVEL)
        merged = merged[
            (involves_other | (merged["Upstream Level"] >= merged["Downstream Level"]))
            & (merged["Downstream Spool ID"] != merged["Upstream Spool ID"])
        ]
        if not merged.empty:
            edge_rows.append(merged[[
                "Downstream Level", "Downstream Spool ID", "Downstream Time",
                "Upstream Level", "Upstream Spool ID", "Upstream Time",
            ]])

    # Level 6 (synthetic, "raw material"): the wire-rod BATCH is not its own queried
    # process step -- it's already present as a plain attribute on every fetched trace
    # row, constant across a whole chain -- so we only turn it into its own node/lane
    # when the user has asked to trace all the way down to pickling (level 5). We take
    # the value from the pickling (level 5) rows since that's the deepest level actually
    # queried; every distinct batch value becomes one node, linked from every pickling
    # spool that carries it.
    if max_level >= 5:
        pickling_nodes = nodes[(nodes["Level"] == 5) & nodes["Wire Rod Batch"].notna()]
        if not pickling_nodes.empty:
            wire_rod_nodes = pickling_nodes.groupby("Wire Rod Batch", as_index=False).agg({"Spool Time": "min"})
            wire_rod_nodes = wire_rod_nodes.rename(columns={"Wire Rod Batch": "Spool ID"})
            wire_rod_nodes["Wire Rod Batch"] = wire_rod_nodes["Spool ID"]
            wire_rod_nodes["Level"] = 6
            for column in ("Batch", "Machine", "Consumes Spool ID", "Quality Label", "Quality Value", "Material"):
                wire_rod_nodes[column] = pd.NA
            nodes = pd.concat([nodes, wire_rod_nodes], ignore_index=True)

            wire_rod_edges = pickling_nodes[["Spool ID", "Spool Time", "Wire Rod Batch"]].rename(columns={
                "Spool ID": "Downstream Spool ID", "Spool Time": "Downstream Time",
                "Wire Rod Batch": "Upstream Spool ID",
            })
            upstream_time = wire_rod_nodes.set_index("Spool ID")["Spool Time"]
            wire_rod_edges["Upstream Time"] = wire_rod_edges["Upstream Spool ID"].map(upstream_time)
            wire_rod_edges["Downstream Level"], wire_rod_edges["Upstream Level"] = 5, 6
            edge_rows.append(wire_rod_edges[[
                "Downstream Level", "Downstream Spool ID", "Downstream Time",
                "Upstream Level", "Upstream Spool ID", "Upstream Time",
            ]])

    consumption_edges = pd.concat(edge_rows, ignore_index=True) if edge_rows else empty_edges
    return nodes, consumption_edges


def genealogy_figure(nodes, consumption_edges, max_level):
    """Multi-level genealogy chart: FG -> HP -> Patenting -> Drawing -> Pickling, up to
    `max_level`. X = each spool's own real production time. Y = a fixed lane per process
    level (FG lane on top, pickling lane at the bottom); symbol/color encode the stage.
    """
    figure = go.Figure()
    lane_positions = {}
    levels_to_render = list(range(1, max_level + 1))
    if max_level >= 5 and (nodes["Level"] == 6).any():
        levels_to_render.append(6)
    if (nodes["Level"] == OTHER_PRODUCT_LEVEL).any():
        levels_to_render.append(OTHER_PRODUCT_LEVEL)
    for level in levels_to_render:
        subset = nodes[nodes["Level"] == level]
        if subset.empty:
            continue
        lane_positions[level] = _lane_plot_positions(subset, levels_to_render.index(level) + 1)

    position_lookup = {level: df.set_index("Spool ID")["Plot Y"] for level, df in lane_positions.items()}

    for _, edge in consumption_edges.iterrows():
        downstream_positions = position_lookup.get(edge["Downstream Level"])
        upstream_positions = position_lookup.get(edge["Upstream Level"])
        if downstream_positions is None or upstream_positions is None:
            continue
        downstream_y = downstream_positions.get(edge["Downstream Spool ID"])
        upstream_y = upstream_positions.get(edge["Upstream Spool ID"])
        if pd.isna(downstream_y) or pd.isna(upstream_y):
            continue
        figure.add_annotation(
            x=edge["Downstream Time"], y=downstream_y,
            ax=edge["Upstream Time"], ay=upstream_y,
            xref="x", yref="y", axref="x", ayref="y",
            arrowhead=2, arrowsize=1, arrowwidth=1,
            arrowcolor="rgba(120,120,120,0.45)", showarrow=True,
        )

    for level, df in lane_positions.items():
        info = STAGE_INFO[level]
        if level == 1:
            # FG nodes are colored by quality (as in the main traceability chart),
            # with the stage symbol still applied so it reads consistently with the
            # other levels below.
            df = df.copy()
            df["Quality Label"] = df["Quality Label"].fillna("Unknown")
            for label, group in df.groupby("Quality Label"):
                figure.add_trace(go.Scatter(
                    x=group["Spool Time"], y=group["Plot Y"], mode="markers",
                    name=f"{info['label']}: {label}",
                    marker=dict(symbol=info["symbol"], size=13, color=QUALITY_COLORS[label], line=dict(color="black", width=0.6)),
                    customdata=group[["Spool ID", "Batch", "Machine", "Quality Value", "Wire Rod Batch"]],
                    hovertemplate=(
                        f"{info['label']} ({label})<br>Spool: %{{customdata[0]}}<br>Batch: %{{customdata[1]}}<br>"
                        "Machine: %{customdata[2]}<br>Quality value: %{customdata[3]}<br>"
                        "Wire rod batch: %{customdata[4]}<br>Production time: %{x}<extra></extra>"
                    ),
                ))
            # Material is shown as a small inner white symbol on top of the quality-colored
            # circle, mirroring the original FG<->HP traceability chart.
            inner_symbols = ["star", "triangle-up", "diamond", "cross", "pentagon", "hexagon", "triangle-down", "x"]
            materials = sorted(df["Material"].dropna().unique())
            material_symbol_map = {material: inner_symbols[i % len(inner_symbols)] for i, material in enumerate(materials)}
            for material, symbol in material_symbol_map.items():
                group = df[df["Material"] == material]
                figure.add_trace(go.Scatter(
                    x=group["Spool Time"], y=group["Plot Y"], mode="markers",
                    name=f"Material: {material}", legendgroup="material",
                    marker=dict(symbol=symbol, size=5, color="white", line=dict(color="black", width=0.8)),
                    hoverinfo="skip",
                ))
            continue
        if level == OTHER_PRODUCT_LEVEL:
            # Rows belonging to a different, unconfigured product (see
            # knodi_pipeline._other_product_slice) -- always rendered gray/"x" so they
            # read as "linked, but not part of this tracked product" rather than being
            # mistaken for one of the genuine stages above.
            figure.add_trace(go.Scatter(
                x=df["Spool Time"], y=df["Plot Y"], mode="markers",
                name=info["label"],
                marker=dict(symbol=info["symbol"], size=10, color=info["color"], line=dict(color="dimgray", width=0.8)),
                customdata=df[["Spool ID", "Batch", "Machine", "Product Group"]],
                hovertemplate=(
                    f"{info['label']}<br>Spool: %{{customdata[0]}}<br>Batch: %{{customdata[1]}}<br>"
                    "Machine: %{customdata[2]}<br>Foreign process/product group: %{customdata[3]}<br>"
                    "Production time: %{x}<extra></extra>"
                ),
            ))
            continue
        if level == 6:
            # Wire-rod nodes: no spool/machine of their own, just the raw-material
            # batch identifier shared by every pickling spool that consumed it.
            figure.add_trace(go.Scatter(
                x=df["Spool Time"], y=df["Plot Y"], mode="markers",
                name=info["label"],
                marker=dict(symbol=info["symbol"], size=12, color=info["color"], line=dict(color="black", width=0.6)),
                customdata=df[["Wire Rod Batch"]],
                hovertemplate=(
                    f"{info['label']}<br>Wire rod batch: %{{customdata[0]}}<br>"
                    "Earliest linked spool time: %{x}<extra></extra>"
                ),
            ))
            continue
        figure.add_trace(go.Scatter(
            x=df["Spool Time"], y=df["Plot Y"], mode="markers",
            name=info["label"],
            marker=dict(symbol=info["symbol"], size=10, color=info["color"], line=dict(color="black", width=0.6)),
            customdata=df[["Spool ID", "Batch", "Machine", "Wire Rod Batch"]],
            hovertemplate=(
                f"{info['label']}<br>Spool: %{{customdata[0]}}<br>Batch: %{{customdata[1]}}<br>"
                "Machine: %{customdata[2]}<br>Wire rod batch: %{customdata[3]}<br>Production time: %{x}<extra></extra>"
            ),
        ))

    figure.update_layout(
        title=f"Multi-level genealogy (Final Goods \u2192 {STAGE_INFO[max_level]['label']})",
        xaxis_title="Spool production time",
        yaxis=dict(
            title="Process stage",
            tickmode="array",
            tickvals=[-(idx + 1) for idx in range(len(levels_to_render))],
            ticktext=[STAGE_INFO[level]["label"] for level in levels_to_render],
        ),
        height=220 + 120 * len(levels_to_render), template="plotly_white", hovermode="closest", dragmode="zoom",
    )
    return figure


def footprint_figure(edges, feature):
    footprint = edges[["HP Spool ID", "HP Batch", "HP Machine", "HP Production Time", "FG Spool ID", "Quality Label", feature]].copy()
    footprint = footprint.drop_duplicates(["HP Spool ID", "FG Spool ID"])
    quality_by_hp = footprint.groupby("HP Spool ID").agg(
        Linked_FG_Count=("FG Spool ID", "nunique"),
        Scrapped_FG_Count=("Quality Label", lambda values: values.eq("No FG (Scrapped or Not Yet Consumed)").sum()),
        Failed_FG_Count=("Quality Label", lambda values: values.eq("Bad").sum()),
        Known_FG_Count=("Quality Label", lambda values: values.isin(["Good", "Bad"]).sum()),
    ).reset_index()
    # Links whose FG chain never reached a genuine final-goods spool -- whether scrapped
    # or simply not yet consumed, we can't tell which from this data -- are excluded from
    # the "how many linked FGs actually have a verdict" denominator, and called out as
    # their own label only when EVERY link for this HP is one of those placeholder chains.
    quality_by_hp["Effective_FG_Count"] = quality_by_hp["Linked_FG_Count"] - quality_by_hp["Scrapped_FG_Count"]
    quality_by_hp["HP Quality Label"] = np.select(
        [
            quality_by_hp["Effective_FG_Count"].eq(0) & quality_by_hp["Linked_FG_Count"].gt(0),
            quality_by_hp["Failed_FG_Count"].eq(quality_by_hp["Effective_FG_Count"])
            & quality_by_hp["Effective_FG_Count"].gt(0),
            quality_by_hp["Failed_FG_Count"].gt(0)
            & quality_by_hp["Failed_FG_Count"].lt(quality_by_hp["Effective_FG_Count"]),
            quality_by_hp["Failed_FG_Count"].eq(0)
            & quality_by_hp["Known_FG_Count"].eq(quality_by_hp["Effective_FG_Count"])
            & quality_by_hp["Effective_FG_Count"].gt(0),
        ],
        [
            "No FG (Scrapped or Not Yet Consumed)",
            "Bad",
            "Mixed (some FGs)",
            "Good",
        ],
        default="Unknown",
    )

    # A single physical HP spool can be split across several downstream branches: some
    # may reach a real FG spool (known or unknown quality), others may never reach one
    # (scrapped, or simply not yet consumed). Show the actual real FG spool(s) linked to
    # this HP in the hover text -- never the placeholder id, which is not a real FG
    # spool and would misleadingly look identical to the HP's own spool ID.
    real_links = footprint[footprint["Quality Label"].ne("No FG (Scrapped or Not Yet Consumed)")]
    fg_display = (
        real_links.groupby("HP Spool ID")["FG Spool ID"]
        .apply(lambda ids: ", ".join(sorted(dict.fromkeys(ids))))
        .rename("Linked FG Display")
        .reset_index()
    )
    quality_by_hp = quality_by_hp.merge(fg_display, on="HP Spool ID", how="left")
    quality_by_hp["Linked FG Display"] = quality_by_hp["Linked FG Display"].where(
        quality_by_hp["Linked FG Display"].notna() & quality_by_hp["Linked FG Display"].ne(""),
        "None (scrapped or not yet consumed)",
    )

    footprint = footprint.drop(columns=["Quality Label", "FG Spool ID"]).drop_duplicates("HP Spool ID").merge(
        quality_by_hp, on="HP Spool ID", how="left"
    ).rename(columns={"HP Quality Label": "Quality Label"})
    figure = go.Figure()
    cluster_spacing = pd.Timedelta(minutes=4)
    footprint["Link Order"] = footprint.groupby("HP Spool ID").cumcount()
    footprint["Link Count"] = footprint.groupby("HP Spool ID")["HP Spool ID"].transform("count")
    footprint["Plot Time"] = footprint["HP Production Time"] + (
        footprint["Link Order"] - (footprint["Link Count"] - 1) / 2
    ) * cluster_spacing

    for label, group in footprint.groupby("Quality Label"):
        figure.add_trace(go.Scatter(
            x=group["Plot Time"], y=group[feature], mode="markers", name=label,
            marker=dict(symbol="square", size=11, color=QUALITY_COLORS[label], line=dict(color="black", width=0.7)),
            customdata=group[["HP Spool ID", "HP Batch", "HP Machine", "Linked FG Display", "HP Production Time", "Failed_FG_Count", "Effective_FG_Count", "Scrapped_FG_Count", "Linked_FG_Count"]],
            hovertemplate=f"HP spool: %{{customdata[0]}}<br>HP batch: %{{customdata[1]}}<br>Machine: %{{customdata[2]}}<br>Linked FG spool(s): %{{customdata[3]}}<br>HP production time: %{{customdata[4]}}<br>{FEATURE_LABELS.get(feature, feature)}: %{{y:.3f}}<br>Failed linked FGs: %{{customdata[5]}} / %{{customdata[6]}}<br>No-FG links (scrapped or not yet consumed): %{{customdata[7]}} / %{{customdata[8]}}<extra></extra>",
        ))
    label = FEATURE_LABELS.get(feature, feature)
    figure.update_layout(
        title=f"{label} by linked final-goods quality",
        xaxis_title="HP production time", yaxis_title=label,
        height=600, template="plotly_white", hovermode="closest", dragmode="zoom",
    )
    return figure


def matching_figure(tables):
    """Plot machine length, reset candidates, and HP matching results."""
    machine = tables["machine_data"][["Time", "Length PV"]].copy()
    machine["Time"] = pd.to_datetime(machine["Time"], errors="coerce")
    machine["Length PV"] = pd.to_numeric(machine["Length PV"], errors="coerce")
    machine = machine.dropna().sort_values("Time")

    resets = tables["reset_candidates"].copy()
    resets["Machine Time"] = pd.to_datetime(resets["Machine Time"], errors="coerce")
    resets["Machine Length"] = pd.to_numeric(resets["Machine Length"], errors="coerce")
    resets = resets.dropna(subset=["Machine Time", "Machine Length"])

    matches = tables["matches"].copy()
    matches["Spool Time"] = pd.to_datetime(matches["Spool Time"], errors="coerce")
    matches["Machine Time"] = pd.to_datetime(matches["Machine Time"], errors="coerce")
    matches["Spool Length (m)"] = pd.to_numeric(matches["Spool Length (m)"], errors="coerce")
    matches["Machine Length"] = pd.to_numeric(matches["Machine Length"], errors="coerce")
    matches["Matched"] = matches["Matched"].fillna(False).astype(bool)

    matched = matches[
        matches["Matched"]
        & matches["Spool Time"].notna()
        & matches["Machine Time"].notna()
        & matches["Spool Length (m)"].notna()
        & matches["Machine Length"].notna()
    ]
    unmatched = matches[
        ~matches["Matched"]
        & matches["Spool Time"].notna()
        & matches["Spool Length (m)"].notna()
    ]

    figure = go.Figure()
    figure.add_trace(go.Scatter(
        x=machine["Time"],
        y=machine["Length PV"],
        mode="lines",
        name="Machine Length PV",
        line=dict(color="steelblue", width=1),
        hovertemplate="Machine time: %{x}<br>Length PV: %{y:.1f} m<extra></extra>",
    ))
    figure.add_trace(go.Scatter(
        x=resets["Machine Time"],
        y=resets["Machine Length"],
        mode="markers",
        name="Machine reset candidate",
        marker=dict(color="crimson", symbol="x", size=8),
        hovertemplate="Reset: %{x}<br>Machine length: %{y:.1f} m<extra></extra>",
    ))
    figure.add_trace(go.Scatter(
        x=matched["Spool Time"],
        y=matched["Spool Length (m)"],
        mode="markers",
        name="Matched HP production spool",
        marker=dict(color="seagreen", size=10, line=dict(color="black", width=0.5)),
        customdata=matched[["HP Spool ID", "Machine Time", "Time Difference (min)", "Relative Length Difference"]],
        hovertemplate=(
            "HP spool: %{customdata[0]}<br>"
            "Production time: %{x}<br>"
            "Production length: %{y:.1f} m<br>"
            "Machine reset: %{customdata[1]}<br>"
            "Time difference: %{customdata[2]:.1f} min<br>"
            "Relative length difference: %{customdata[3]:.2%}<extra></extra>"
        ),
    ))
    figure.add_trace(go.Scatter(
        x=unmatched["Spool Time"],
        y=unmatched["Spool Length (m)"],
        mode="markers",
        name="Unmatched HP production spool",
        marker=dict(color="darkorange", symbol="circle-open", size=10),
        customdata=unmatched[["HP Spool ID"]],
        hovertemplate=(
            "HP spool: %{customdata[0]}<br>"
            "Production time: %{x}<br>"
            "Production length: %{y:.1f} m<br>"
            "No machine reset match<extra></extra>"
        ),
    ))

    for _, row in matched.iterrows():
        figure.add_annotation(
            x=row["Spool Time"],
            y=row["Spool Length (m)"],
            ax=row["Machine Time"],
            ay=row["Machine Length"],
            xref="x",
            yref="y",
            axref="x",
            ayref="y",
            arrowhead=3,
            arrowsize=1,
            arrowwidth=1.2,
            arrowcolor="seagreen",
            opacity=0.65,
            showarrow=True,
        )

    figure.update_layout(
        title="HP production spools matched to machine reset candidates",
        xaxis=dict(
            title="Time",
            type="date",
            rangeslider=dict(visible=True, thickness=0.08),
            rangeselector=dict(buttons=[
                dict(count=6, label="6h", step="hour", stepmode="backward"),
                dict(count=1, label="1d", step="day", stepmode="backward"),
                dict(count=3, label="3d", step="day", stepmode="backward"),
                dict(step="all", label="All"),
            ]),
        ),
        yaxis_title="Length (m)",
        hovermode="closest",
        dragmode="zoom",
        height=700,
        template="plotly_white",
        legend=dict(x=1.02, y=1, xanchor="left", yanchor="top"),
        margin=dict(r=240),
    )
    return figure


@st.cache_data(show_spinner=False)
def cached_prepare(dates, mode, machines, level=None, final_ids=None):
    return prepare_analysis(dates, mode=mode, machines=machines, level=level, final_ids=final_ids)


st.set_page_config(page_title="KNODI Traceability", layout="wide")
st.title("V1: KNODI traceability and process footprint")
st.caption("Select production dates, then inspect traceability and process features linked to final-goods quality.")

with st.sidebar:
    st.subheader("Query anchor")
    identifier_mode = st.radio(
        "How do you want to select spools?",
        ["Production date + machine", "Batch number(s)", "Spool ID(s)", "Wire rod raw-material batch"],
    )

    level = None
    if identifier_mode != "Wire rod raw-material batch":
        level = st.selectbox(
            "Anchor level / process stage",
            [1, 2, 3, 4, 5],
            format_func=lambda lvl: f"{lvl} \u2013 {STAGE_INFO[lvl]['label']}",
        )
    else:
        st.caption(
            "Matches ZPP_M06's BATCH field (raw wire-rod batch/heat number), which stays "
            "constant across every process level of one genealogy chain, so no anchor "
            "level is needed."
        )

    machine_map = {}
    line_choice = []
    date_values = None
    batch_text = spool_id_text = wire_rod_text = ""

    if identifier_mode == "Production date + machine":
        machine_map = LEVEL_MACHINE_MAP[level]
        date_values = st.date_input(
            f"{STAGE_INFO[level]['label']} production date(s)",
            value=(pd.Timestamp("2026-08-26").date(), pd.Timestamp("2026-08-26").date()),
        )
        line_choice = st.multiselect(
            "Machine / Equipment / Line",
            options=list(machine_map),
            default=list(machine_map),
        )
    elif identifier_mode == "Batch number(s)":
        st.caption("Matches ZPP_M08's batch classification code (_-BIC_-ZBAT_CLSF).")
        batch_text = st.text_area("Batch number(s) \u2014 one per line or comma-separated", height=100)
    elif identifier_mode == "Spool ID(s)":
        st.caption("Matches the spool's own ID at the selected anchor level.")
        spool_id_text = st.text_area("Spool ID(s) \u2014 one per line or comma-separated", height=100)
    else:
        wire_rod_text = st.text_area("Wire rod batch/heat number(s) \u2014 one per line or comma-separated", height=100)

    footprint_options = list(FEATURE_LABELS)
    selected_feature = st.selectbox(
        "Process footprint", footprint_options,
        format_func=lambda value: FEATURE_LABELS[value],
    )

    genealogy_max_level = st.select_slider(
        "Trace back to level (genealogy chart)",
        options=[1, 2, 3, 4, 5],
        value=2,
        format_func=lambda level: f"{level} \u2013 {STAGE_INFO[level]['label']}",
    )

    quality_tag_options = list(MASTERCHAR_DESCRIPTIONS)
    default_tag_index = quality_tag_options.index("T_VC_IS") if "T_VC_IS" in quality_tag_options else 0
    selected_quality_tag = st.selectbox(
        "Quality tag for coloring", quality_tag_options,
        index=default_tag_index,
        format_func=lambda value: f"{value} — {MASTERCHAR_DESCRIPTIONS[value]}",
    )
    if selected_quality_tag in BINARY_QUALITY_TAGS:
        quality_rule = {"type": "binary", "bad_value": 0}
        st.caption("Binary tag: 0 = Bad, 1 = Good.")
    else:
        threshold = st.number_input("Defect threshold", value=3.0)
        direction = st.radio("Bad when value is", ["≥ threshold", "≤ threshold"], horizontal=True)
        quality_rule = {
            "type": "threshold",
            "threshold": threshold,
            "direction": ">=" if direction.startswith("≥") else "<=",
        }

    run = st.button("Load analysis", type="primary")

if "analysis_tables" not in st.session_state:
    st.session_state.analysis_tables = None

if run:
    try:
        if identifier_mode == "Production date + machine":
            if not line_choice:
                st.error("Select at least one Machine / Equipment / Line.")
                st.stop()
            machines = tuple(machine_map[choice] for choice in line_choice)
            if isinstance(date_values, tuple):
                dates = pd.date_range(date_values[0], date_values[1], freq="D").date.tolist()
            else:
                dates = [date_values]
            dates_tuple = tuple(str(date) for date in dates)
            if level == 1:
                run_mode, run_level = "fg", None
            elif level == 2:
                run_mode, run_level = "hp", None
            else:
                run_mode, run_level = "level", level
            with st.spinner("Querying production, traceability, machine, and quality data..."):
                st.session_state.analysis_tables = cached_prepare(dates_tuple, run_mode, machines, run_level, None)
        else:
            if identifier_mode == "Batch number(s)":
                values = parse_id_list(batch_text)
                if not values:
                    st.error("Enter at least one batch number.")
                    st.stop()
                with st.spinner("Resolving batch number(s) to final-goods spools..."):
                    final_ids = resolve_root_fg_ids_by_batch_class(values, level=level, config=None)
            elif identifier_mode == "Spool ID(s)":
                values = parse_id_list(spool_id_text)
                if not values:
                    st.error("Enter at least one spool ID.")
                    st.stop()
                with st.spinner("Resolving spool ID(s) to final-goods spools..."):
                    final_ids = resolve_root_fg_ids(values, level=level, config=None)
            else:
                values = parse_id_list(wire_rod_text)
                if not values:
                    st.error("Enter at least one wire rod batch/heat number.")
                    st.stop()
                with st.spinner("Resolving wire rod batch(es) to final-goods spools..."):
                    final_ids = resolve_root_fg_ids_by_wire_rod_batch(values, config=None)
            if not final_ids:
                st.error("No final-goods spools were found for the given input.")
                st.stop()
            with st.spinner("Querying production, traceability, machine, and quality data..."):
                st.session_state.analysis_tables = cached_prepare(None, "ids", None, None, tuple(final_ids))
    except Exception as error:
        st.error(str(error))
        st.stop()

if st.session_state.analysis_tables is None:
    st.info("Select production dates and click Load analysis.")
    st.stop()

tables = st.session_state.analysis_tables
edges = build_visual_tables(tables, selected_quality_tag, quality_rule)
if edges.empty:
    st.warning("No complete traceability relationships were found for the selected dates.")
    st.stop()

with st.expander("Data preparation diagnostics"):
    st.dataframe(tables["diagnostics"], use_container_width=True, hide_index=True)

metric_1, metric_2, metric_3 = st.columns(3)
metric_1.metric("FG spools", edges["FG Spool ID"].nunique())
metric_2.metric("HP spools", edges["HP Spool ID"].nunique())
metric_3.metric("Traceability links", len(edges))

# The original FG<->HP-only chart (`traceability_figure`) has been disabled in favor of
# the multi-level genealogy chart below, which now also carries the FG quality/material
# styling and covers the full FG<->HP link on its own (levels 1-2 are always included).
st.subheader(f"Traceability (multi-level genealogy): {STAGE_INFO[genealogy_max_level]['label']} and above")
genealogy_nodes, genealogy_edges = build_genealogy_tables(edges, tables, genealogy_max_level)
if genealogy_nodes.empty:
    st.info("No spools found for the selected level range.")
else:
    st.plotly_chart(genealogy_figure(genealogy_nodes, genealogy_edges, genealogy_max_level), use_container_width=True)
    with st.expander("Genealogy data"):
        st.dataframe(genealogy_nodes.sort_values(["Level", "Spool Time"]), use_container_width=True, hide_index=True)
        st.dataframe(genealogy_edges.sort_values(["Downstream Level", "Downstream Time"]), use_container_width=True, hide_index=True)

st.subheader(f"Process footprint: {FEATURE_LABELS.get(selected_feature, selected_feature)}")
st.plotly_chart(footprint_figure(edges, selected_feature), use_container_width=True)

with st.expander("Traceability data"):
    st.dataframe(edges.sort_values(["FG Production Time", "HP Production Time"]), use_container_width=True)

with st.expander("Spool matching details"):
    st.plotly_chart(matching_figure(tables), use_container_width=True)
    st.dataframe(
        tables["matches"].sort_values("Spool Time"),
        use_container_width=True,
        hide_index=True,
    )
