from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd
import pyodbc
import yaml
from as_sql import query_datakap


@dataclass(frozen=True)
class SynapseConfig:
    server: str = os.getenv("SYNAPSE_SERVER", "azweuprdglosynsqlsrv.database.windows.net")
    database: str = os.getenv("SYNAPSE_DATABASE", "azweuprdbekglobalsynapse")
    username: str = os.getenv("SYNAPSE_USERNAME", "mesdlpaaaiuser")
    password: str = os.getenv("SYNAPSE_PASSWORD", "M3sdlp3Ai@2020")
    driver: str = os.getenv("SYNAPSE_DRIVER", "{ODBC Driver 17 for SQL Server}")


# Absolute path to the shared traceability config (levels, machines, materials).
# See production_config.yaml's `meta.level_convention`: level 1 is the last
# processing step (final goods), and level number increases moving *backwards*
# towards the raw material (e.g. for WIPER_ARM_WIRE: 1=rolling, 2=coating,
# 3=patenting, 4=drawing, 5=pickling).
PRODUCTION_CONFIG_PATH = r"C:\Users\tian.tang\OneDrive - Bekaert\Desktop\KNODI\config\production_config.yaml"
PRODUCTION_CONFIG_PRODUCT = "WIPER_ARM_WIRE"
PRODUCTION_CONFIG_SITE = "BE01"


def _load_production_config(path: str = PRODUCTION_CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _sap_codes(machine_catalog: dict, machine_ids: Iterable[str], site: str) -> tuple[str, ...]:
    codes = []
    for machine_id in machine_ids:
        machine = machine_catalog[machine_id]
        if machine.get("site") == site:
            codes.append(str(machine["aliases"]["sap"]["code"]))
    return tuple(codes)


def _build_process_config(raw_config: dict, product: str, site: str) -> dict:
    product_cfg = raw_config["products"][product]
    machine_catalog = raw_config["machine_catalog"]

    # {1: "rolling", 2: "coating", 3: "patenting", 4: "drawing", 5: "pickling"}
    process_levels = {int(level): step for level, step in product_cfg["process_levels"].items()}
    # ZPP_M06's TCTHIELEV is the level number zero-padded to 4 digits, e.g. {1: "0001", ...}.
    # Used only as a last-resort fallback now -- see matl_group_step below.
    level_tcthielev = {level: f"{level:04d}" for level in process_levels}
    # ZPP_M06's MATL_GROUP -> process step, e.g. {"FG_WPC": "rolling", "WIP_BZ": "coating", ...}.
    # This is the reliable ground-truth field for identifying a trace row's true process
    # step, confirmed against live data (see knodi_pipeline._annotate_process_levels).
    matl_group_step = {
        str(group).strip(): step for group, step in product_cfg["matl_group_process_step"].items()
    }
    # Machines allowed at each process step, e.g. {"rolling": ("47006", "410031"), ...}.
    step_machines = {
        step: _sap_codes(machine_catalog, machine_ids, site)
        for step, machine_ids in product_cfg["step_machine_pool"].items()
    }
    # Same zero-padded 18-digit format the SQL literals previously hardcoded.
    material_codes = [str(code).zfill(18) for code in product_cfg["material_codes"]]

    return {
        "process_levels": process_levels,
        "level_tcthielev": level_tcthielev,
        "matl_group_step": matl_group_step,
        "step_machines": step_machines,
        "material_codes": material_codes,
    }


_PROCESS_CONFIG = _build_process_config(
    _load_production_config(), PRODUCTION_CONFIG_PRODUCT, PRODUCTION_CONFIG_SITE
)

# {1: "rolling", 2: "coating", 3: "patenting", 4: "drawing", 5: "pickling"}
PROCESS_LEVELS = _PROCESS_CONFIG["process_levels"]
# {1: "0001", 2: "0002", 3: "0003", 4: "0004", 5: "0005"}
LEVEL_TCTHIELEV = _PROCESS_CONFIG["level_tcthielev"]
# {"FG_WPC": "rolling", "WIP_BZ": "coating", "WIP_IP": "patenting", "WIP_DD": "drawing",
#  "WIP_PI": "pickling"}
MATL_GROUP_STEP = _PROCESS_CONFIG["matl_group_step"]
# Reverse of PROCESS_LEVELS, e.g. {"rolling": 1, "coating": 2, ...}.
STEP_LEVEL = {step: level for level, step in PROCESS_LEVELS.items()}
# {"FG_WPC": 1, "WIP_BZ": 2, "WIP_IP": 3, "WIP_DD": 4, "WIP_PI": 5}
MATL_GROUP_LEVEL = {group: STEP_LEVEL[step] for group, step in MATL_GROUP_STEP.items()}
# {"rolling": ("47006", "410031"), "coating": ("46157",), "patenting": ("46100P",),
#  "drawing": ("45021", "45020", "45025", "45024"), "pickling": ("45030",)}
STEP_MACHINES = _PROCESS_CONFIG["step_machines"]
MATERIAL_CODES = _PROCESS_CONFIG["material_codes"]

# Finished-goods rolling lines: LZ6 feeds machine 47006, TK31 feeds machine 410031.
LINE_MACHINE_MAP = {
    "LZ6 (47006)": "47006",
    "TK31 (410031)": "410031",
}
DEFAULT_MACHINES = STEP_MACHINES["rolling"]

# Half-product producing lines, e.g. the coating line at location IVD157.
HP_LINE_MACHINE_MAP = {
    "IVD157 (46157)": "46157",
}
DEFAULT_HP_MACHINES = STEP_MACHINES["coating"]

MASTERCHAR_DESCRIPTIONS = {
    "T_CC_CO": "Camera on Test Value (%)",
    "T_CC_HD": "Harmful defects Test Value (%)",
    "T_CC_TD": "Total defects Test Value (%)",
    "T_CO_CW1": "Coating 1 Test Value (G)",
    "T_CO_CW2": "Coating 2 Test Value (G)",
    "T_CO_CWF": "Coating Test Value (G/m2)",
    "T_CO_LS": "Length Test Value (mm)",
    "T_DI_D1": "b1 Test Value (mm)",
    "T_DI_D1U": "Dimens.1_u Test Value (mm)",
    "T_DI_D2": "a1 Test Value (mm)",
    "T_DI_D2U": "Dimens.2_u Test Value (mm)",
    "T_EL_AC": "A Test Value (%)",
    "T_EL_AM": "Lu - delta A Test Value (mm)",
    "T_RM_CP": "Rm Test Value (N/mm2)",
    "T_RP_2P": "Rp 0.2% Test Value (%)",
    "T_RP_I2": "Rp 0.2 Test Value (N/mm2)",
    "T_TE_FM": "Fm Test Value (N)",
    "T_VC_IS": "Visual (c=1 OK) Test Value (ERR)",
    "T_VC_NB": "Bend (1=OK) Test Value (ERR)",
    "T_VC_PI": "Pitch Test Value (mm)",
}


def sql_string_list(values: Iterable[object]) -> str:
    values = [str(value) for value in values]
    if not values:
        raise ValueError("SQL IN lists cannot be empty")
    return ",".join("'" + value.replace("'", "''") + "'" for value in values)


def read_synapse_query(query: str, params=None, config: SynapseConfig | None = None) -> pd.DataFrame:
    config = config or SynapseConfig()
    if not config.password:
        raise RuntimeError("Set SYNAPSE_PASSWORD before querying Synapse.")
    connection_string = (
        f"DRIVER={config.driver};SERVER={config.server};PORT=1433;"
        f"DATABASE={config.database};UID={config.username};PWD={config.password};"
    )
    with pyodbc.connect(connection_string) as connection:
        return pd.read_sql_query(query, connection, params=params)


def normalize_id(value: object, suffix_length: int | None = None) -> str:
    text = str(value).strip()
    if suffix_length is not None:
        return text[-suffix_length:]
    return text.lstrip("0") or "0"


def as_dates(production_dates: Iterable[object]) -> list[pd.Timestamp]:
    dates = pd.to_datetime(list(production_dates), errors="coerce")
    dates = pd.DatetimeIndex(dates).dropna().normalize().unique()
    if len(dates) == 0:
        raise ValueError("production_dates contains no valid dates")
    return sorted(pd.Timestamp(date) for date in dates)


def _query_production_units(production_date, machines, materials=None, config=None) -> pd.DataFrame:
    """Fetch production-unit (spool) records from ZPP_M08 for one date and set of machines.

    [_-BIC_-ZPRD_UNIT] is the spool id, equivalent to [_-BIC_-ZSPLID]/[_-BIC_-ZFGSPLID] in
    ZPP_M06. Pass materials=None to skip the MATERIAL filter entirely (half-product
    machines are not restricted to MATERIAL_CODES).
    """
    filters = ["plant = 'BE01'", f"[WORKCENTER] IN ({sql_string_list(machines)})", "[CALDAY] = ?"]
    if materials:
        filters.insert(1, f"MATERIAL IN ({sql_string_list(materials)})")
    query = f"""
        SELECT [_-BIC_-ZPRD_UNIT],[_-BIC_-ZBAT_CLSF],
               [WORKCENTER], [MATERIAL], [_-BIC_-ZPU_LOC],[_-BIC_-ZPU_RDES],[_-BIC_-ZPU_RCOM],
               [MATL_GROUP],[MATL_TYPE],[_-BIC_-ZPUSTATUS],[CALDAY]
        FROM [SAP_CL].[ZPP_M08]
        WHERE {" AND ".join(filters)}
        ORDER BY [WORKCENTER], [_-BIC_-ZBAT_CLSF],[_-BIC_-ZPRD_UNIT]
    """
    return read_synapse_query(query, params=[pd.Timestamp(production_date).strftime("%Y-%m-%d")], config=config)


def query_production_data(production_date, machines=None, config=None) -> pd.DataFrame:
    """Fetch final-goods production units for one date, optionally limited to specific lines."""
    machines = tuple(machines) if machines else DEFAULT_MACHINES
    return _query_production_units(production_date, machines=machines, materials=MATERIAL_CODES, config=config)


def query_hp_production_data(hp_production_date, machines=None, config=None) -> pd.DataFrame:
    """Fetch half-product production units directly by the HP's own machine/date.

    Unlike query_production_data, this is not filtered by MATERIAL_CODES or by an
    already-known FG spool: it returns every production unit made on this machine/date,
    whether or not it has been consumed by a final-goods spool yet.
    """
    machines = tuple(machines) if machines else DEFAULT_HP_MACHINES
    return _query_production_units(hp_production_date, machines=machines, materials=None, config=config)


def query_children_fg_data(hp_splids, config=None) -> pd.DataFrame:
    """Fetch the FG-level rows that consumed the given raw HP spool IDs.

    Matches either _-BIC_-ZCSPLID (consumed spool id) or _-BIC_-ZSPLID (produced
    spool id) against the raw HP spool IDs, since it is not certain up front which
    field carries the link for a given genealogy row. Filtered to MATERIAL_CODES,
    same as the FG-anchored query, since untracked materials are never relevant here.
    """
    if not hp_splids:
        return pd.DataFrame(columns=[
            "MATERIAL", "MATL_GROUP", "_-BIC_-ZTRMACH", "_-BIC_-ZSPLID",
            "CREATEDON", "CREA_TIME", "_-BIC_-ZCSPLID", "_-BIC_-ZFGSPLID",
            "_-BIC_-ZDOFFNO", "PRODORDER", "BATCH",
        ])
    query = f"""
        SELECT distinct [MATERIAL], [MATL_GROUP], [_-BIC_-ZTRMACH], [_-BIC_-ZSPLID],
               [CREATEDON], [CREA_TIME], [_-BIC_-ZCSPLID], [_-BIC_-ZFGSPLID],
               [_-BIC_-ZDOFFNO], [PRODORDER], [BATCH]
        FROM [SAP_CL].[ZPP_M06]
        WHERE plant = 'BE01'
          AND MATERIAL IN ({sql_string_list(MATERIAL_CODES)})
          AND [TCTHIELEV] = '0001'
          AND (
                [_-BIC_-ZCSPLID] IN ({sql_string_list(hp_splids)})
             OR [_-BIC_-ZSPLID] IN ({sql_string_list(hp_splids)})
          )
    """
    return read_synapse_query(query, config=config)


def query_quality_data(final_spool_ids, config=None) -> pd.DataFrame:
    empty_columns = ["BATCH",
        "_-BIC_-ZUSERC1", "MASTERCHAR", "SUM_XBAR", "ICHAR_UOM",
        "INSP_CHAR", "LWR_LIMIT", "UPPR_LIMIT", "MASTERCHAR Explanation",
    ]
    if not final_spool_ids:
        return pd.DataFrame(columns=empty_columns)
    quality_ids = [str(value)[2:] for value in final_spool_ids]
    query = f"""
        SELECT [_-BIC_-ZUSERC1], [MASTERCHAR], [SUM_XBAR], [ICHAR_UOM],
               [INSP_CHAR], [LWR_LIMIT], [UPPR_LIMIT]
        FROM [SAP_CL].[ZQM_O6DT_1]
        WHERE [_-BIC_-ZUSERC1] IN ({sql_string_list(quality_ids)})
          AND [_-BIC_-ZSERIALNR] IS NOT NULL
        ORDER BY [_-BIC_-ZUSERC1]
    """
    result = read_synapse_query(query, config=config)
    result["MASTERCHAR Explanation"] = (
        result["MASTERCHAR"].astype("string").str.strip()
        .map(MASTERCHAR_DESCRIPTIONS).fillna("Unknown MASTERCHAR")
    )
    return result


def _annotate_process_levels(trace: pd.DataFrame) -> pd.DataFrame:
    """Map every trace row to its canonical PROCESS_LEVELS number and step name using
    MATL_GROUP, e.g. FG_WPC=rolling, WIP_BZ=coating, WIP_IP=patenting, WIP_DD=drawing,
    WIP_PI=pickling (see MATL_GROUP_STEP / production_config.yaml).

    MATL_GROUP is the reliable ground-truth process-step indicator for a trace row --
    confirmed against live ZPP_M06 data, cross-checked against the row's own production
    machine (_-BIC_-ZTRMACH). It does NOT depend on TCTHIELEV's numbering, which turned
    out to be unsafe to treat as either a fixed level code or a simple per-chain-relative
    counter: real chains can be missing intermediate levels (material scrapped partway)
    AND can have one step repeated more than once in the same chain (e.g. re-patenting),
    and TCTHIELEV alone cannot be reliably decoded in either case. MATL_GROUP is
    unaffected by both and always names the true step for that specific row.

    A given root spool ID can legitimately be shared (via an ID collision, or a chain
    that splits mid-genealogy) with a row that belongs to a DIFFERENT tracked product
    (e.g. this dashboard is configured for WIPER_ARM_WIRE, but MATL_GROUP carries a
    value like "FG_AUW" or "WIP_DO" from an entirely different product's own process
    steps). Those rows are real data, just not part of THIS product's configured
    levels, so they're labeled "other_product" (Process Level left NA) rather than
    silently coerced into one of our five levels -- letting the dashboard show/gray
    them out distinctly instead of mislabeling them as a genuine step of this product.

    Falls back to treating the raw TCTHIELEV code as a literal fixed level (1=rolling,
    ..., 5=pickling) only when MATL_GROUP itself is blank/missing (a genuine data-quality
    gap within this product's own rows) -- never for a real but unrecognized value,
    since that reliably signals a different product, not a data gap.
    """
    result = trace.copy()
    if result.empty:
        result["Process Level"] = pd.Series(dtype="Int64")
        result["Process Step"] = pd.Series(dtype="string")
        return result
    matl_group = result["MATL_GROUP"].astype("string").str.strip()
    result["Process Step"] = matl_group.map(MATL_GROUP_STEP)
    result["Process Level"] = matl_group.map(MATL_GROUP_LEVEL).astype("Int64")

    blank = matl_group.isna() | matl_group.eq("")
    unmapped = result["Process Level"].isna()

    missing_data = unmapped & blank
    if missing_data.any():
        fallback_level = pd.to_numeric(
            result.loc[missing_data, "TCTHIELEV"].astype("string").str.strip(), errors="coerce"
        ).astype("Int64")
        result.loc[missing_data, "Process Level"] = fallback_level
        result.loc[missing_data, "Process Step"] = fallback_level.map(PROCESS_LEVELS)

    other_product = unmapped & ~blank
    result.loc[other_product, "Process Step"] = "other_product"
    return result



def query_trace_data(final_spool_ids, levels=None, config=None) -> pd.DataFrame:
    """Fetch every traceability row for the given root FG spool IDs and annotate each
    row with its true canonical process level/step (see _annotate_process_levels, which
    uses MATL_GROUP as ground truth, not TCTHIELEV).

    Always fetches every row present for these chains -- filtering by TCTHIELEV up
    front is not safe, since it is not a reliable fixed level code (see
    _annotate_process_levels). Pass an explicit `levels` iterable (e.g. [1, 2]) to
    narrow the RETURNED rows to those canonical levels after computing the per-row
    mapping; the fetch itself is always unfiltered.
    """
    if not final_spool_ids:
        return pd.DataFrame(columns=[
            "MATERIAL", "MATL_GROUP", "_-BIC_-ZTRMACH", "_-BIC_-ZSPLID",
            "CREATEDON", "CREA_TIME", "_-BIC_-ZCSPLID", "_-BIC_-ZFGSPLID",
            "_-BIC_-ZDOFFNO", "PRODORDER", "BATCH", "TCTHIELEV", "Process Level", "Process Step",
        ])
    query = f"""
        SELECT distinct [MATERIAL], [MATL_GROUP], [_-BIC_-ZTRMACH], [_-BIC_-ZSPLID],
               [CREATEDON], [CREA_TIME], [_-BIC_-ZCSPLID], [_-BIC_-ZFGSPLID],
               [_-BIC_-ZDOFFNO], [PRODORDER], [BATCH], [TCTHIELEV]
        FROM [SAP_CL].[ZPP_M06]
        WHERE plant = 'BE01'
          AND [_-BIC_-ZFGSPLID] IN ({sql_string_list(final_spool_ids)})
    """
    result = _annotate_process_levels(read_synapse_query(query, config=config))
    if levels is not None:
        allowed = set(levels)
        result = result[result["Process Level"].isin(allowed)].copy()
    return result


def _empty_level_trace_slice() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "Spool ID", "FG Spool ID", "Consumes Spool ID", "Spool Time", "Prod Order", "BATCH",
        "MATERIAL", "MATL_GROUP", "Machine", "Process Step",
    ])


def _level_trace_slice(trace: pd.DataFrame, level: int, id_suffix_length: int | None = None) -> pd.DataFrame:
    """Extract one canonical traceability level (1=rolling/FG .. 5=pickling) out of a
    query_trace_data result, deriving a real CREATEDON+CREA_TIME timestamp for that
    level's spool id.

    This is how the trace table compensates for ZPP_M08 only carrying a date (CALDAY)
    and no time-of-day: ZPP_M06's CREATEDON/CREA_TIME give an actual timestamp per level.

    Also carries "Consumes Spool ID" (raw _-BIC_-ZCSPLID, normalized) \u2014 the *parent*
    spool (one level deeper, e.g. HP's patenting spool) that was consumed to produce
    this row's own spool. This is what lets the dashboard draw genuine spool-to-spool
    consumption arrows between adjacent process levels, instead of just grouping
    everything under the shared root FG Spool ID.
    """
    if trace.empty:
        return _empty_level_trace_slice()
    if "Process Level" not in trace.columns:
        trace = _annotate_process_levels(trace)
    subset = trace[trace["Process Level"].eq(level)].copy()
    if subset.empty:
        return _empty_level_trace_slice()
    subset["Spool ID"] = subset["_-BIC_-ZSPLID"].map(
        lambda value: normalize_id(value, suffix_length=id_suffix_length)
    )
    subset["FG Spool ID"] = subset["_-BIC_-ZFGSPLID"].map(normalize_id)
    subset["Consumes Spool ID"] = subset["_-BIC_-ZCSPLID"].map(
        lambda value: normalize_id(value) if pd.notna(value) and str(value).strip() else pd.NA
    )
    subset["Spool Time"] = pd.to_datetime(
        subset["CREATEDON"].astype(str) + " " + subset["CREA_TIME"].astype(str), errors="coerce"
    )
    subset["Process Step"] = PROCESS_LEVELS.get(level)
    subset = subset.rename(columns={"PRODORDER": "Prod Order", "_-BIC_-ZTRMACH": "Machine"})
    return subset[[
        "Spool ID", "FG Spool ID", "Consumes Spool ID", "Spool Time", "Prod Order", "BATCH",
        "MATERIAL", "MATL_GROUP", "Machine", "Process Step",
    ]].drop_duplicates()


def _other_product_slice(trace: pd.DataFrame, id_suffix_length: int | None = None) -> pd.DataFrame:
    """Extract every trace row that belongs to a genuinely different, unconfigured
    product (Process Step == "other_product", see _annotate_process_levels) -- e.g. the
    material at some point got diverted into another finished-goods line, or an
    unrelated product's row got pulled in via a shared/collided spool ID. These rows
    are real, just not part of this dashboard's configured product levels, so they're
    kept in their own slice (same shape as _level_trace_slice) rather than dropped, so
    the dashboard can still show/gray them out instead of silently losing the link.
    """
    if trace.empty:
        return _empty_level_trace_slice()
    if "Process Level" not in trace.columns:
        trace = _annotate_process_levels(trace)
    subset = trace[trace["Process Step"].eq("other_product")].copy()
    if subset.empty:
        return _empty_level_trace_slice()
    subset["Spool ID"] = subset["_-BIC_-ZSPLID"].map(
        lambda value: normalize_id(value, suffix_length=id_suffix_length)
    )
    subset["FG Spool ID"] = subset["_-BIC_-ZFGSPLID"].map(normalize_id)
    subset["Consumes Spool ID"] = subset["_-BIC_-ZCSPLID"].map(
        lambda value: normalize_id(value) if pd.notna(value) and str(value).strip() else pd.NA
    )
    subset["Spool Time"] = pd.to_datetime(
        subset["CREATEDON"].astype(str) + " " + subset["CREA_TIME"].astype(str), errors="coerce"
    )
    # Process Step is kept as the raw MATL_GROUP code (e.g. "FG_AUW", "WIP_DO") instead
    # of the literal "other_product" marker, so the dashboard can surface which foreign
    # product/process group is actually involved.
    subset["Process Step"] = subset["MATL_GROUP"].astype("string").str.strip()
    subset = subset.rename(columns={"PRODORDER": "Prod Order", "_-BIC_-ZTRMACH": "Machine"})
    return subset[[
        "Spool ID", "FG Spool ID", "Consumes Spool ID", "Spool Time", "Prod Order", "BATCH",
        "MATERIAL", "MATL_GROUP", "Machine", "Process Step",
    ]].drop_duplicates()


def _deeper_level_tables(trace: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Traceability slices for every configured level below coating (patenting, drawing,
    pickling, ...), keyed by the process-step name from production_config.yaml, plus an
    "other_product" slice for any row belonging to a different, unconfigured product.
    """
    return {
        "other_product": _other_product_slice(trace),
        **{
            PROCESS_LEVELS[level]: _level_trace_slice(trace, level)
            for level in PROCESS_LEVELS
            if level >= 3
        },
    }


def query_weight_data(half_product_ids, config=None) -> pd.DataFrame:
    if not half_product_ids:
        return pd.DataFrame(columns=[
            "_-BIC_-ZPRD_UNIT", "_-BIC_-ZPU_QTY", "_-BIC_-ZCDIAMETR",
            "WORKCENTER", "MATERIAL", "PLANT",
        ])
    query = f"""
        SELECT [_-BIC_-ZPRD_UNIT], [_-BIC_-ZPU_QTY], [_-BIC_-ZCDIAMETR],
               [WORKCENTER], [MATERIAL], [PLANT]
        FROM [SAP_CL].[ZPP_M08]
        WHERE plant = 'BE01'
          AND [_-BIC_-ZPRD_UNIT] IN ({sql_string_list(half_product_ids)})
    """
    return read_synapse_query(query, config=config)


def to_m06_spool_id(value: object) -> str:
    """Reconstruct the 20-char zero-padded _-BIC_-ZSPLID/_-BIC_-ZFGSPLID format ZPP_M06
    stores IDs in, from any input shape (already-padded 20-char, normalized/short digits,
    with or without leading zeros)."""
    digits = str(value).strip().lstrip("0") or "0"
    return digits.zfill(20)


def prepare_spool_tables_from_fg_ids(final_ids, config=None) -> dict[str, pd.DataFrame]:
    """Build the full FG-anchored trace/weight/footprint-input tables for an EXPLICIT set of
    root final-goods spool IDs, regardless of how those IDs were found (production
    date+machine, batch number, spool ID lookup, wire-rod raw-material batch, or a
    deeper-level date+machine anchor). This is the shared backend every FG-resolving entry
    point below funnels into once it knows which FG spools to analyze.
    """
    final_ids = sorted({normalize_id(value) for value in final_ids if pd.notna(value) and str(value).strip()})
    if not final_ids:
        raise ValueError("No final-goods spool IDs resolved from the given input.")
    padded_ids = [to_m06_spool_id(value) for value in final_ids]

    quality = query_quality_data(padded_ids, config=config)
    trace = query_trace_data(padded_ids, config=config).drop_duplicates()
    # Process Level is the per-chain-corrected canonical level (see
    # _annotate_process_levels) -- NOT the raw TCTHIELEV code, which is only chain-
    # relative and can point at a different step when a chain is scrapped partway.
    level_two = trace[trace["Process Level"].eq(2)].copy()
    level_two["FG Spool ID"] = level_two["_-BIC_-ZFGSPLID"].map(normalize_id)
    level_two["HP Spool ID"] = level_two["_-BIC_-ZSPLID"].map(
        lambda value: normalize_id(value, suffix_length=10)
    )
    # The parent (patenting) spool this HP row consumed, for chaining deeper-level arrows.
    level_two["Consumes Spool ID"] = level_two["_-BIC_-ZCSPLID"].map(
        lambda value: normalize_id(value) if pd.notna(value) and str(value).strip() else pd.NA
    )

    hp_times = level_two[["HP Spool ID", "CREATEDON", "CREA_TIME", "PRODORDER", "_-BIC_-ZTRMACH", "Consumes Spool ID", "BATCH"]].rename(
        columns={"PRODORDER": "HP Batch", "_-BIC_-ZTRMACH": "HP Machine"}
    ).copy()
    hp_times["Spool Time"] = pd.to_datetime(
        hp_times["CREATEDON"].astype(str) + " " + hp_times["CREA_TIME"].astype(str),
        errors="coerce",
    )
    hp_times = hp_times[["HP Spool ID", "Spool Time", "HP Batch", "HP Machine", "Consumes Spool ID", "BATCH"]].dropna(
        subset=["Spool Time"]
    ).drop_duplicates("HP Spool ID", keep="first")
    hp_ids = level_two["HP Spool ID"].dropna().unique().tolist()
    weights = query_weight_data(hp_ids, config=config)
    weights["HP Spool ID"] = weights["_-BIC_-ZPRD_UNIT"].map(normalize_id)
    weights["Spool Weight (kg)"] = pd.to_numeric(weights["_-BIC_-ZPU_QTY"], errors="coerce")
    weights["Spool Diameter (mm)"] = pd.to_numeric(weights["_-BIC_-ZCDIAMETR"], errors="coerce")
    area = math.pi * (weights["Spool Diameter (mm)"] / 1000) ** 2 / 4
    weights["Spool Length (m)"] = weights["Spool Weight (kg)"] / (7850 * area)
    hp = (
        weights.drop_duplicates("HP Spool ID", keep="last")
        .merge(hp_times, on="HP Spool ID", how="left")
    )

    # Levels below coating (patenting, drawing, pickling, ...) per production_config.yaml.
    deeper_levels = _deeper_level_tables(trace)
    # Level-1 (rolling/FG) slice with real CREATEDON+CREA_TIME timestamp from the trace
    # table, since ZPP_M08 only carries CALDAY (date-only).
    fg_trace = _level_trace_slice(trace, 1)

    return {
        "quality": quality,
        "traceability": trace,
        "trace_edges": level_two[["FG Spool ID", "HP Spool ID"]],
        "half_products": hp,
        "fg_trace": fg_trace,
        **deeper_levels,
        "diagnostics": pd.DataFrame([{
            "Final spool IDs": len(final_ids),
            "Quality rows": len(quality),
            "Traceability rows": len(trace),
            "Traceability edges": len(level_two),
            "Unique HP IDs in trace": level_two["HP Spool ID"].nunique(),
            "Weight rows": len(weights),
            **{f"{step.capitalize()} rows": len(df) for step, df in deeper_levels.items()},
        }]),
    }


def prepare_spool_tables(production_dates, machines=None, config=None) -> dict[str, pd.DataFrame]:
    dates = as_dates(production_dates)
    production = pd.concat(
        [query_production_data(date, machines=machines, config=config) for date in dates],
        ignore_index=True,
    ).drop_duplicates()
    if production.empty:
        raise ValueError("No production rows found for the selected dates.")

    final_ids = production["_-BIC_-ZPRD_UNIT"].dropna().unique().tolist()
    result = prepare_spool_tables_from_fg_ids(final_ids, config=config)
    result["production"] = production
    result["diagnostics"]["Production rows"] = len(production)
    return result


def query_level_production_data(level, production_date, machines=None, config=None) -> pd.DataFrame:
    """Fetch production units directly at ANY configured traceability level (e.g. 3=patenting,
    4=drawing, 5=pickling) for one date/machine set, straight from ZPP_M06's real
    CREATEDON+CREA_TIME timestamp (unlike ZPP_M08, which only has a date-only CALDAY).

    Every row already carries the root FG spool ID (_-BIC_-ZFGSPLID), so anchoring on a
    deeper level needs no extra resolution step before handing off to
    prepare_spool_tables_from_fg_ids.
    """
    step = PROCESS_LEVELS[level]
    machines = tuple(machines) if machines else STEP_MACHINES[step]
    empty_columns = [
        "MATERIAL", "MATL_GROUP", "_-BIC_-ZTRMACH", "_-BIC_-ZSPLID", "CREATEDON",
        "CREA_TIME", "_-BIC_-ZCSPLID", "_-BIC_-ZFGSPLID", "_-BIC_-ZDOFFNO",
        "PRODORDER", "BATCH", "TCTHIELEV",
    ]
    if not machines:
        return pd.DataFrame(columns=empty_columns)
    # No TCTHIELEV filter here: TCTHIELEV is a per-chain relative counter (see
    # _annotate_process_levels), not a fixed process-step id, so a literal '000N' code
    # would wrongly exclude genuine `step` rows from partial/scrapped chains where that
    # step happens to land on a different local TCTHIELEV number. The machine set
    # already unambiguously identifies the process step, since STEP_MACHINES per step
    # are disjoint -- so machine + date alone is sufficient here.
    query = f"""
        SELECT [MATERIAL], [MATL_GROUP], [_-BIC_-ZTRMACH], [_-BIC_-ZSPLID],
               [CREATEDON], [CREA_TIME], [_-BIC_-ZCSPLID], [_-BIC_-ZFGSPLID],
               [_-BIC_-ZDOFFNO], [PRODORDER], [BATCH], [TCTHIELEV]
        FROM [SAP_CL].[ZPP_M06]
        WHERE plant = 'BE01'
          AND [_-BIC_-ZTRMACH] IN ({sql_string_list(machines)})
          AND [CREATEDON] = ?
    """
    return read_synapse_query(
        query, params=[pd.Timestamp(production_date).strftime("%Y-%m-%d")], config=config
    )


def resolve_fg_ids_from_level_anchor(level, production_dates, machines=None, config=None) -> list[str]:
    """Anchor mode: date range + machine at an arbitrary level (3=patenting, 4=drawing,
    5=pickling). Resolves straight to root FG spool IDs via the level rows' own
    _-BIC_-ZFGSPLID column."""
    dates = as_dates(production_dates)
    rows = pd.concat(
        [query_level_production_data(level, date, machines=machines, config=config) for date in dates],
        ignore_index=True,
    ).drop_duplicates()
    if rows.empty:
        return []
    return rows["_-BIC_-ZFGSPLID"].dropna().map(normalize_id).unique().tolist()


def query_spools_by_batch_class(batch_classes, config=None) -> pd.DataFrame:
    """Resolve one or more batch-classification codes (_-BIC_-ZBAT_CLSF, ZPP_M08) to their
    production-unit spool IDs, irrespective of process level/workcenter."""
    empty_columns = ["_-BIC_-ZPRD_UNIT", "_-BIC_-ZBAT_CLSF", "WORKCENTER", "MATERIAL", "CALDAY"]
    if not batch_classes:
        return pd.DataFrame(columns=empty_columns)
    query = f"""
        SELECT [_-BIC_-ZPRD_UNIT], [_-BIC_-ZBAT_CLSF], [WORKCENTER], [MATERIAL], [CALDAY]
        FROM [SAP_CL].[ZPP_M08]
        WHERE plant = 'BE01'
          AND [_-BIC_-ZBAT_CLSF] IN ({sql_string_list(batch_classes)})
    """
    return read_synapse_query(query, config=config)


def resolve_root_fg_ids(spool_ids, level=None, config=None) -> list[str]:
    """Resolve spool IDs at a given traceability level to their root final-goods spool IDs
    via ZPP_M06's _-BIC_-ZFGSPLID.

    IMPORTANT: _-BIC_-ZSPLID values are only unique *within* a level, not globally -- the
    same numeric ID can independently occur as e.g. a patenting spool in one genealogy
    chain and a coating spool in a completely unrelated chain (confirmed against live
    ZPP_M06 data). Always pass `level` when it is known (i.e. whenever the caller knows
    which stage the pasted spool IDs came from) to avoid resolving to unrelated FG chains.
    Only omit `level` (matching any canonical level) when the level is genuinely unknown,
    and be aware the result may then include false-positive FG IDs from an ID collision.

    NOTE: cannot filter by a literal TCTHIELEV code up front, since that code is only
    chain-relative (see _annotate_process_levels) -- the same physical process step can
    carry a different TCTHIELEV number depending on how many levels its own chain has.
    Instead: find every chain that contains one of the given spool ids at all, fetch each
    chain's FULL trace, compute each row's true canonical level, then keep only the
    chains where the given spool id actually lands on `level`.
    """
    ids = [str(value).strip() for value in spool_ids if pd.notna(value) and str(value).strip()]
    if not ids:
        return []
    padded = [to_m06_spool_id(value) for value in ids]
    candidate_query = f"""
        SELECT DISTINCT [_-BIC_-ZFGSPLID]
        FROM [SAP_CL].[ZPP_M06]
        WHERE plant = 'BE01'
          AND [_-BIC_-ZSPLID] IN ({sql_string_list(padded)})
    """
    candidates = read_synapse_query(candidate_query, config=config)
    if candidates.empty:
        return []
    candidate_fg_ids = candidates["_-BIC_-ZFGSPLID"].dropna().unique().tolist()
    if level is None:
        return candidates["_-BIC_-ZFGSPLID"].dropna().map(normalize_id).unique().tolist()

    # NOTE: some of these candidate "_-BIC_-ZFGSPLID" values are only placeholders for
    # chains that were scrapped before ever reaching a genuine rolling/FG spool (Process
    # Level 1 never appears in their trace). Those chains are still kept here on purpose
    # -- the user wants scrapped/partial chains visible in the traceability genealogy --
    # but they carry no real FG production/quality data, which downstream code (the
    # footprint plot) must flag distinctly instead of quietly mislabeling as "Unknown".
    trace = query_trace_data(candidate_fg_ids, config=config)
    if trace.empty:
        return []
    matched_splids = {normalize_id(value) for value in ids}
    row_splid = trace["_-BIC_-ZSPLID"].map(normalize_id)
    matches = trace[row_splid.isin(matched_splids) & trace["Process Level"].eq(level)]
    if matches.empty:
        return []
    return matches["_-BIC_-ZFGSPLID"].dropna().map(normalize_id).unique().tolist()


def resolve_root_fg_ids_by_batch_class(batch_classes, level=None, config=None) -> list[str]:
    """Anchor mode: one or more ZPP_M08 batch-classification codes (_-BIC_-ZBAT_CLSF).
    Pass `level` to disambiguate spool ID collisions across levels, same caveat as
    resolve_root_fg_ids."""
    spools = query_spools_by_batch_class(batch_classes, config=config)
    if spools.empty:
        return []
    if level is not None:
        allowed_machines = set(STEP_MACHINES[PROCESS_LEVELS[level]])
        spools = spools[spools["WORKCENTER"].astype("string").str.strip().isin(allowed_machines)]
        if spools.empty:
            return []
    return resolve_root_fg_ids(spools["_-BIC_-ZPRD_UNIT"].dropna().unique().tolist(), level=level, config=config)


def resolve_root_fg_ids_by_wire_rod_batch(batch_values, config=None) -> list[str]:
    """Anchor mode: raw wire-rod batch/heat number(s). ZPP_M06's BATCH field carries the
    raw-material identifier and stays constant across every traceability level of one FG
    chain, so a single lookup resolves straight to the root FG spool IDs."""
    values = [str(value).strip() for value in batch_values if str(value).strip()]
    if not values:
        return []
    query = f"""
        SELECT DISTINCT [_-BIC_-ZFGSPLID]
        FROM [SAP_CL].[ZPP_M06]
        WHERE plant = 'BE01'
          AND [BATCH] IN ({sql_string_list(values)})
    """
    result = read_synapse_query(query, config=config)
    if result.empty:
        return []
    return result["_-BIC_-ZFGSPLID"].dropna().map(normalize_id).unique().tolist()


def prepare_spool_tables_from_hp(hp_production_dates, machines=None, config=None) -> dict[str, pd.DataFrame]:
    """Start from half-product production dates/machine and trace forward to children FG spools.

    HPs may not have been consumed yet, or may feed products outside MATERIAL_CODES.
    In both cases the HP is still returned (weights, times, batch) but with unknown
    FG linkage/quality, instead of raising an error.
    """
    machines = tuple(machines) if machines else DEFAULT_HP_MACHINES
    dates = as_dates(hp_production_dates)
    hp_level = pd.concat(
        [query_hp_production_data(date, machines=machines, config=config) for date in dates],
        ignore_index=True,
    ).drop_duplicates()
    if hp_level.empty:
        raise ValueError("No half-product production rows found for the selected dates/machine.")

    hp_level["HP Spool ID"] = hp_level["_-BIC_-ZPRD_UNIT"].map(normalize_id)
    hp_splids = hp_level["_-BIC_-ZPRD_UNIT"].dropna().unique().tolist()
    hp_splids = ['0000000000'+i for i in hp_splids]

    children = query_children_fg_data(hp_splids, config=config).drop_duplicates()
    tracked_materials = {normalize_id(code) for code in MATERIAL_CODES}
    if not children.empty:
        children["MATERIAL"] = children["MATERIAL"].astype("string").str.strip()
        children = children[children["MATERIAL"].map(normalize_id).isin(tracked_materials)]

    empty_trace_columns = [
        "MATERIAL", "MATL_GROUP", "_-BIC_-ZTRMACH", "_-BIC_-ZSPLID", "CREATEDON",
        "CREA_TIME", "_-BIC_-ZCSPLID", "_-BIC_-ZFGSPLID", "_-BIC_-ZDOFFNO",
        "PRODORDER", "BATCH", "TCTHIELEV",
    ]
    if children.empty:
        # None of the queried HPs have been consumed by a tracked-material FG yet.
        production = pd.DataFrame(columns=empty_trace_columns)
        quality = pd.DataFrame(columns=["_-BIC_-ZUSERC1", "MASTERCHAR", "SUM_XBAR", "MASTERCHAR Explanation"])
        trace = pd.DataFrame(columns=empty_trace_columns)
        trace_edges = pd.DataFrame({"FG Spool ID": pd.Series(dtype="string"), "HP Spool ID": hp_level["HP Spool ID"]})
        precise_hp_times = pd.DataFrame(columns=["HP Spool ID", "Spool Time", "HP Batch", "HP Machine", "Consumes Spool ID", "BATCH"])
        deeper_levels = {step: _empty_level_trace_slice() for step in PROCESS_LEVELS.values() if step not in ("rolling", "coating")}
        fg_trace = _empty_level_trace_slice()
    else:
        root_fg_ids = children["_-BIC_-ZSPLID"].dropna().unique().tolist()
        quality = query_quality_data(root_fg_ids, config=config)
        trace = query_trace_data(root_fg_ids, config=config).drop_duplicates()
        # Process Level is the per-chain-corrected canonical level (see
        # _annotate_process_levels) -- NOT the raw TCTHIELEV code, which is only chain-
        # relative and can point at a different step when a chain is scrapped partway.
        production = trace[trace["Process Level"].eq(1)].copy()
        production = production[production["MATERIAL"].map(normalize_id).isin(tracked_materials)].drop_duplicates()

        level_two = trace[trace["Process Level"].eq(2)].copy()
        level_two["FG Spool ID"] = level_two["_-BIC_-ZFGSPLID"].map(normalize_id)
        level_two["HP Spool ID"] = level_two["_-BIC_-ZSPLID"].map(
            lambda value: normalize_id(value, suffix_length=10)
        )
        # The parent (patenting) spool this HP row consumed, for chaining deeper-level arrows.
        level_two["Consumes Spool ID"] = level_two["_-BIC_-ZCSPLID"].map(
            lambda value: normalize_id(value) if pd.notna(value) and str(value).strip() else pd.NA
        )
        queried_hp_ids = set(hp_level["HP Spool ID"])
        tracked_fg_ids = set(production["_-BIC_-ZSPLID"].map(normalize_id))
        linked_edges = level_two.loc[
            level_two["HP Spool ID"].isin(queried_hp_ids) & level_two["FG Spool ID"].isin(tracked_fg_ids),
            ["FG Spool ID", "HP Spool ID"],
        ].drop_duplicates()

        # Keep queried HPs that turned out unconsumed/untracked, with unknown FG linkage.
        unlinked_hp_ids = queried_hp_ids - set(linked_edges["HP Spool ID"])
        unlinked_edges = pd.DataFrame({
            "FG Spool ID": pd.Series([pd.NA] * len(unlinked_hp_ids), dtype="string"),
            "HP Spool ID": list(unlinked_hp_ids),
        })
        trace_edges = pd.concat([linked_edges, unlinked_edges], ignore_index=True)

        # Real CREATEDON+CREA_TIME timestamp from the trace table (ZPP_M06), for whichever
        # queried HPs turned out to be linked to a tracked FG (i.e. have a trace row at all).
        precise_hp_times = level_two[["HP Spool ID", "CREATEDON", "CREA_TIME", "PRODORDER", "_-BIC_-ZTRMACH", "Consumes Spool ID", "BATCH"]].rename(
            columns={"PRODORDER": "HP Batch", "_-BIC_-ZTRMACH": "HP Machine"}
        ).copy()
        precise_hp_times["Spool Time"] = pd.to_datetime(
            precise_hp_times["CREATEDON"].astype(str) + " " + precise_hp_times["CREA_TIME"].astype(str),
            errors="coerce",
        )
        precise_hp_times = precise_hp_times[["HP Spool ID", "Spool Time", "HP Batch", "HP Machine", "Consumes Spool ID", "BATCH"]].dropna(
            subset=["Spool Time"]
        ).drop_duplicates("HP Spool ID", keep="first")

        # Levels below coating (patenting, drawing, pickling, ...) per production_config.yaml.
        deeper_levels = _deeper_level_tables(trace)
        # Level-1 (rolling/FG) slice with real CREATEDON+CREA_TIME timestamp from the trace
        # table, mirroring the FG-anchored path's fg_trace so the dashboard can use one key
        # regardless of which entry point produced the tables.
        fg_trace = _level_trace_slice(trace, 1)

    # ZPP_M08 only carries a calendar day, not a time-of-day. Use it only as a fallback for
    # HPs with no trace row at all (i.e. genuinely unconsumed/untracked so far), and prefer
    # the trace-derived timestamp above (precise_hp_times) whenever one is available.
    fallback_hp_times = hp_level[["HP Spool ID", "CALDAY", "_-BIC_-ZBAT_CLSF", "WORKCENTER"]].rename(
        columns={"_-BIC_-ZBAT_CLSF": "HP Batch", "WORKCENTER": "HP Machine"}
    ).copy()
    fallback_hp_times["Spool Time"] = pd.to_datetime(fallback_hp_times["CALDAY"], errors="coerce")
    # ZPP_M08 (the fallback source) carries no genealogy pointer to the parent patenting spool
    # nor the raw wire-rod BATCH (that field only exists in ZPP_M06's trace rows).
    fallback_hp_times["Consumes Spool ID"] = pd.NA
    fallback_hp_times["BATCH"] = pd.NA
    fallback_hp_times = fallback_hp_times[["HP Spool ID", "Spool Time", "HP Batch", "HP Machine", "Consumes Spool ID", "BATCH"]].dropna(
        subset=["Spool Time"]
    ).drop_duplicates("HP Spool ID", keep="first")

    precise_hp_times = precise_hp_times.assign(**{"Timestamp Source": "trace (ZPP_M06 CREATEDON+CREA_TIME)"})
    fallback_hp_times = fallback_hp_times.assign(**{"Timestamp Source": "ZPP_M08 CALDAY (date-only, no time-of-day)"})
    non_empty_time_frames = [df for df in (precise_hp_times, fallback_hp_times) if not df.empty]
    hp_times = (
        pd.concat(non_empty_time_frames, ignore_index=True) if non_empty_time_frames else precise_hp_times
    ).drop_duplicates("HP Spool ID", keep="first")

    hp_ids = hp_level["HP Spool ID"].dropna().unique().tolist()
    weights = query_weight_data(hp_ids, config=config)
    weights["HP Spool ID"] = weights["_-BIC_-ZPRD_UNIT"].map(normalize_id)
    weights["Spool Weight (kg)"] = pd.to_numeric(weights["_-BIC_-ZPU_QTY"], errors="coerce")
    weights["Spool Diameter (mm)"] = pd.to_numeric(weights["_-BIC_-ZCDIAMETR"], errors="coerce")
    area = math.pi * (weights["Spool Diameter (mm)"] / 1000) ** 2 / 4
    weights["Spool Length (m)"] = weights["Spool Weight (kg)"] / (7850 * area)
    hp = (
        weights.drop_duplicates("HP Spool ID", keep="last")
        .merge(hp_times, on="HP Spool ID", how="left")
    )

    return {
        "production": production,
        "quality": quality,
        "traceability": trace,
        "trace_edges": trace_edges,
        "half_products": hp,
        "fg_trace": fg_trace,
        **deeper_levels,
        "diagnostics": pd.DataFrame([{
            "HP-level rows queried": len(hp_level),
            "Children FG rows (tracked materials)": len(children),
            "FG rows in tracked materials": len(production),
            "Traceability rows": len(trace),
            "Traceability edges (linked)": int(trace_edges["FG Spool ID"].notna().sum()),
            "Traceability edges (unconsumed/unknown)": int(trace_edges["FG Spool ID"].isna().sum()),
            "Unique HP IDs queried": hp_level["HP Spool ID"].nunique(),
            "HP times from trace (precise)": int((hp_times["Timestamp Source"] == "trace (ZPP_M06 CREATEDON+CREA_TIME)").sum()),
            "HP times from CALDAY (date-only fallback)": int((hp_times["Timestamp Source"] == "ZPP_M08 CALDAY (date-only, no time-of-day)").sum()),
            "Weight rows": len(weights),
            **{f"{step.capitalize()} rows": len(df) for step, df in deeper_levels.items()},
        }]),
    }


def fetch_machine_length_by_day(half_products, machine_config=None, max_delay=pd.Timedelta(hours=4)) -> pd.DataFrame:
    machine_config = machine_config or {
        "server": "bezwvai1THI", "location": "IVD157", "part": "Take Up",
        "description": "Length PV", "tagid": "Length PV",
    }
    periods = half_products.dropna(subset=["Spool Time"]).copy()
    periods["Spool Time"] = pd.to_datetime(periods["Spool Time"], errors="coerce")
    days = periods["Spool Time"].dropna().dt.normalize().unique()
    frames = []
    for day in sorted(days):
        start_utc = pd.Timestamp(day).tz_localize("Europe/Brussels").tz_convert("UTC") - max_delay
        end_utc = (pd.Timestamp(day) + pd.Timedelta(days=1)).tz_localize("Europe/Brussels").tz_convert("UTC")
        keys = [machine_config[key] for key in ("server", "location", "part", "description")] + [
            start_utc.strftime("%Y-%m-%d %H:%M:%S"), end_utc.strftime("%Y-%m-%d %H:%M:%S"), machine_config["tagid"]
        ]
        daily = query_datakap(keys, tagtype="float")
        if daily.empty:
            continue
        time_col = "LocalTime" if "LocalTime" in daily.columns else "Time"
        frames.append(pd.DataFrame({
            "Time": pd.to_datetime(daily[time_col], errors="coerce"),
            "Length PV": pd.to_numeric(daily["Value"], errors="coerce"),
        }).dropna())
    if not frames:
        return pd.DataFrame(columns=["Time", "Length PV"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("Time").sort_values("Time").reset_index(drop=True)


def match_spools(half_products, machine_data, max_delay=pd.Timedelta(hours=4), length_tolerance=0.11):
    hp = half_products.copy().reset_index(drop=True)
    hp["Spool Time"] = pd.to_datetime(hp["Spool Time"], errors="coerce")
    machine = machine_data.copy().sort_values("Time").reset_index(drop=True)
    machine["Next Length PV"] = machine["Length PV"].shift(-1)
    machine["Counter Difference"] = machine["Next Length PV"] - machine["Length PV"]
    resets = machine.loc[machine["Counter Difference"] < -1000, ["Time", "Length PV"]].rename(
        columns={"Time": "Machine Time", "Length PV": "Machine Length"}
    ).reset_index(drop=True)
    resets["Machine Start Time"] = resets["Machine Time"].shift(1)
    if not resets.empty:
        resets.loc[0, "Machine Start Time"] = machine["Time"].min()

    candidates = []
    for hp_index, spool in hp.iterrows():
        length = pd.to_numeric(spool.get("Spool Length (m)"), errors="coerce")
        if pd.isna(length) or length <= 0:
            continue
        for reset_index, reset in resets.iterrows():
            delay = spool["Spool Time"] - reset["Machine Time"]
            relative_difference = abs(length - reset["Machine Length"]) / length
            if pd.Timedelta(0) <= delay <= max_delay and relative_difference <= length_tolerance:
                candidates.append({
                    "HP Index": hp_index, "Reset Index": reset_index,
                    "HP Spool ID": spool["HP Spool ID"], "Spool Time": spool["Spool Time"],
                    "Spool Length (m)": length, "Machine Start Time": reset["Machine Start Time"],
                    "Machine Time": reset["Machine Time"], "Machine Length": reset["Machine Length"],
                    "Time Difference (min)": delay.total_seconds() / 60,
                    "Length Difference (m)": length - reset["Machine Length"],
                    "Relative Length Difference": relative_difference,
                })
    candidates = pd.DataFrame(candidates)
    selected = []
    used_hp, used_reset = set(), set()
    if not candidates.empty:
        for _, candidate in candidates.sort_values(["Time Difference (min)", "Relative Length Difference"]).iterrows():
            if candidate["HP Index"] in used_hp or candidate["Reset Index"] in used_reset:
                continue
            selected.append(candidate.to_dict())
            used_hp.add(candidate["HP Index"])
            used_reset.add(candidate["Reset Index"])
    matches = hp.copy()
    matches["HP Index"] = matches.index
    selected = pd.DataFrame(selected)
    if not selected.empty:
        matches = matches.merge(selected.drop(columns=["HP Spool ID", "Spool Time", "Spool Length (m)"], errors="ignore"), on="HP Index", how="left")
    else:
        for column in ["Machine Start Time", "Machine Time", "Machine Length", "Time Difference (min)", "Length Difference (m)", "Relative Length Difference"]:
            matches[column] = pd.NaT if "Time" in column else np.nan
    matches["Matched"] = matches["Machine Time"].notna()
    return matches.drop(columns=["HP Index"]), resets, candidates


def summarize_parameter(values, peak_distance=10):
    values = pd.to_numeric(pd.Series(values), errors="coerce").dropna().reset_index(drop=True)
    result = {"Sample Count": int(len(values))}
    if values.empty:
        return {**result, **{key: np.nan for key in ["Parameter Min", "Parameter Max", "Parameter Mean", "Parameter Median", "Parameter Std", "Parameter Range", "Peak Count", "Peak Maximum"]}}
    result.update({
        "Parameter Min": values.min(), "Parameter Max": values.max(), "Parameter Mean": values.mean(),
        "Parameter Median": values.median(), "Parameter Std": values.std(), "Parameter Range": values.max() - values.min(),
    })
    try:
        from scipy.signal import find_peaks
        peaks, _ = find_peaks(values.to_numpy(), prominence=max(values.std(ddof=0) * 1.5, result["Parameter Range"] * 0.05), distance=peak_distance)
        result["Peak Count"] = len(peaks)
        result["Peak Maximum"] = values.iloc[peaks].max() if len(peaks) else np.nan
    except ImportError:
        result["Peak Count"] = np.nan
        result["Peak Maximum"] = np.nan
    return result


def build_footprints(matches, parameter_data, parameter_name="Coat Weight PV") -> pd.DataFrame:
    empty_columns = [
        "Sample Count", "Parameter Min", "Parameter Max", "Parameter Mean", "Parameter Median",
        "Parameter Std", "Parameter Range", "Peak Count", "Peak Maximum",
        "HP Spool ID", "Machine Start Time", "Machine Time", "Parameter",
    ]
    data = parameter_data.copy()
    data["Time"] = pd.to_datetime(data["Time"], errors="coerce", utc=True)
    data["Value"] = pd.to_numeric(data["Value"], errors="coerce")
    rows = []
    for _, match in matches[matches["Matched"]].iterrows():
        start = pd.Timestamp(match["Machine Start Time"]).tz_localize("Europe/Brussels").tz_convert("UTC")
        end = pd.Timestamp(match["Machine Time"]).tz_localize("Europe/Brussels").tz_convert("UTC")
        period = data[(data["Time"] > start) & (data["Time"] <= end)]
        row = summarize_parameter(period["Value"])
        row.update({"HP Spool ID": match["HP Spool ID"], "Machine Start Time": match["Machine Start Time"], "Machine Time": match["Machine Time"], "Parameter": parameter_name})
        rows.append(row)
    if not rows:
        # No HP spool could be matched to a machine-length reset (e.g. no signal data
        # for that date/machine, or nothing tracked yet on the HP-anchored path).
        # Still return the expected columns so downstream merges/renames don't KeyError.
        return pd.DataFrame(columns=empty_columns)
    return pd.DataFrame(rows)


def prepare_analysis(
    production_dates=None,
    footprint_feature="Parameter Mean",
    config=None,
    mode="fg",
    machines=None,
    level=None,
    final_ids=None,
) -> dict[str, pd.DataFrame]:
    """Prepare all analysis tables.

    mode="fg": production_dates are finished-goods production dates; traces backward to HP parents.
    mode="hp": production_dates are half-product production dates; traces forward to FG children
               (restricted to MATERIAL_CODES).
    mode="level": production_dates + machines anchor an arbitrary deeper level (level=3
                  patenting, 4 drawing, 5 pickling); resolves to root FG spool IDs via
                  ZPP_M06's own _-BIC_-ZFGSPLID, then builds the same FG-anchored tables.
    mode="ids": final_ids is an already-resolved list of root FG spool IDs (e.g. from a
                batch number, spool ID, or wire-rod raw-material batch lookup); skips the
                date/machine query entirely.
    """
    if mode == "hp":
        tables = prepare_spool_tables_from_hp(production_dates, machines=machines, config=config)
    elif mode == "level":
        if level is None:
            raise ValueError("mode='level' requires a level (3=patenting, 4=drawing, 5=pickling).")
        resolved_ids = resolve_fg_ids_from_level_anchor(level, production_dates, machines=machines, config=config)
        if not resolved_ids:
            raise ValueError("No spools found at that level for the selected dates/machine.")
        tables = prepare_spool_tables_from_fg_ids(resolved_ids, config=config)
    elif mode == "ids":
        if not final_ids:
            raise ValueError("mode='ids' requires a non-empty list of final_ids.")
        tables = prepare_spool_tables_from_fg_ids(final_ids, config=config)
    else:
        tables = prepare_spool_tables(production_dates, machines=machines, config=config)
    machine = fetch_machine_length_by_day(tables["half_products"])
    matches, resets, candidates = match_spools(tables["half_products"], machine)
    parameter_data = fetch_parameter_by_day(matches)
    footprints = build_footprints(matches, parameter_data)
    tables.update({"machine_data": machine, "reset_candidates": resets, "matching_candidates": candidates, "matches": matches, "parameter_data": parameter_data, "footprints": footprints})
    return tables


def fetch_parameter_by_day(matches, config=None) -> pd.DataFrame:
    config = config or {"server": "bezwvai1THI", "location": "IVD157", "part": "DDD01", "description": "Coat Weight PV", "tagid": "Coat Weight PV"}
    valid = matches[matches["Matched"]].dropna(subset=["Machine Start Time", "Machine Time"])
    days = set()
    for _, row in valid.iterrows():
        start = pd.Timestamp(row["Machine Start Time"]).normalize()
        end = pd.Timestamp(row["Machine Time"]).normalize()
        days.update(pd.date_range(start, end, freq="D"))
    frames = []
    for day in sorted(days):
        start_utc = day.tz_localize("Europe/Brussels").tz_convert("UTC")
        end_utc = (day + pd.Timedelta(days=1)).tz_localize("Europe/Brussels").tz_convert("UTC")
        keys = [config[key] for key in ("server", "location", "part", "description")] + [start_utc.strftime("%Y-%m-%d %H:%M:%S"), end_utc.strftime("%Y-%m-%d %H:%M:%S"), config["tagid"]]
        daily = query_datakap(keys, tagtype="float")
        if daily.empty or "Value" not in daily.columns:
            continue
        time_col = "Time" if "Time" in daily.columns else "LocalTime"
        frames.append(pd.DataFrame({"Time": pd.to_datetime(daily[time_col], errors="coerce", utc=True), "Value": pd.to_numeric(daily["Value"], errors="coerce")}).dropna())
    if not frames:
        return pd.DataFrame(columns=["Time", "Value"])
    return pd.concat(frames, ignore_index=True).drop_duplicates("Time").sort_values("Time").reset_index(drop=True)
