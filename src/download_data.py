"""Download the raw data: EIA spot prices (no API key) and NYMEX front-month futures (Yahoo).

Usage: python src/download_data.py [--refresh-eia]
Writes data/raw_futures.csv and data/manifest.json. data/raw_eia_spot.csv is committed (EIA data is
public domain) and is only re-downloaded if missing or with --refresh-eia, so the spot stays frozen.
"""
import hashlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import xlrd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hedge import END_DATE, START_DATE  # noqa: E402

DATA = Path(__file__).resolve().parents[1] / "data"
EIA_URL = "https://www.eia.gov/dnav/pet/xls/PET_PRI_SPT_S1_D.xls"
EIA_SERIES = {
    "EER_EPD2DXL0_PF4_Y35NY_DPG": "nyh_ulsd",  # NY Harbor ULSD, $/gal
    "EER_EPD2DXL0_PF4_RGC_DPG": "usgc_ulsd",  # US Gulf Coast ULSD, $/gal
    "RWTC": "wti_cushing",  # WTI Cushing spot, $/bbl
}
TICKERS = {"HO=F": "ho", "CL=F": "cl"}  # HO in $/gal, CL in $/bbl


def download_eia() -> pd.DataFrame:
    resp = requests.get(EIA_URL, timeout=60)
    resp.raise_for_status()
    sheets = pd.read_excel(io.BytesIO(resp.content), sheet_name=None, header=None)
    cols = {}
    for sheet in sheets.values():
        # row 1 holds the series IDs ("Sourcekey"), data starts at row 3
        if sheet.shape[0] < 4 or sheet.iloc[1, 0] != "Sourcekey":
            continue
        dates = pd.to_datetime(sheet.iloc[3:, 0])
        for j in range(1, sheet.shape[1]):
            sid = sheet.iloc[1, j]
            if sid in EIA_SERIES:
                cols[EIA_SERIES[sid]] = pd.Series(
                    pd.to_numeric(sheet.iloc[3:, j], errors="coerce").values, index=dates
                )
    missing = set(EIA_SERIES.values()) - set(cols)
    assert not missing, f"series not found in EIA workbook: {missing}"
    df = pd.DataFrame(cols).sort_index()
    df.index.name = "date"
    return df.loc[START_DATE:END_DATE]


def download_futures() -> pd.DataFrame:
    raw = yf.download(list(TICKERS), start=START_DATE, auto_adjust=False, progress=False)
    close = raw["Close"]  # flatten the (field, ticker) MultiIndex explicitly
    df = close[list(TICKERS)].rename(columns=TICKERS)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    df = df.loc[START_DATE:END_DATE]
    assert df["ho"].dropna().between(0.5, 6).all(), "HO=F should be in $/gal"
    assert df["cl"].median() > 20, "CL=F should be in $/bbl"
    return df


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    DATA.mkdir(exist_ok=True)
    eia_path = DATA / "raw_eia_spot.csv"
    if eia_path.exists() and "--refresh-eia" not in sys.argv:
        spot = pd.read_csv(eia_path, index_col=0, parse_dates=True)
        print(f"using committed {eia_path.name} (pass --refresh-eia to re-download)")
    else:
        spot = download_eia()
        spot.to_csv(eia_path)
    fut = download_futures()
    fut.to_csv(DATA / "raw_futures.csv")
    manifest = {
        "downloaded_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "start": START_DATE,
        "end": END_DATE,
        "versions": {"pandas": pd.__version__, "yfinance": yf.__version__, "xlrd": xlrd.__version__},
        "files": {
            name: {"rows": len(df), "sha256": sha256(DATA / name)}
            for name, df in [("raw_eia_spot.csv", spot), ("raw_futures.csv", fut)]
        },
        # witness values: if these change, the source has revised its history
        "witness": {
            "cl_2020-04-20": round(float(fut.loc["2020-04-20", "cl"]), 2),
            "nyh_ulsd_2022-05-02": round(float(spot.loc["2022-05-02", "nyh_ulsd"]), 3),
        },
    }
    (DATA / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
