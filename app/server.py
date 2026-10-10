"""FastAPI server. Binds to loopback by default to keep private quote uploads local."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import json
import base64
import os
import tempfile
from typing import Annotated
from urllib.parse import parse_qs
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .engine import Scenario, simulate
from .data import load_ual, load_fuel, manifest
from .quotes import parse_quotes, save_quotes, read_quotes, quote_coverage, route_diagnostic, QUOTES_FILE
from .monthly import MonthlyScenario, simulate_monthly, baseline
from .evidence import load_snapshot, evidence_manifest
from .financial_history import history_status
from .operating_history import operating_history_status
from .form41_history import form41_history_status
from .utilization import utilization_status
from .quarterly_capacity import capacity_status
from .monthly_traffic import traffic_status
from .traffic_vintage_research import vintage_research_status
from .thesis import ThesisNotes, export_thesis, verified_export
from .failure_search import SearchAxis, FailureSearch, search_failures
from .target_inversion import OperatingTarget, ValuationScenario, TargetInversion, invert_target
from .model_archive import archive_source, capture_source, run_preserved, replay_saved, read_verified_study, STUDY_CACHE_ROOT
from .convergence import ConvergenceSpec, study_convergence
from .lab.api import router as lab_router

# Capture at module import so edits on disk cannot silently archive a different
# implementation from the one this server loaded. Restart after model edits.
MODEL_SOURCE = {kind: capture_source(kind) for kind in ("monthly", "failure_search", "target_inversion", "convergence")}
HOSTED_DEMO = os.environ.get("VERCEL") == "1"

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
app = FastAPI(title="UAL Counterfactual Engine", version="0.1.0", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(lab_router)


@app.get("/api/traffic/status")
def monthly_traffic_status():
    try:
        return traffic_status()
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(status_code=503, detail="Monthly traffic estimates failed verification: " + str(error)) from error


@app.get("/api/traffic/record")
def monthly_traffic_record():
    verified = monthly_traffic_status()
    if verified["status"] != "available":
        raise HTTPException(status_code=404, detail="Monthly traffic study has not been built")
    return FileResponse(ROOT / "data/monthly_traffic" / verified["snapshot_id"] / "artifact.json",
        media_type="application/json", filename="ual-monthly-traffic-" + verified["snapshot_id"][:12] + ".json")


@app.get("/api/traffic-vintages/status")
def monthly_traffic_vintage_status():
    try:
        return vintage_research_status()
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(status_code=503, detail="Traffic source comparison failed verification: " + str(error)) from error


@app.get("/api/traffic-vintages/record")
def monthly_traffic_vintage_record():
    verified = monthly_traffic_vintage_status()
    if verified["status"] != "available":
        raise HTTPException(status_code=404, detail="Traffic source comparison has not been reviewed")
    return FileResponse(ROOT / "data/monthly_traffic_vintages" / verified["snapshot_id"] / "artifact.json",
        media_type="application/json", filename="ual-traffic-source-comparison-" + verified["snapshot_id"][:12] + ".json")


@app.get("/api/traffic-vintages/audit")
def monthly_traffic_vintage_audit():
    verified = monthly_traffic_vintage_status()
    if verified["status"] != "available":
        raise HTTPException(status_code=404, detail="Traffic source comparison has not been reviewed")
    return FileResponse(ROOT / "data/monthly_traffic_vintage_checks" / (verified["audit_id"] + ".json"),
        media_type="application/json", filename="ual-traffic-source-comparison-audit.json")

class ScenarioPayload(BaseModel):
    fuel_shock_pct: float = Field(default=20, ge=-60, le=100)
    fuel_shock_half_life_days: int = Field(default=35, ge=1, le=180)
    response_lag_days: int = Field(default=10, ge=0, le=90)
    response_half_life_days: int = Field(default=18, ge=1, le=120)
    pass_through_fraction: float = Field(default=.55, ge=0, le=1.25)
    demand_elasticity: float = Field(default=.55, ge=0, le=1.5)
    horizon_days: int = Field(default=60, ge=14, le=180)
    paths: int = Field(default=300, ge=20, le=1200)
    seed: int = Field(default=2026, ge=0, le=1_000_000_000)

class QuoteUpload(BaseModel):
    csv_text: str

class ThesisNotesPayload(BaseModel):
    model_config = {"extra": "forbid"}
    title: str = Field(min_length=1, max_length=70)
    hypothesis: str = Field(min_length=1, max_length=220)
    reference: str
    checkpoint_on: str
    checkpoint: str = Field(min_length=1, max_length=180)
    checkpoint_source: str = Field(min_length=1, max_length=180)
    falsification: str = Field(min_length=1, max_length=220)


class ThesisPayload(BaseModel):
    model_config = {"extra": "forbid"}
    run_record: dict
    notes: ThesisNotesPayload


class ConvergenceSpecPayload(BaseModel):
    model_config = {"extra": "forbid"}
    path_counts: list[Annotated[int, Field(strict=True)]] = Field(default=[1000, 5000, 10000, 20000, 50000], min_length=3, max_length=10, strict=True)
    replicates: int = Field(default=12, ge=8, le=24, strict=True)
    absolute_tolerance_musd: float = Field(default=25, ge=.0001, le=10000, strict=True)
    relative_tolerance: float = Field(default=.01, ge=0, le=.2, strict=True)
    margin_tolerance_pp: float = Field(default=.05, ge=.0001, le=10, strict=True)
    breach_tolerance_fraction: float = Field(default=.005, ge=.000001, le=.2, strict=True)
    consecutive_levels: int = Field(default=2, ge=2, le=3, strict=True)
    batch_paths: int = Field(default=5000, ge=20, le=10000, strict=True)


class ConvergencePayload(BaseModel):
    model_config = {"extra": "forbid"}
    run_record: dict
    convergence: ConvergenceSpecPayload = Field(default_factory=ConvergenceSpecPayload)


class MonthlyPayload(BaseModel):
    model_config = {"extra": "forbid"}
    information_cutoff: str = "2026-07-16"
    horizon_months: int = Field(default=12, ge=1, le=24)
    paths: int = Field(default=10000, ge=20, le=50000)
    seed: int = Field(default=2026, ge=0, le=1_000_000_000)
    fuel_shock_pct: float = Field(default=25, ge=-60, le=100)
    demand_shock_pct: float = Field(default=0, ge=-80, le=100)
    capacity_shock_pct: float = Field(default=0, ge=-90, le=0)
    delivery_delay_months: int = Field(default=0, ge=0, le=24)
    delivery_aircraft_count: int | None = Field(default=None, ge=1, le=1000, strict=True)
    delivery_group: str = "all"
    aircraft_unavailable_pct: float = Field(default=0, ge=0, le=90)
    availability_group: str = "all"
    regional_contract_shock_pct: float = Field(default=0, ge=-50, le=100)
    fleet_spare_fraction: float = Field(default=.02, ge=0, le=.2)
    schedule_growth_fraction: float = Field(default=1, ge=0, le=1)
    regional_contract_fixed_fraction: float = Field(default=.7, ge=0, le=1)
    geography: str = "all"
    onset_month: int = Field(default=1, ge=1, le=24)
    duration_months: int = Field(default=6, ge=1, le=24)
    recovery_half_life_months: float = Field(default=2, ge=.1, le=24)
    response_mode: str = "assumed_constrained"
    response_lag_months: int = Field(default=1, ge=0, le=24)
    response_half_life_months: float = Field(default=2, ge=.1, le=24)
    pass_through_fraction: float = Field(default=.55, ge=0, le=1.25)
    demand_elasticity: float = Field(default=.55, ge=0, le=1.5)
    max_yield_step_pct: float = Field(default=3, ge=.01, le=20)
    demand_monthly_log_sd: float = Field(default=.03, ge=0, le=.3)
    fuel_demand_correlation: float = Field(default=.25, ge=-.95, le=.95)
    nonfuel_fixed_fraction: float = Field(default=.7, ge=0, le=1)
    marginal_cash_tax_fraction: float = Field(default=.21, ge=0, le=.5)
    cash_floor_musd: float = Field(default=5000, ge=0, le=100000)
    stochastic: bool = True
    cash_plan: dict | None = None
    purchase_plan: dict | None = None
    retention_plan: dict | None = None

class SearchAxisPayload(BaseModel):
    model_config = {"extra": "forbid"}
    driver: str
    low: float
    high: float
    points: int = Field(default=5, ge=2, le=7)


class FailureSearchPayload(BaseModel):
    model_config = {"extra": "forbid"}
    base_scenario: MonthlyPayload = Field(default_factory=MonthlyPayload)
    x: SearchAxisPayload = Field(default_factory=lambda: SearchAxisPayload(driver="fuel_shock_pct", low=0, high=60))
    y: SearchAxisPayload = Field(default_factory=lambda: SearchAxisPayload(driver="demand_shock_pct", low=-20, high=0))
    criterion: str = "cumulative_fcf"
    threshold_musd: float = Field(default=0, ge=-100000, le=100000)
    statistic: str = "p50"


class OperatingTargetPayload(BaseModel):
    model_config = {"extra": "forbid"}
    metric: str = "cumulative_fcf"
    value: float = 1000
    relation: str = "at_least"
    tolerance: float = Field(default=0, ge=0, le=1000000)
    statistic: str = "p50"
    minimum_cash_floor_musd: float | None = Field(default=None, ge=0, le=100000)


class ValuationPayload(BaseModel):
    model_config = {"extra": "forbid"}
    annual_discount_rate: float
    terminal_growth_rate: float
    terminal_annual_fcff_musd: float
    annual_non_cash_charges_musd: float
    annual_working_capital_investment_musd: float
    cash_operating_tax_rate: float
    enterprise_value_target_musd: float
    enterprise_value_tolerance_musd: float


class TargetInversionPayload(BaseModel):
    model_config = {"extra": "forbid"}
    base_scenario: MonthlyPayload = Field(default_factory=MonthlyPayload)
    x: SearchAxisPayload = Field(default_factory=lambda: SearchAxisPayload(driver="fuel_shock_pct", low=0, high=40))
    y: SearchAxisPayload = Field(default_factory=lambda: SearchAxisPayload(driver="demand_shock_pct", low=-10, high=10))
    target: OperatingTargetPayload = Field(default_factory=OperatingTargetPayload)
    valuation: ValuationPayload | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "monthly.html")

@app.get("/lab")
def lab_index():
    return FileResponse(STATIC / "lab" / "index.html")

@app.get("/report")
def report_index():
    page = STATIC / "lab" / "report.html"
    if not page.is_file():
        raise HTTPException(status_code=404, detail="The report has not been built yet")
    return FileResponse(page)

@app.get("/research")
def research_index():
    return FileResponse(STATIC / "monthly.html")

@app.get("/fuel-pilot")
def fuel_pilot():
    return FileResponse(STATIC / "index.html")

@app.get("/monthly")
def monthly_index():
    return FileResponse(STATIC / "monthly.html")

@app.get("/api/monthly/bootstrap")
def monthly_bootstrap(cutoff: str = "2026-07-16"):
    try:
        snapshot = load_snapshot()
        return {"baseline": baseline(snapshot, cutoff), "evidence": evidence_manifest(snapshot, cutoff)}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

@app.get("/api/monthly/purchase-example")
def purchase_example():
    # Public illustrative assumptions only; these are never enabled by default.
    return FileResponse(ROOT / "data" / "purchase_plan_example.json", media_type="application/json")

@app.get("/api/monthly/retention-example")
def retention_example():
    return FileResponse(ROOT / "data" / "retention_plan_example.json", media_type="application/json")

@app.get("/api/history/status")
def financial_history_status():
    try:
        return history_status()
    except (ValueError, OSError, KeyError) as e:
        raise HTTPException(status_code=503, detail="Cached financial history failed verification; rebuild from preserved sources") from e


@app.get("/api/operating-history/status")
def bts_history_status():
    try:
        return operating_history_status()
    except (ValueError, KeyError, FileNotFoundError) as e:
        raise HTTPException(status_code=503, detail="Cached BTS history failed verification; rebuild from preserved sources") from e

@app.get("/api/form41-history/status")
def bts_financial_history_status():
    try:
        return form41_history_status()
    except (ValueError, OSError, KeyError) as e:
        raise HTTPException(status_code=503, detail="Cached Form 41 history failed verification; rebuild from preserved sources") from e


@app.get("/api/utilization/status")
def fitted_utilization_status():
    try:
        return utilization_status()
    except (ValueError, OSError, KeyError, TypeError) as e:
        raise HTTPException(status_code=503, detail="Cached utilization estimate failed verification; rebuild from preserved inputs") from e


@app.get("/api/capacity/status")
def fitted_capacity_status():
    try:
        return capacity_status()
    except (ValueError, OSError, KeyError, TypeError) as e:
        raise HTTPException(status_code=503, detail="Cached capacity estimate failed verification; rebuild from preserved inputs") from e


@app.post("/api/monthly/thesis")
def monthly_thesis(settings: ThesisPayload):
    try:
        if HOSTED_DEMO:
            # A cold instance can verify a current-code run from another instance.
            archive_source(MODEL_SOURCE["monthly"])
        result = export_thesis(settings.run_record, ThesisNotes(**settings.notes.model_dump()))
        identity = result["artifact_id"]
        if HOSTED_DEMO:
            folder, _, _ = verified_export(identity)
            # Deliver immutable bytes in this response; later requests may reach
            # a different instance whose temporary filesystem has no export.
            urls = {kind + "_url": "data:" + mime + ";base64," + base64.b64encode((folder / ("thesis." + kind)).read_bytes()).decode("ascii")
                    for kind, mime in (("pdf", "application/pdf"), ("json", "application/json"))}
            return {"artifact_id": identity, "run_id": result["run_id"], "thesis": result["thesis"], **urls}
        return {"artifact_id": identity, "run_id": result["run_id"], "thesis": result["thesis"],
                "pdf_url": f"/api/theses/{identity}/pdf", "json_url": f"/api/theses/{identity}/json"}
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.get("/api/theses/{identity}/{format}")
def thesis_download(identity: str, format: str):
    if format not in {"pdf", "json"}:
        raise HTTPException(status_code=404, detail="Unsupported thesis format")
    try:
        folder, _, _ = verified_export(identity)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="Thesis export unavailable") from e
    except (ValueError, OSError, KeyError, TypeError) as e:
        raise HTTPException(status_code=503, detail="Thesis export failed verification") from e
    return FileResponse(folder / f"thesis.{format}", media_type="application/pdf" if format == "pdf" else "application/json",
                        filename=f"ual-thesis-{identity[:12]}.{format}")


@app.post("/api/monthly/thesis-download/{format}")
async def stateless_thesis_download(format: str, request: Request):
    """Native browser attachment, without relying on a previous instance's files."""
    if format not in {"pdf", "json"}:
        raise HTTPException(status_code=404, detail="Unsupported thesis format")
    body = await request.body()
    if len(body) > 4_000_000:
        raise HTTPException(status_code=413, detail="Thesis download request is too large")
    try:
        fields = parse_qs(body.decode("utf-8"), strict_parsing=True, max_num_fields=1)
        settings = ThesisPayload.model_validate(json.loads(fields["payload"][0]))
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(status_code=422, detail="Invalid thesis download request") from error

    def build_attachment():
        try:
            if HOSTED_DEMO:
                archive_source(MODEL_SOURCE["monthly"])
            result = export_thesis(settings.run_record, ThesisNotes(**settings.notes.model_dump()))
            folder, _, _ = verified_export(result["artifact_id"])
            return FileResponse(folder / f"thesis.{format}",
                media_type="application/pdf" if format == "pdf" else "application/json",
                filename=f"ual-thesis-{result['artifact_id'][:12]}.{format}")
        except (ValueError, KeyError, TypeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    return await run_in_threadpool(build_attachment)


@app.post("/api/monthly/simulate")
def monthly_simulation(settings: MonthlyPayload):
    try:
        return run_preserved(MODEL_SOURCE["monthly"], lambda: simulate_monthly(MonthlyScenario(**settings.model_dump())))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.post("/api/monthly/convergence")
def monthly_convergence(settings: ConvergencePayload):
    if HOSTED_DEMO:
        raise HTTPException(status_code=422, detail="Large sampling studies run locally. Download the run record and use the local convergence workflow.")
    try:
        base = MonthlyScenario(**settings.run_record["scenario"])
        spec = ConvergenceSpec(**settings.convergence.model_dump())
        spec.validate(base)
        if settings.run_record["manifest"]["code_sha256"] != MODEL_SOURCE["monthly"].code_sha256:
            raise ValueError("Run the scenario again with the current model before checking numerical stability")
        with tempfile.TemporaryDirectory(prefix="ual-convergence-reference-") as temporary:
            path = Path(temporary) / "run.json"
            path.write_text(json.dumps(settings.run_record, allow_nan=False))
            reference = replay_saved(path, "monthly")
        inputs = reference["replay_inputs"]
        MODEL_SOURCE["convergence"].assert_unchanged()
        cached = read_verified_study(reference["manifest"]["run_id"], asdict(spec),
            MODEL_SOURCE["convergence"].code_sha256, cache_root=STUDY_CACHE_ROOT)
        if cached is not None:
            return JSONResponse(cached, headers={"X-UAL-Study-Cache": "verified"})
        result = run_preserved(MODEL_SOURCE["convergence"], lambda: study_convergence(base, spec,
            snapshot=inputs["evidence_snapshot"], fuel=inputs["fuel_observations"]))
        if result["manifest"]["reference_run_id"] != reference["manifest"]["run_id"]:
            raise ValueError("Convergence reference differs from the verified displayed run")
        return result
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

@app.post("/api/monthly/failure-search")
def failure_search(settings: FailureSearchPayload):
    if HOSTED_DEMO and settings.base_scenario.paths * settings.x.points * settings.y.points > 250000:
        raise HTTPException(status_code=422, detail="Hosted grid budget exceeded. Use fewer points or paths, or run this study locally.")
    try:
        search = FailureSearch(x=SearchAxis(**settings.x.model_dump()), y=SearchAxis(**settings.y.model_dump()),
            criterion=settings.criterion, threshold_musd=settings.threshold_musd, statistic=settings.statistic)
        return run_preserved(MODEL_SOURCE["failure_search"], lambda: search_failures(MonthlyScenario(**settings.base_scenario.model_dump()), search))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.post("/api/monthly/target-inversion")
def target_inversion(settings: TargetInversionPayload):
    if HOSTED_DEMO and settings.base_scenario.paths * settings.x.points * settings.y.points > 250000:
        raise HTTPException(status_code=422, detail="Hosted grid budget exceeded. Use fewer points or paths, or run this study locally.")
    try:
        spec = TargetInversion(x=SearchAxis(**settings.x.model_dump()), y=SearchAxis(**settings.y.model_dump()),
            target=OperatingTarget(**settings.target.model_dump()),
            valuation=ValuationScenario(**settings.valuation.model_dump()) if settings.valuation else None)
        return run_preserved(MODEL_SOURCE["target_inversion"], lambda: invert_target(MonthlyScenario(**settings.base_scenario.model_dump()), spec))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "UAL Counterfactual Engine V1", "hosted_demo": HOSTED_DEMO}

@app.get("/api/bootstrap")
def bootstrap():
    fuel = load_fuel()
    ual = load_ual()
    data = read_quotes()
    return {
        "product": "UAL Counterfactual Engine",
        "version": "0.1.0",
        "fuel": fuel,
        "ual": ual,
        "manifest": manifest(fuel, ual),
        "quotes": quote_coverage(data),
        "fuel_last_price": fuel[-1]["usd_per_gallon"],
        "fuel_last_date": fuel[-1]["date"],
        "real_world_status": "EIA historical spot + UAL reported Q2 2026, modeled management response",
        "signal_gate": "not investable: pricing response not empirically identified or backtested"
    }

@app.post("/api/simulate")
def run_simulation(settings: ScenarioPayload):
    try:
        scenario = Scenario(**settings.model_dump())
        return simulate(load_fuel(), load_ual(), scenario)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

@app.get("/api/quotes")
def quotes(route: str = ""):
    data = read_quotes()
    return {"coverage": quote_coverage(data), "diagnostic": route_diagnostic(data, load_fuel(), load_ual(), route.strip().upper())}

@app.post("/api/quotes")
def upload_quotes(body: QuoteUpload):
    if HOSTED_DEMO:
        raise HTTPException(status_code=403, detail="Quote uploads are available in the local application only.")
    try:
        count = save_quotes(body.csv_text)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"count": count, "coverage": quote_coverage(read_quotes())}

@app.delete("/api/quotes")
def delete_quotes():
    if HOSTED_DEMO:
        raise HTTPException(status_code=403, detail="Quote changes are available in the local application only.")
    QUOTES_FILE.unlink(missing_ok=True)
    return {"count": 0}
