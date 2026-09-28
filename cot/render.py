"""HTML 렌더링 (리포트 + 아카이브 인덱스)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import charts, config, metrics as mx

WEEKDAY_KR = "월화수목금토일"
# ⑫ 구글 시트 행의 열 순서 (기존 시트와 동일하게 유지)
SHEET_ORDER = ["ES", "NQ", "WTI", "EUR", "JPY", "BTC"]


def kr_date(d: date) -> str:
    return f"{d.year}년 {d.month}월 {d.day}일 ({WEEKDAY_KR[d.weekday()]})"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "templates"),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters.update({
        "num": charts.fmt_num,
        "stars": lambda n: "★" * int(n) + "☆" * (5 - int(n)),
        "badge": lambda r: {"Bullish": "bull", "Bearish": "bear"}.get(r, "neut"),
        "tone": lambda v: "bull" if v > 0 else ("bear" if v < 0 else "neut"),
        "pctcolor": charts.pct_color,
        "extreme": mx.extreme_label,
        "scorecolor": charts.score_color,
        "prob": lambda v: "—" if v is None else f"{v}%",
        "vclass": lambda v: {"Strong Buy": "v-buy", "Buy": "v-buy", "Hold": "v-hold"}.get(v, "v-sell"),
        "scorebg": lambda s: "#eaf3de" if s >= 60 else ("#faeeda" if s >= 40 else "#fcebeb"),
        "scorefg": lambda s: "#3B6D11" if s >= 60 else ("#854F0B" if s >= 40 else "#A32D2D"),
    })
    return env


def sheet_row(report_date: date, metrics: dict, analysis: dict) -> tuple[str, str]:
    header = ["날짜"] + [f"{k} Net" for k in SHEET_ORDER] + [f"{k}점수" for k in SHEET_ORDER] + ["투자의견", "핵심메모"]
    vals = [report_date.isoformat()]
    for k in SHEET_ORDER:
        vals.append(f"'{metrics[k]['net']:+,}" if k in metrics else "")
    for k in SHEET_ORDER:
        vals.append(str(analysis["markets"][k]["score"]) if k in metrics else "")
    vals += [analysis.get("sheet_opinion", ""), analysis.get("sheet_memo", "")]
    clean = [v.replace("\t", " ").replace("\n", " ") for v in vals]
    return "\t".join(header), "\t".join(clean)


def render_report(*, report_date: date, release: date, run_kst: datetime, metrics: dict,
                  analysis: dict, news: dict | None, data_sources: list[str], model: str,
                  index_link: str | None = "../index.html") -> str:
    markets = []
    for cfg in config.MARKETS:
        mt = metrics.get(cfg.key)
        item = {"cfg": cfg, "metrics": mt, "ai": analysis["markets"].get(cfg.key) if mt else None}
        if mt:
            item["chart"] = charts.history_svg(mt["chart"], cfg.groups, cfg.name)
            item["minibars"] = charts.mini_bars(mt["quant_history"])
        markets.append(item)

    missing = [i["cfg"].name for i in markets if not i["metrics"]]
    if not missing:
        coverage = "금융선물 + WTI 원유 완전 분석"
    else:
        coverage = "데이터 수신 대기: " + ", ".join(missing)

    gauge_rows = [{"label": k, "pct": m["pct_3y"], "pct_prev": m["pct_3y_4w_ago"]} for k, m in metrics.items()]
    extremes = ", ".join(f"{k}({m['pct_3y']}%ile)" for k, m in metrics.items()
                         if m["pct_3y"] <= 10 or m["pct_3y"] >= 90)
    biggest = sorted(metrics.values(), key=lambda m: abs(m["chg_z"]), reverse=True)[:5]
    hist_dates = [q["date"] for m in metrics.values() for q in m["quant_history"][-6:]]
    trend_range = (f"{min(hist_dates)} ~ {max(hist_dates)}, 6주" if hist_dates else "")
    header, row = sheet_row(report_date, metrics, analysis)

    return _env().get_template("report.html.j2").render(
        report_date_dot=report_date.strftime("%Y.%m.%d"),
        report_date_kr=kr_date(report_date), release_date_kr=kr_date(release),
        run_kst=run_kst.strftime("%Y-%m-%d %H:%M"),
        coverage_text=coverage, missing=missing,
        ai=analysis.get("ai", False), model=model,
        markets=markets, analysis=analysis, news=news,
        gauge=charts.gauge_svg(gauge_rows) if gauge_rows else "",
        extremes=extremes, biggest_moves=biggest,
        trend_range=trend_range,
        sheet_text=f"{header}\n{row}",
        data_sources=" / ".join(dict.fromkeys(data_sources)),
        index_link=index_link,
    )


def render_index(records: list[dict]) -> str:
    """records: 보고서 메타 목록 (최신순)."""
    keys = [m.key for m in config.MARKETS]
    rows = [{
        "date": r["report_date"], "link": f"reports/{r['report_date']}.html",
        "scores": r.get("scores", {}), "opinion": r.get("sheet_opinion", ""), "memo": r.get("sheet_memo", ""),
    } for r in records]
    latest = None
    if rows:
        latest = {"date": rows[0]["date"], "link": rows[0]["link"],
                  "opinion": rows[0]["opinion"], "memo": rows[0]["memo"]}
    return _env().get_template("index.html.j2").render(keys=keys, rows=rows, latest=latest)
