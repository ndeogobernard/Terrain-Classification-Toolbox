"""
Prepare Water Service — Water/Utility Service Area Preprocessing
====================================================================
Downloads the EPA Community Water System (CWS) service area dataset,
filters to a user-specified state, merges the CWS and T_NTNC (transient
non-transient non-community) layers plus an optional local sewer/utility
shapefile, clips to a user-supplied boundary layer, dissolves overlapping
boundaries, and produces a water service area feature class with a
Has_Water flag — ready to feed the Calculate Dev Indices tool's Water
Service Area input.

Has_Water = 1 for every polygon in the output (every polygon here IS a
water-served area by definition). Downstream, any location that doesn't
intersect one of these polygons is treated as not water-served.

Data source: EPA ORD — Community Water System Service Area Boundaries v3.0
  GitHub: https://github.com/USEPA/ORD_SAB_Model
  GeoPackage layers: main.CWS, main.T_NTNC
"""

import arcpy
import os
import datetime
import warnings

# ── EPA constants ─────────────────────────────────────────────────────────────
EPA_ZIP_URL = (
    "https://github.com/USEPA/ORD_SAB_Model/raw/refs/heads/main/"
    "Version_History/PWS_Boundaries_Latest.zip"
)
GPKG_FILENAME = "Service_Areas_V_3_0.gpkg"
CWS_LAYER = "main.CWS"
NTNC_LAYER = "main.T_NTNC"


def _count(fc):
    return int(arcpy.management.GetCount(fc).getOutput(0))


def _build_clip_boundary(boundary_fc, where_clause):
    """Dissolve the (optionally filtered) boundary layer into a single
    clip polygon."""
    tmp_sel = "_prewater_sel"
    tmp_diss = r"in_memory\prewater_clip"
    try:
        if arcpy.Exists(tmp_sel):
            arcpy.management.Delete(tmp_sel)
        arcpy.management.MakeFeatureLayer(boundary_fc, tmp_sel, where_clause or None)
        n = _count(tmp_sel)
        arcpy.AddMessage(f"  Boundary features selected: {n:,}")
        if arcpy.Exists(tmp_diss):
            arcpy.management.Delete(tmp_diss)
        arcpy.management.Dissolve(tmp_sel, tmp_diss, multi_part="SINGLE_PART")
        arcpy.management.Delete(tmp_sel)
        return tmp_diss
    except Exception as e:
        arcpy.AddWarning(f"  Could not build clip boundary: {e}")
        return None


def _download_epa_gpkg(work_folder, verify_ssl):
    """Download the EPA zip from GitHub and extract the GeoPackage.
    Returns path to gpkg or None on failure.

    verify_ssl=False is a corporate-network compatibility setting for
    environments where TLS traffic is inspected by a proxy — common on
    managed enterprise networks. Leave True unless downloads fail with
    SSL errors on your network."""
    import requests
    if not verify_ssl:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")

    dest_zip = os.path.join(work_folder, "PWS_Boundaries_Latest.zip")
    dest_gpkg = os.path.join(work_folder, GPKG_FILENAME)

    if os.path.exists(dest_gpkg):
        arcpy.AddMessage(f"  Using cached GeoPackage: {dest_gpkg}")
        return dest_gpkg, "cached"

    arcpy.AddMessage("  Downloading EPA water service data from GitHub...")
    try:
        r = requests.get(EPA_ZIP_URL, stream=True, timeout=300, verify=verify_ssl)
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest_zip, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    arcpy.AddMessage(f"  {pct:.0f}%  ({downloaded/1024/1024:.0f} / {total/1024/1024:.0f} MB)")
        arcpy.AddMessage("  Download complete, extracting GeoPackage...")
    except Exception as e:
        arcpy.AddWarning(f"  Download failed: {e}")
        return None, None

    import zipfile
    try:
        with zipfile.ZipFile(dest_zip, "r") as zf:
            gpkg_member = next((n for n in zf.namelist() if n.endswith(".gpkg")), None)
            if gpkg_member is None:
                arcpy.AddWarning("  No .gpkg file found in downloaded zip.")
                return None, None
            with zf.open(gpkg_member) as src, open(dest_gpkg, "wb") as dst:
                dst.write(src.read())
        os.remove(dest_zip)
        return dest_gpkg, "downloaded"
    except Exception as e:
        arcpy.AddWarning(f"  Extraction failed: {e}")
        return None, None


def _read_state_layer(gpkg, layer_name, where_clause, out_lyr_name):
    """Read a filtered layer from the GeoPackage into a feature layer."""
    try:
        src = os.path.join(gpkg, layer_name)
        if arcpy.Exists(out_lyr_name):
            arcpy.management.Delete(out_lyr_name)
        arcpy.management.MakeFeatureLayer(src, out_lyr_name, where_clause)
        n = _count(out_lyr_name)
        return (out_lyr_name if n > 0 else None), n
    except Exception as e:
        arcpy.AddWarning(f"  Could not read {layer_name}: {e}")
        return None, 0


def _write_log(path, lines):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except PermissionError:
        arcpy.AddWarning("Could not write log — file may be open.")


# ── Toolbox ───────────────────────────────────────────────────────────────────

class Toolbox(object):
    def __init__(self):
        self.label = "Prepare Water Service"
        self.alias = "PrepareWaterService"
        self.tools = [WaterServicePrep]


class WaterServicePrep(object):

    def __init__(self):
        self.label = "Prepare Water Service"
        self.description = (
            "Downloads the EPA Community Water System service area dataset, "
            "filters to a user-specified state, merges CWS/T_NTNC layers "
            "plus an optional local sewer shapefile, clips to a boundary "
            "layer, dissolves overlaps, and writes a water service area "
            "feature class with a Has_Water flag."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="Project Geodatabase", name="project_gdb",
            datatype="DEWorkspace", parameterType="Required", direction="Input")
        p0.filter.list = ["Local Database"]

        p1 = arcpy.Parameter(
            displayName="Log Folder", name="log_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p2 = arcpy.Parameter(
            displayName="Boundary/Zone Layer", name="boundary_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p2.description = "Any polygon layer defining the area to clip to (e.g. zone/TAZ layer, county boundary, project extent)."

        p3 = arcpy.Parameter(
            displayName="Boundary Selection Query (optional)", name="boundary_query",
            datatype="GPSQLExpression", parameterType="Optional", direction="Input")
        p3.parameterDependencies = [p2.name]

        p4 = arcpy.Parameter(
            displayName="State Postal Code (for CWS filter)", name="state_code",
            datatype="GPString", parameterType="Required", direction="Input")
        p4.description = "Two-letter state postal code used to filter the EPA dataset's Primacy_Agency / PRIMACY_AGENCY_CODE fields, e.g. 'OH', 'TX', 'CA'."

        p5 = arcpy.Parameter(
            displayName="Local Sewer/Utility Shapefile (optional)", name="private_shp",
            datatype="DEShapefile", parameterType="Optional", direction="Input")

        p6 = arcpy.Parameter(
            displayName="Verify SSL Certificates", name="verify_ssl",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p6.value = True
        p6.description = (
            "Uncheck only if downloads fail with SSL errors on a "
            "corporate network that inspects TLS traffic."
        )

        return [p0, p1, p2, p3, p4, p5, p6]

    def execute(self, parameters, messages):
        gdb_path = parameters[0].valueAsText
        log_folder = parameters[1].valueAsText
        boundary_fc = parameters[2].valueAsText
        boundary_query = parameters[3].valueAsText
        state_code = (parameters[4].valueAsText or "").upper().strip()
        private_shp = parameters[5].valueAsText
        verify_ssl = parameters[6].value if parameters[6].value is not None else True

        if not state_code or len(state_code) != 2:
            arcpy.AddError("State Postal Code must be a 2-letter code, e.g. 'OH'.")
            return

        run_id = datetime.datetime.now().strftime("PW_%Y%m%d_%H%M%S")
        work_folder = os.path.join(os.path.dirname(gdb_path), "Source_Data", "WaterService")
        os.makedirs(work_folder, exist_ok=True)
        log_dir = os.path.join(log_folder, run_id)
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"PrepareWaterService_{run_id}.txt")
        out_shp = os.path.join(work_folder, "Terrain_Water_merged.shp")
        out_ds = gdb_path

        arcpy.env.overwriteOutput = True
        run_start = datetime.datetime.now()
        operator = os.environ.get("USERNAME", "unknown")

        # ── Step 1: Download / locate EPA GeoPackage ──────────────────────
        arcpy.AddMessage("Step 1: EPA water service dataset...")
        gpkg, gpkg_source = _download_epa_gpkg(work_folder, verify_ssl)
        if gpkg is None:
            arcpy.AddError("Could not obtain EPA GeoPackage — cannot proceed.")
            return

        # ── Step 2: Build clip boundary ────────────────────────────────────
        arcpy.AddMessage("Step 2: Building clip boundary...")
        clip_fc = _build_clip_boundary(boundary_fc, boundary_query)
        if clip_fc is None:
            arcpy.AddError("Could not build clip boundary from the boundary layer.")
            return

        # ── Step 3: Read CWS layer ─────────────────────────────────────────
        arcpy.AddMessage(f"Step 3: Reading CWS layer (state={state_code})...")
        cws_lyr, n_cws = _read_state_layer(
            gpkg, CWS_LAYER, f"Primacy_Agency = '{state_code}'", "_prewater_cws")
        if cws_lyr is None:
            arcpy.AddError("Could not read CWS layer from GeoPackage.")
            return
        arcpy.AddMessage(f"  CWS records ({state_code}): {n_cws:,}")

        # ── Step 4: Read T_NTNC layer ──────────────────────────────────────
        arcpy.AddMessage("Step 4: Reading T_NTNC layer...")
        ntnc_lyr, n_ntnc = _read_state_layer(
            gpkg, NTNC_LAYER, f"PRIMACY_AGENCY_CODE = '{state_code}'", "_prewater_ntnc")
        if ntnc_lyr:
            arcpy.AddMessage(f"  T_NTNC records ({state_code}): {n_ntnc:,}")
        else:
            arcpy.AddWarning("T_NTNC unavailable — proceeding with CWS only.")

        # ── Step 5: Optional local sewer/utility layer ─────────────────────
        to_merge = [l for l in [cws_lyr, ntnc_lyr] if l is not None]
        n_private = 0
        if private_shp and os.path.exists(private_shp):
            arcpy.AddMessage(f"Step 5: Local sewer layer: {os.path.basename(private_shp)}")
            try:
                arcpy.management.MakeFeatureLayer(private_shp, "_prewater_private")
                n_private = _count("_prewater_private")
                to_merge.append("_prewater_private")
                arcpy.AddMessage(f"  Records: {n_private:,}")
            except Exception as e:
                arcpy.AddWarning(f"Could not read local sewer shapefile: {e}")

        # ── Step 6: Merge ───────────────────────────────────────────────────
        arcpy.AddMessage("Step 6: Merging layers...")
        tmp_merged = r"in_memory\prewater_merged"
        arcpy.management.Merge(to_merge, tmp_merged)
        n_merged = _count(tmp_merged)
        arcpy.AddMessage(f"  Merged: {n_merged:,} total features")
        for lyr in ["_prewater_cws", "_prewater_ntnc", "_prewater_private"]:
            try:
                arcpy.management.Delete(lyr)
            except Exception:
                pass

        # ── Step 7: Clip ────────────────────────────────────────────────────
        arcpy.AddMessage("Step 7: Clipping to boundary...")
        tmp_clipped = r"in_memory\prewater_clipped"
        arcpy.analysis.Clip(tmp_merged, clip_fc, tmp_clipped)
        n_clipped = _count(tmp_clipped)
        arcpy.AddMessage(f"  After clip: {n_clipped:,}")
        arcpy.management.Delete(tmp_merged)
        try:
            arcpy.management.Delete(clip_fc)
        except Exception:
            pass
        if n_clipped == 0:
            arcpy.AddWarning("No water service features within selected boundary.")

        # ── Step 8: Dissolve ────────────────────────────────────────────────
        arcpy.AddMessage("Step 8: Dissolving overlapping boundaries...")
        tmp_dissolved = r"in_memory\prewater_dissolved"
        arcpy.management.Dissolve(tmp_clipped, tmp_dissolved, dissolve_field=None, multi_part="SINGLE_PART")
        n_dissolved = _count(tmp_dissolved)
        arcpy.AddMessage(f"  Dissolved: {n_clipped:,} -> {n_dissolved:,}")
        arcpy.management.Delete(tmp_clipped)

        # ── Step 9: Has_Water flag ──────────────────────────────────────────
        arcpy.management.AddField(tmp_dissolved, "Has_Water", "SHORT")
        arcpy.management.CalculateField(tmp_dissolved, "Has_Water", "1", "PYTHON3")

        # ── Step 10: Write output ───────────────────────────────────────────
        arcpy.management.CopyFeatures(tmp_dissolved, out_shp)
        arcpy.management.Delete(tmp_dissolved)
        n_out = _count(out_shp)

        run_end = datetime.datetime.now()
        run_elapsed = round((run_end - run_start).total_seconds(), 1)

        arcpy.AddMessage(f"Output: {out_shp} ({n_out:,} polygons)")
        out_fc_path = os.path.join(out_ds, "Terrain_Water")
        if arcpy.Exists(out_fc_path):
            arcpy.management.Delete(out_fc_path)
        arcpy.conversion.FeatureClassToFeatureClass(out_shp, out_ds, "Terrain_Water")
        arcpy.AddMessage(f"  Terrain_Water written to GDB: {out_fc_path}")

        qa_flags = []
        if n_clipped == 0:
            qa_flags.append("WARN: No features within boundary.")
        if n_cws == 0:
            qa_flags.append(f"WARN: CWS returned 0 records for state {state_code}.")
        if n_out < 10:
            qa_flags.append(f"WARN: Output has only {n_out} polygons — may be incomplete.")

        lines = [
            "=" * 70, "WATER SERVICE AREA PREPROCESSING — QAQC REPORT", "=" * 70, "",
            "RUN INFORMATION", "-" * 70,
            f"Run ID          : {run_id}",
            f"Operator        : {operator}",
            f"State           : {state_code}",
            f"Start Time      : {run_start.strftime('%Y-%m-%d %H:%M:%S')}",
            f"End Time        : {run_end.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Elapsed         : {run_elapsed}s ({round(run_elapsed/60,1)} min)",
            f"Output          : {out_shp}", "",
            "DATA SOURCE", "-" * 70,
            f"GeoPackage      : {gpkg}",
            f"Source          : {gpkg_source}",
            f"EPA Dataset     : CWS Service Area Boundaries v3.0",
            f"GitHub URL      : {EPA_ZIP_URL}", "",
            "LAYER STATISTICS", "-" * 70,
            f"CWS ({state_code})     : {n_cws:,} records",
            f"T_NTNC ({state_code})  : {n_ntnc:,} records",
            f"Local sewer     : {n_private:,} records",
            f"Merged total    : {n_merged:,}",
            f"After clip      : {n_clipped:,}",
            f"After dissolve  : {n_dissolved:,}",
            f"Output polygons : {n_out:,}", "",
            "PROCESSING NOTES", "-" * 70,
            "Has_Water = 1 for all output polygons.",
            "Locations not intersecting any polygon are treated as not water-served downstream.",
            "Overlapping boundaries resolved by dissolve step.", "",
        ]
        if qa_flags:
            lines += ["QA FLAGS RAISED", "-" * 70] + [f"  {f}" for f in qa_flags] + [""]
        else:
            lines += ["QA FLAGS : None raised.", ""]
        lines += ["=" * 70, f"END OF REPORT — Run ID: {run_id}", "=" * 70]

        _write_log(log_file, lines)
        arcpy.AddMessage(f"QAQC log : {log_file}")
        arcpy.AddMessage("\nSUCCESS — Prepare Water Service complete.")
