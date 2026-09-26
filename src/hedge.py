"""Hedge-effectiveness toolkit for a physical diesel position (pure functions, prices in $/bbl)."""
import numpy as np
import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay, USMartinLutherKingJr,
    USMemorialDay, USPresidentsDay, USThanksgivingDay, nearest_workday, sunday_to_monday,
)

START_DATE = "2013-05-01"  # first full month after NYMEX HO switched to ULSD (May 2013 contract)
END_DATE = "2026-08-31"
GAL_PER_BBL = 42

# Fixed before computing the per-regime numbers, but after a preliminary out-of-sample pass that
# already showed 2022 (hence 2021 and 2022 split). Sources for the 2026 dates: see README.
REGIMES = [
    ("2013-19", "2013-05-01", "2019-12-31"),
    ("2020", "2020-01-01", "2020-12-31"),
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023-Feb 26", "2023-01-01", "2026-02-28"),
    ("Iran war 2026", "2026-03-01", "2026-08-31"),
]
OOS_YEARS = range(2017, 2027)
OOS_WINDOW = 156  # weeks used to estimate h before each test year


# ---------------------------------------------------------------- calendar and rolls
class CMEHolidays(AbstractHolidayCalendar):
    """NYMEX energy holidays (no settlement). Columbus and Veterans Day are trading days."""
    rules = [
        Holiday("New Year", month=1, day=1, observance=sunday_to_monday),
        USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
        Holiday("Juneteenth", month=6, day=19, start_date="2022-01-01", observance=nearest_workday),
        Holiday("Independence Day", month=7, day=4, observance=nearest_workday),
        USLaborDay, USThanksgivingDay,
        Holiday("Christmas", month=12, day=25, observance=nearest_workday),
    ]


SPECIAL_CLOSURES = pd.DatetimeIndex(["2018-12-05"])  # national day of mourning (G.H.W. Bush)


def business_days(start: str, end: str) -> pd.DatetimeIndex:
    """Weekdays minus CME holidays: the calendar used to count days before expiry."""
    days = pd.bdate_range(start, end)
    return days.difference(CMEHolidays().holidays(start, end)).difference(SPECIAL_CLOSURES)


def ho_expiry(delivery: pd.Period, bdays: pd.DatetimeIndex) -> pd.Timestamp:
    """HO: last business day of the month before delivery."""
    prev = delivery - 1
    return bdays[(bdays.year == prev.year) & (bdays.month == prev.month)][-1]


def cl_expiry(delivery: pd.Period, bdays: pd.DatetimeIndex) -> pd.Timestamp:
    """CL: 3 business days before the 25th of the month before delivery;
    if the 25th is not a business day, 3 business days before the last business day before the 25th."""
    prev = delivery - 1
    d25 = pd.Timestamp(prev.year, prev.month, 25)
    before = bdays[bdays < d25]
    return before[-3] if d25 in bdays else before[-4]


def roll_days(bdays: pd.DatetimeIndex, expiry_fn) -> pd.DatetimeIndex:
    """First business day after each expiry: the day the continuous series switches contract."""
    months = pd.period_range(bdays[0], bdays[-1], freq="M") + 1
    rolls = []
    for m in months:
        exp = expiry_fn(m, bdays)
        nxt = bdays[bdays > exp]
        if len(nxt) and exp >= bdays[0]:
            rolls.append(nxt[0])
    return pd.DatetimeIndex(rolls)


def mark_roll_changes(common_days: pd.DatetimeIndex, rolls: pd.DatetimeIndex) -> pd.Series:
    """Flag every common-day change (t_prev, t] that contains at least one contract switch.

    Works even when the roll day itself is missing from one source: the change that spans it
    is flagged. Returns a boolean Series indexed by common_days (t)."""
    rolls = rolls[(rolls > common_days[0]) & (rolls <= common_days[-1])]
    pos = common_days.searchsorted(rolls, side="left")  # first common day >= roll
    flag = pd.Series(False, index=common_days)
    flag.iloc[pos] = True
    # each roll lies inside the change it flags: common_days[pos-1] < roll <= common_days[pos]
    # (two rolls inside the same change give a single flag)
    assert (common_days[pos - 1] < rolls).all() and (rolls <= common_days[pos]).all()
    assert flag.groupby(common_days.to_period("M")).sum().max() <= 2
    return flag


def roll_alignment(prices: pd.DataFrame, rolls: pd.DatetimeIndex, future: str, spot: str,
                   shifts=range(-2, 3)) -> pd.Series:
    """Median |dF - dS| on the calendar roll days, with the calendar shifted by k common days.

    If the calendar matches the data, the jump is largest at shift 0."""
    idx = prices.index
    jump = (prices[future].diff() - prices[spot].diff()).abs()
    pos = idx.searchsorted(rolls[(rolls > idx[0]) & (rolls <= idx[-1])])
    out = {}
    for k in shifts:
        p = pos + k
        out[k] = jump.iloc[p[(p > 0) & (p < len(idx))]].median()
    return pd.Series(out, name=f"median |d{future} - d{spot}|")


# ---------------------------------------------------------------- weekly changes
def weekly_changes(prices: pd.DataFrame, flag: pd.Series | None) -> pd.DataFrame:
    """Weekly (Friday-ending) price changes in $/bbl.

    flag given  -> main spec: daily changes on common days, flagged (roll) changes dropped for
                   all series, summed by week.
    flag None   -> sensitivity: raw change between the last common day of consecutive weeks.
    Weeks labelled after END_DATE are dropped (END_DATE is a Monday: no partial week in September)."""
    if flag is None:
        w = prices.resample("W-FRI").last().diff().dropna()
    else:
        daily = prices.diff().iloc[1:]
        daily = daily[~flag.reindex(daily.index).to_numpy()]
        w = daily.resample("W-FRI").sum(min_count=1).dropna()
    return w[w.index <= pd.Timestamp(END_DATE)]


# ---------------------------------------------------------------- hedging
def hedge_ratio(ds: pd.Series, df: pd.Series) -> float:
    """Minimum-variance hedge ratio h* = Cov(dS, dF) / Var(dF) (= OLS slope)."""
    return float(np.cov(ds, df)[0, 1] / np.var(df, ddof=1))


def effectiveness(ds: pd.Series, df: pd.Series, h: float) -> float:
    """1 - Var(dS - h dF) / Var(dS). In-sample with h = h* this equals R^2."""
    return float(1 - np.var(ds - h * df, ddof=1) / np.var(ds, ddof=1))


def strategies(ds: pd.Series, ho: pd.Series, wti: pd.Series) -> dict:
    """In-sample effectiveness of the five strategies, plus h* for each future."""
    h_ho, h_wti = hedge_ratio(ds, ho), hedge_ratio(ds, wti)
    return {
        "N": len(ds),
        "unhedged": 0.0,
        "HO 1:1": effectiveness(ds, ho, 1.0),
        "HO h*": effectiveness(ds, ho, h_ho),
        "WTI 1:1": effectiveness(ds, wti, 1.0),
        "WTI h*": effectiveness(ds, wti, h_wti),
        "h* HO": h_ho,
        "h* WTI": h_wti,
    }


def block_bootstrap_ci(ds, df, h=None, block=8, n=2000, level=0.90, seed=0):
    """Moving-block bootstrap CI for effectiveness over weeks.

    h fixed (out-of-sample) -> only the evaluation weeks are resampled.
    h None (in-sample)      -> h* re-estimated on each draw."""
    rng = np.random.default_rng(seed)
    x, y = np.asarray(ds, float), np.asarray(df, float)
    T = len(x)
    block = min(block, T)
    starts = np.arange(T - block + 1)
    k = int(np.ceil(T / block))
    out = np.empty(n)
    for i in range(n):
        idx = np.concatenate([np.arange(s, s + block) for s in rng.choice(starts, k)])[:T]
        xs, ys = x[idx], y[idx]
        hh = hedge_ratio(xs, ys) if h is None else h
        out[i] = 1 - np.var(xs - hh * ys, ddof=1) / np.var(xs, ddof=1)
    a = (1 - level) / 2
    return float(np.quantile(out, a)), float(np.quantile(out, 1 - a))


def out_of_sample(w: pd.DataFrame, spot: str, future: str, years=OOS_YEARS, window=OOS_WINDOW,
                  ci=True) -> pd.DataFrame:
    """For each calendar year T: h* from the `window` weeks before 1 Jan T, applied to the weeks of T.

    A week belongs to the year of its Friday end date."""
    rows = []
    for year in years:
        cut = pd.Timestamp(year, 1, 1)
        est = w[w.index < cut].iloc[-window:]
        test = w[(w.index >= cut) & (w.index.year == year)]
        assert len(est) == window and est.index[0] >= pd.Timestamp(START_DATE)
        assert est.index.max() < test.index.min()  # estimation strictly before evaluation
        h = hedge_ratio(est[spot], est[future])
        row = {"year": year, "N": len(test), "h": h,
               "eff": effectiveness(test[spot], test[future], h)}
        if ci:
            b = 4 if len(test) < 52 else 8
            row["lo"], row["hi"] = block_bootstrap_ci(test[spot], test[future], h=h, block=b)
        rows.append(row)
    return pd.DataFrame(rows).set_index("year")


def by_regime(w: pd.DataFrame, spot: str, regimes=REGIMES) -> pd.DataFrame:
    rows = {}
    for name, a, b in regimes:
        sub = w.loc[a:b]
        rows[name] = strategies(sub[spot], sub["ho"], sub["cl"])
    return pd.DataFrame(rows).T


def frozen_runs(s: pd.Series, min_len=3) -> pd.DataFrame:
    """Runs of >= min_len identical consecutive values (possible stale quotes)."""
    grp = (s != s.shift()).cumsum()
    runs = s.groupby(grp).agg(["first", "size"])
    runs["start"] = s.index.to_series().groupby(grp.values).first().values
    return runs[runs["size"] >= min_len][["start", "first", "size"]]
