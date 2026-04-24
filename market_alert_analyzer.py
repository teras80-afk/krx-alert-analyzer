"""
투경예고 / 단기과열예고 임계값 조회기 + 관심종목 대시보드 + 현재 지정종목
────────────────────────────────────────────────────────────────────
 [1] 개별 종목 조회 (차트 포함)
 [2] 관심종목 대시보드 (GitHub 저장 + 근접 경고)
 [3] 현재 지정종목 (네이버 금융 실시간 반영, 지정해제 시 자동 제외)
"""
import streamlit as st
import pandas as pd
import requests
import base64
import altair as alt
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from io import StringIO

st.set_page_config(page_title="예고 임계값 조회기", layout="wide")
st.title("🔍 투경예고 / 단기과열예고 조회")

PROXIMITY_PCT = 0.95


# ─────────────────────────────────────────────────────────────
# 사이드바 — 임계값 프리셋 설정
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### ⚙️ 임계값 설정")
    st.caption("KRX 유형별로 기준이 달라서 조정 가능하게 해뒀습니다. "
               "기본값은 실제 예고 사례 분석 기반.")

    preset = st.selectbox(
        "프리셋",
        [
            "단기상승&불건전 (5일×1.45 / 15일×1.75) — 기본",
            "중장기상승&불건전 (20일×1.60 / 60일×2.00)",
            "위험 해제요건 (5일×1.60 / 15일×2.00)",
            "구버전 (5일×1.60 / 20일×2.00)",
            "직접 설정",
        ],
        index=0,
        key="threshold_preset",
    )

    if preset.startswith("단기상승"):
        short_days, short_mult = 5, 1.45
        long_days, long_mult = 15, 1.75
    elif preset.startswith("중장기상승"):
        short_days, short_mult = 20, 1.60
        long_days, long_mult = 60, 2.00
    elif preset.startswith("위험"):
        short_days, short_mult = 5, 1.60
        long_days, long_mult = 15, 2.00
    elif preset.startswith("구버전"):
        short_days, short_mult = 5, 1.60
        long_days, long_mult = 20, 2.00
    else:
        # 직접 설정
        short_days = st.number_input("단기 기준일수", 2, 30, 5, key="sd_short")
        short_mult = st.number_input("단기 배수", 1.0, 3.0, 1.45, 0.05,
                                      key="sm_short")
        long_days = st.number_input("장기 기준일수", 5, 100, 15, key="sd_long")
        long_mult = st.number_input("장기 배수", 1.0, 5.0, 1.75, 0.05,
                                     key="sm_long")

    if not preset.startswith("직접"):
        st.markdown(f"- 단기: **{short_days}일 × {short_mult:.2f}**")
        st.markdown(f"- 장기: **{long_days}일 × {long_mult:.2f}**")

    require_max15 = st.checkbox("③ 15일 최고가 조건 적용", value=True,
                                 key="require_max15",
                                 help="일부 유형은 이 조건이 없음")

    st.markdown("---")
    st.caption("💡 가온전선처럼 기본값으로 탈락한 종목이 있다면 "
               "프리셋을 '단기상승&불건전'으로 두세요 (현재 기본값).")


# ─────────────────────────────────────────────────────────────
# 데이터 로더
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_ticker_name_map() -> dict:
    df = fdr.StockListing("KRX")
    code_col = "Code" if "Code" in df.columns else "Symbol"
    return dict(zip(df[code_col].astype(str).str.zfill(6), df["Name"]))


def resolve_ticker(user_input: str, name_map: dict) -> str | None:
    s = user_input.strip()
    if s.isdigit() and len(s) == 6:
        return s if s in name_map else None
    for t, n in name_map.items():
        if n == s:
            return t
    hits = [t for t, n in name_map.items() if s in n]
    return hits[0] if len(hits) == 1 else None


@st.cache_data(ttl=600)
def load_ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame:
    df = fdr.DataReader(ticker, start, end)
    df = df.rename(columns={"Open": "시가", "High": "고가", "Low": "저가",
                            "Close": "종가", "Volume": "거래량"})
    return df[~df.index.duplicated(keep="last")].sort_index()


# ─────────────────────────────────────────────────────────────
# 현재 지정종목 스크래핑 (네이버 금융)
# ─────────────────────────────────────────────────────────────
NAVER_URLS = {
    "투자경고": [
        "https://finance.naver.com/sise/investment_alert.naver?type=warning",
        "https://finance.naver.com/sise/investment_alert.nhn?type=warning",
    ],
    "투자위험": [
        "https://finance.naver.com/sise/investment_alert.naver?type=risk",
        "https://finance.naver.com/sise/investment_alert.nhn?type=risk",
    ],
    "단기과열": [
        "https://finance.naver.com/sise/investment_alert.naver?type=short_period",
        "https://finance.naver.com/sise/investment_alert.nhn?type=short_period",
    ],
}


def calculate_release_date(category: str, designated_date_str: str) -> str:
    """지정일에서 해제 판단일 계산 (거래일 기준)
    - 단기과열: 지정일 + 3 거래일 (확정 해제일)
    - 투자경고/투자위험: 지정일 + 10 거래일 (해제 평가 시작일)
    """
    if not designated_date_str or designated_date_str in ("—", "nan", ""):
        return "—"

    # 다양한 날짜 포맷 시도
    dt = None
    s = str(designated_date_str).strip()
    current_year = datetime.now().year

    for fmt in ["%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d"]:
        try:
            dt = pd.to_datetime(s, format=fmt)
            break
        except Exception:
            continue

    # 년도 없는 MM.DD 같은 경우 — 현재 연도 가정
    if dt is None:
        for fmt in ["%m.%d", "%m-%d", "%m/%d"]:
            try:
                normalized = s.replace('-', '.').replace('/', '.')
                dt = pd.to_datetime(f"{current_year}.{normalized}",
                                    format="%Y.%m.%d")
                break
            except Exception:
                continue

    if dt is None:
        return "—"

    # 거래일 기준 계산 (주말 제외, 공휴일 미반영이라 ±1일 오차 가능)
    days = 3 if category == "단기과열" else 10
    release_dt = dt + pd.offsets.BDay(days)

    return release_dt.strftime("%Y-%m-%d")


def _parse_naver_date(s: str) -> str | None:
    """네이버의 다양한 날짜 포맷을 YYYY-MM-DD로 정규화"""
    s = str(s).strip()
    if not s or s in ("nan", "None"):
        return None

    # YY.MM.DD 또는 YYYY.MM.DD
    for fmt in ["%Y.%m.%d", "%y.%m.%d", "%Y-%m-%d", "%y-%m-%d",
                "%Y/%m/%d", "%y/%m/%d"]:
        try:
            dt = pd.to_datetime(s, format=fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            continue

    # 시간 붙은 경우 ("2026.04.22 16:30" 등) 날짜만 추출 후 재시도
    import re
    m = re.match(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})", s)
    if m:
        y, mo, d = m.groups()
        if len(y) == 2:
            y = "20" + y
        try:
            dt = pd.to_datetime(f"{y}-{mo}-{d}")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    return None


@st.cache_data(ttl=1800)
def fetch_designation_date(code: str, category: str) -> tuple[str | None, str]:
    """
    네이버 개별 종목 공시 페이지에서 지정일 파싱.
    반환: (YYYY-MM-DD 또는 None, 상태메시지)
    """
    keyword_map = {
        "단기과열": "단기과열",
        "투자경고": "투자경고",
        "투자위험": "투자위험",
    }
    keyword = keyword_map.get(category)
    if not keyword:
        return None, "unknown_category"

    urls = [
        f"https://finance.naver.com/item/news_notice.naver?code={code}&page=1",
        f"https://finance.naver.com/item/news_notice.nhn?code={code}&page=1",
    ]
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
        "Referer": "https://finance.naver.com/",
    }

    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            html = r.content.decode("euc-kr", errors="replace")
            tables = pd.read_html(StringIO(html))
        except Exception:
            continue

        for table in tables:
            if len(table) == 0:
                continue
            # 제목/날짜 컬럼 탐지
            title_col = None
            date_col = None
            for col in table.columns:
                col_str = str(col)
                if "제목" in col_str:
                    title_col = col
                elif "날짜" in col_str or "일시" in col_str or "일자" in col_str:
                    date_col = col

            if title_col is None:
                continue

            for _, row in table.iterrows():
                title = str(row.get(title_col, ""))
                if not title or title == "nan":
                    continue
                # 카테고리 키워드 + '지정' 포함, 단 '예고'/'해제'는 제외
                if keyword not in title:
                    continue
                if "지정" not in title:
                    continue
                if "예고" in title or "해제" in title:
                    continue
                # 지정일 추출
                if date_col:
                    date_raw = row.get(date_col, "")
                    parsed = _parse_naver_date(str(date_raw))
                    if parsed:
                        return parsed, "ok"
        # 표는 찾았지만 매칭되는 공시가 없음 → 다음 URL 시도 안 하고 바로 종료
        return None, "no_match"

    return None, "fetch_failed"


def analyze_retrospective_thresholds(df_ohlcv: pd.DataFrame,
                                      designation_date: str) -> dict | None:
    """
    지정일 직전 거래일(T-1) 기준으로 상승률을 역산.
    반환: {'base_idx': i, 'close_tm1': 종가, 'ret_5d': 비율, 'ret_20d': 비율,
           'max15_reached': bool, 'close_tm1_date': 날짜}
    실패 시 None
    """
    if df_ohlcv.empty or not designation_date or designation_date == "—":
        return None

    try:
        dt = pd.Timestamp(designation_date)
    except Exception:
        return None

    # 지정일 "이전"의 가장 가까운 거래일 찾기 (T-1)
    prior = df_ohlcv.index[df_ohlcv.index < dt]
    if len(prior) == 0:
        return None

    tm1_date = prior[-1]
    idx = df_ohlcv.index.get_loc(tm1_date)
    if isinstance(idx, slice):
        idx = idx.stop - 1

    if idx < 21:  # 20일 전 데이터 필요
        return None

    close_tm1 = float(df_ohlcv["종가"].iloc[idx])
    close_5d = float(df_ohlcv["종가"].iloc[idx - 5])
    close_20d = float(df_ohlcv["종가"].iloc[idx - 20])
    max_15 = float(df_ohlcv["종가"].iloc[idx - 14: idx + 1].max())

    return {
        "base_idx": idx,
        "close_tm1": close_tm1,
        "close_tm1_date": tm1_date.strftime("%Y-%m-%d"),
        "ret_5d": close_tm1 / close_5d if close_5d > 0 else None,
        "ret_20d": close_tm1 / close_20d if close_20d > 0 else None,
        "max15_reached": close_tm1 >= max_15,
    }


@st.cache_data(ttl=600)
def fetch_designated_stocks(category: str) -> tuple[pd.DataFrame, str]:
    """네이버 금융에서 지정종목 리스트 스크래핑"""
    urls = NAVER_URLS.get(category, [])
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
    }

    last_err = ""
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
                continue

            # 네이버는 EUC-KR 인코딩
            html = r.content.decode("euc-kr", errors="replace")

            # pandas read_html로 테이블 추출
            tables = pd.read_html(StringIO(html))

            # 가장 행이 많은 테이블이 종목 리스트
            best = None
            for t in tables:
                if len(t) > 0 and "종목명" in str(t.columns.tolist()):
                    if best is None or len(t) > len(best):
                        best = t
            if best is None and tables:
                # 종목명 컬럼이 없으면 가장 큰 테이블
                best = max(tables, key=len)

            if best is not None and len(best) > 0:
                # NaN 행 제거
                best = best.dropna(how="all").reset_index(drop=True)
                return best, "ok"
        except Exception as e:
            last_err = str(e)[:100]
            continue

    return pd.DataFrame(), f"데이터 조회 실패: {last_err}"


# ─────────────────────────────────────────────────────────────
# GitHub 연동
# ─────────────────────────────────────────────────────────────
def _github_config():
    try:
        return {
            "token": st.secrets["GITHUB_TOKEN"],
            "repo": st.secrets["GITHUB_REPO"],
            "branch": st.secrets.get("GITHUB_BRANCH", "main"),
            "path": st.secrets.get("WATCHLIST_PATH", "watchlist.txt"),
        }
    except Exception:
        return None


def github_get_watchlist():
    cfg = _github_config()
    if not cfg:
        return "", None
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{cfg['path']}"
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers,
                         params={"ref": cfg["branch"]}, timeout=10)
        if r.status_code == 200:
            js = r.json()
            return base64.b64decode(js["content"]).decode("utf-8"), js["sha"]
    except Exception:
        pass
    return "", None


def github_put_watchlist(new_content, sha):
    cfg = _github_config()
    if not cfg:
        return False, "GitHub 연동 설정 없음"
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{cfg['path']}"
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Accept": "application/vnd.github+json"}
    body = {
        "message": f"Update watchlist ({datetime.now():%Y-%m-%d %H:%M})",
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": cfg["branch"],
    }
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=body, timeout=10)
        if r.status_code in (200, 201):
            return True, "✅ 저장 완료"
        return False, f"❌ HTTP {r.status_code}"
    except Exception as e:
        return False, f"❌ {e}"


def parse_watchlist(text):
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]


def add_to_watchlist(stock_names: list) -> str:
    """관심종목 리스트에 종목 추가 (중복 제외)"""
    current_text = st.session_state.get("watchlist_text", "")
    current_set = set(parse_watchlist(current_text))
    to_add = [n for n in stock_names if n and n not in current_set]
    if not to_add:
        return "이미 모두 관심종목에 있습니다"
    new_lines = current_text.rstrip() + "\n" + "\n".join(to_add)
    ok, msg = github_put_watchlist(new_lines,
                                    st.session_state.get("watchlist_sha"))
    if ok:
        st.session_state.watchlist_text = new_lines
        _, new_sha = github_get_watchlist()
        st.session_state.watchlist_sha = new_sha
        return f"✅ {len(to_add)}개 종목 추가됨"
    return f"❌ 저장 실패: {msg}"


# ─────────────────────────────────────────────────────────────
# 이상상태 & 예고 판정
# ─────────────────────────────────────────────────────────────
def detect_anomaly(df):
    if df.empty:
        return {"anomaly": True, "reason": "데이터 없음"}
    recent5 = df["거래량"].tail(5)
    zero_days = int((recent5 == 0).sum())
    if zero_days >= 3:
        return {"anomaly": True,
                "reason": f"최근 5거래일 중 {zero_days}일 거래량 0"}
    try:
        days_ago = (pd.Timestamp.now().normalize()
                    - df.index[-1].normalize()).days
    except Exception:
        days_ago = 0
    if days_ago > 10:
        return {"anomaly": True, "reason": f"최종 거래일이 {days_ago}일 전"}
    return {"anomaly": False, "reason": ""}


def evaluate_warning(df, idx,
                      short_days: int = 5, short_mult: float = 1.45,
                      long_days: int = 15, long_mult: float = 1.75,
                      require_max15: bool = True):
    """투자경고/위험 요건 판정.
    기본값은 '단기상승&불건전' 해제요건(5일 +45%, 15일 +75%) 기반.
    투자위험으로 쓸 때는 (5, 1.60, 15, 2.00)으로 호출.
    """
    curr = int(df["종가"].iloc[idx])
    required = max(long_days, short_days)
    if idx < required:
        return {"status": None, "reason": f"데이터 부족({required}거래일 미만)"}
    p_short = int(df["종가"].iloc[idx - short_days])
    p_long = int(df["종가"].iloc[idx - long_days])
    max15 = int(df["종가"].iloc[idx - 14: idx + 1].max())
    th1 = int(p_short * short_mult)
    th2 = int(p_long * long_mult)
    th3 = max15
    c1 = curr >= th1
    c2 = curr >= th2
    c3 = (curr >= th3) if require_max15 else True
    ratios = [
        curr / th1 if th1 else 0,
        curr / th2 if th2 else 0,
        curr / th3 if (th3 and require_max15) else (1.0 if c3 else 0),
    ]
    return {
        "status": all([c1, c2, c3]), "current": curr,
        "thresholds": {
            f"{short_days}일x{short_mult:.2f}": th1,
            f"{long_days}일x{long_mult:.2f}": th2,
            "15일최고": th3,
        },
        "criteria": [
            (f"① {short_days}일 전 × {short_mult:.2f}", th1, c1, ratios[0]),
            (f"② {long_days}일 전 × {long_mult:.2f}", th2, c2, ratios[1]),
            ("③ 15일 최고가", th3, c3, ratios[2]),
        ],
        "max_ratio": max(ratios),
    }


def evaluate_overheat(df, idx):
    if idx < 40:
        return {"status": None, "reason": "데이터 부족"}
    curr = int(df["종가"].iloc[idx])
    avg40_close = df["종가"].iloc[idx - 39: idx + 1].mean()
    price_th = int(avg40_close * 1.3)
    c1 = curr >= price_th
    r1 = curr / price_th if price_th else 0
    avg2_vol = df["거래량"].iloc[idx - 1: idx + 1].mean()
    avg40_vol = df["거래량"].iloc[idx - 39: idx + 1].mean()
    vol_ratio = avg2_vol / avg40_vol if avg40_vol > 0 else 0
    c2 = vol_ratio >= 6.0
    r2 = vol_ratio / 6.0
    prev_close = df["종가"].shift(1)
    daily_vola = (df["고가"] - df["저가"]) / prev_close
    avg2_vola = daily_vola.iloc[idx - 1: idx + 1].mean()
    avg40_vola = daily_vola.iloc[idx - 39: idx + 1].mean()
    vola_ratio = avg2_vola / avg40_vola if avg40_vola > 0 else 0
    c3 = vola_ratio >= 1.5
    r3 = vola_ratio / 1.5
    return {
        "status": all([c1, c2, c3]), "current": curr,
        "thresholds": {"주가x1.3": price_th},
        "criteria": [
            ("① 주가 (40일평균 × 1.3)", f"{price_th:,}원",
             f"{curr:,}원", c1, r1),
            ("② 회전율 (40일평균 × 6배)", f"{avg40_vol * 6:,.0f}주",
             f"{avg2_vol:,.0f}주 ({vol_ratio:.2f}배)", c2, r2),
            ("③ 변동성 (40일평균 × 1.5배)", f"{avg40_vola * 1.5 * 100:.2f}%",
             f"{avg2_vola * 100:.2f}% ({vola_ratio:.2f}배)", c3, r3),
        ],
        "max_ratio": max(r1, r2, r3),
    }


def detect_predesignation_history(df, base_idx, evaluator_func, lookback=10):
    """
    기준일 이전 lookback 거래일 동안 예고 조건 충족 이력을 확인.
    반환:
    - is_predesignated: 현재 예고 상태인지
    - trigger_date: 첫 예고 발동일 (YYYY-MM-DD)
    - days_remaining: 지정 유예기간 남은 거래일수 (최대 10)
    """
    start_idx = max(0, base_idx - lookback)
    for i in range(start_idx, base_idx):  # base_idx 자신(오늘)은 제외
        try:
            ev = evaluator_func(df, i)
            if ev.get("status"):
                trigger_idx = i
                trigger_date = df.index[i].strftime("%Y-%m-%d")
                days_elapsed = base_idx - trigger_idx
                days_remaining = lookback - days_elapsed
                return True, trigger_date, days_remaining
        except Exception:
            continue
    return False, None, None


def status_label(ev):
    if ev.get("status") is None:
        return "—", 99
    if ev["status"]:
        return "🔴 해당", 0
    n_crit = len(ev["criteria"])
    n_ok = sum(1 for row in ev["criteria"] if row[2])
    max_r = ev.get("max_ratio", 0)
    if max_r >= PROXIMITY_PCT:
        return f"🟡 근접 ({int(max_r*100)}%, {n_ok}/{n_crit})", 1
    return f"🟢 {n_ok}/{n_crit}", 2


def fmt_warning_table(ev):
    return pd.DataFrame([
        {"조건": k, "임계값": f"{th:,}원",
         "현재가": f"{ev['current']:,}원",
         "근접도": f"{r*100:.1f}%",
         "충족": "✅" if ok else "❌"}
        for (k, th, ok, r) in ev["criteria"]])


def fmt_overheat_table(ev):
    return pd.DataFrame([
        {"조건": k, "기준값": th, "현재값": cur,
         "근접도": f"{r*100:.1f}%",
         "충족": "✅" if ok else "❌"}
        for (k, th, cur, ok, r) in ev["criteria"]])


def build_price_chart(df, warn_ev, oh_ev, days=60):
    recent = df.tail(days).reset_index()
    recent = recent.rename(columns={recent.columns[0]: "날짜"})
    base = alt.Chart(recent).mark_line(point=True, color="#d35400").encode(
        x=alt.X("날짜:T", title=""),
        y=alt.Y("종가:Q", title="종가 (원)", scale=alt.Scale(zero=False)),
        tooltip=["날짜:T", alt.Tooltip("종가:Q", format=",")]
    ).properties(height=340)
    layers = [base]

    def hline(v, label, color, dash):
        rule_df = pd.DataFrame({"y": [v], "label": [label]})
        rule = alt.Chart(rule_df).mark_rule(
            color=color, strokeDash=dash, size=1.5
        ).encode(y="y:Q")
        text = alt.Chart(rule_df).mark_text(
            align="left", dx=5, dy=-5, color=color, fontSize=11
        ).encode(x=alt.value(5), y="y:Q", text="label:N")
        return [rule, text]

    if warn_ev.get("thresholds"):
        t = warn_ev["thresholds"]
        layers += hline(t["5일x1.6"], "투경 ① 5일×1.6", "#c0392b", [4, 4])
        layers += hline(t["20일x2.0"], "투경 ② 20일×2.0", "#8e44ad", [4, 4])
        layers += hline(t["15일최고"], "투경 ③ 15일최고", "#16a085", [4, 4])
    if oh_ev.get("thresholds"):
        layers += hline(oh_ev["thresholds"]["주가x1.3"],
                        "단기과열 ① 주가×1.3", "#2980b9", [2, 2])
    return alt.layer(*layers).resolve_scale(y="shared")


# ─────────────────────────────────────────────────────────────
# 종목명 매핑
# ─────────────────────────────────────────────────────────────
try:
    name_map = get_ticker_name_map()
except Exception as e:
    st.error(f"종목 리스트 로딩 실패: {e}")
    st.stop()


# ═══════════════════════════════════════════════════════════
# 탭 4개
# ═══════════════════════════════════════════════════════════
tab1, tab2, tab3, tab4 = st.tabs([
    "🎯 개별 종목 조회",
    "📋 관심종목 대시보드",
    "📌 현재 지정종목",
    "🔬 진단",
])

# ───────────────────────────────────────────────────────────
# 탭 1: 개별 종목 조회
# ───────────────────────────────────────────────────────────
with tab1:
    col_input, col_date_space = st.columns([2, 1])
    with col_input:
        user_input = st.text_input("종목코드 6자리 또는 종목명",
                                   value="009150", key="single_input")

    if user_input:
        ticker = resolve_ticker(user_input, name_map)
        if not ticker:
            st.error("종목을 찾지 못했습니다.")
        else:
            name = name_map[ticker]
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

            try:
                df = load_ohlcv(ticker, start, end)
            except Exception as e:
                st.error(f"주가 데이터 로딩 실패: {e}")
                st.stop()

            if df.empty:
                st.error("주가 데이터가 비어 있습니다.")
            else:
                anomaly = detect_anomaly(df)
                if anomaly["anomaly"]:
                    st.warning(f"🚨 **이상상태 감지**: {anomaly['reason']}")

                with col_date_space:
                    date_list = df.index.strftime("%Y-%m-%d").tolist()[::-1]
                    selected_date = st.selectbox("기준일", date_list, index=0,
                                                  key="single_date")

                base_idx = df.index.get_loc(pd.Timestamp(selected_date))
                if isinstance(base_idx, slice):
                    base_idx = base_idx.stop - 1

                # 사이드바 설정 적용한 평가 함수
                def warn_eval_with_params(d, i):
                    return evaluate_warning(d, i,
                                            short_days=short_days,
                                            short_mult=short_mult,
                                            long_days=long_days,
                                            long_mult=long_mult,
                                            require_max15=require_max15)

                warn_ev = warn_eval_with_params(df, base_idx)
                oh_ev = evaluate_overheat(df, base_idx)
                curr_p = int(df["종가"].iloc[base_idx])

                # 최근 10거래일 예고 이력 스캔 → 현재 '예고 중' 여부
                warn_pre, warn_trigger, warn_days_left = \
                    detect_predesignation_history(df, base_idx, warn_eval_with_params)
                oh_pre, oh_trigger, oh_days_left = \
                    detect_predesignation_history(df, base_idx, evaluate_overheat)

                st.markdown(f"### 📍 {name} ({ticker})")
                st.markdown(f"**{selected_date} 종가:** "
                            f"<span style='font-size:22px;color:#d35400'>"
                            f"{curr_p:,}원</span>", unsafe_allow_html=True)
                st.markdown("---")

                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("#### ⚠️ 투자경고")

                    # 현재 예고 상태 설명
                    if warn_pre:
                        st.warning(
                            f"🟠 **예고 상태 지속 중** — "
                            f"예고 발동일 추정: {warn_trigger} "
                            f"(지정까지 {warn_days_left}거래일 남음)"
                        )
                        table_label = "**🎯 지정 임계값** (예고 상태 → 재충족 시 지정 발동)"
                    else:
                        st.info("🔵 최근 10거래일 내 예고 이력 없음 → "
                                "오늘 충족 시 **예고** 발동")
                        table_label = "**💡 예고 임계값** (충족 시 예고 발동)"

                    # 오늘 판정
                    if warn_ev["status"] is None:
                        st.info(warn_ev["reason"])
                    else:
                        label, _ = status_label(warn_ev)
                        if warn_ev["status"]:
                            if warn_pre:
                                st.error(f"🚨 {label} — **지정 발동!** 3요건 재충족")
                            else:
                                st.error(f"🔴 {label} — 3요건 모두 충족 → 예고 발동")
                        elif "근접" in label:
                            st.warning(f"{label} — 근접 중")
                        else:
                            st.success(label)

                    if warn_ev.get("criteria"):
                        st.markdown(table_label)
                        st.dataframe(fmt_warning_table(warn_ev),
                                     use_container_width=True, hide_index=True)

                with c2:
                    st.markdown("#### 🔥 단기과열")

                    if oh_pre:
                        st.warning(
                            f"🟠 **예고 상태 지속 중** — "
                            f"예고 발동일 추정: {oh_trigger} "
                            f"(지정까지 {oh_days_left}거래일 남음)"
                        )
                        table_label = "**🎯 지정 임계값** (예고 상태 → 재충족 시 지정 발동)"
                    else:
                        st.info("🔵 최근 10거래일 내 예고 이력 없음 → "
                                "오늘 충족 시 **예고** 발동")
                        table_label = "**💡 예고 임계값** (충족 시 예고 발동)"

                    if oh_ev["status"] is None:
                        st.info(oh_ev["reason"])
                    else:
                        label, _ = status_label(oh_ev)
                        if oh_ev["status"]:
                            if oh_pre:
                                st.error(f"🚨 {label} — **지정 발동!** 3요건 재충족")
                            else:
                                st.error(f"🔴 {label} — 3요건 모두 충족 → 예고 발동")
                        elif "근접" in label:
                            st.warning(f"{label} — 근접 중")
                        else:
                            st.success(label)

                    if oh_ev.get("criteria"):
                        st.markdown(table_label)
                        st.dataframe(fmt_overheat_table(oh_ev),
                                     use_container_width=True, hide_index=True)

                st.caption("💡 예고 ↔ 지정 임계값은 **수치상 동일**합니다. "
                           "다만 이미 예고된 상태에서 재충족하면 '지정'으로, "
                           "예고 이력 없을 때 충족하면 '예고'로 발동됩니다.")

                st.markdown("---")
                st.markdown("#### 📈 최근 60일 종가 + 임계값")
                try:
                    chart = build_price_chart(df, warn_ev, oh_ev)
                    st.altair_chart(chart, use_container_width=True)
                    st.caption("점선 = 각 조건의 임계값.")
                except Exception as e:
                    st.info(f"차트 생성 생략: {e}")


# ───────────────────────────────────────────────────────────
# 탭 2: 관심종목 대시보드
# ───────────────────────────────────────────────────────────
with tab2:
    if "watchlist_text" not in st.session_state:
        content, sha = github_get_watchlist()
        st.session_state.watchlist_text = content or "삼성전자\nSK하이닉스\n009150"
        st.session_state.watchlist_sha = sha

    col_mode, col_reload = st.columns([3, 1])
    with col_mode:
        edit_mode = st.toggle("✏️ 편집 모드", value=False)
    with col_reload:
        if st.button("🔄 GitHub에서 새로고침", key="watchlist_reload"):
            content, sha = github_get_watchlist()
            if content:
                st.session_state.watchlist_text = content
                st.session_state.watchlist_sha = sha
                st.success("갱신됨")
                st.rerun()

    if edit_mode:
        new_text = st.text_area(
            "편집창", value=st.session_state.watchlist_text, height=250,
            label_visibility="collapsed",
        )
        col_save, col_cancel = st.columns([1, 1])
        with col_save:
            if st.button("💾 GitHub에 저장", type="primary",
                         use_container_width=True, key="watchlist_save"):
                if _github_config() is None:
                    st.error("GitHub 연동 미설정")
                else:
                    ok, msg = github_put_watchlist(
                        new_text, st.session_state.watchlist_sha)
                    if ok:
                        st.session_state.watchlist_text = new_text
                        _, new_sha = github_get_watchlist()
                        st.session_state.watchlist_sha = new_sha
                        st.success(msg)
                    else:
                        st.error(msg)
        with col_cancel:
            if st.button("↩️ 변경 취소", use_container_width=True,
                         key="watchlist_cancel"):
                st.rerun()
    else:
        lines = parse_watchlist(st.session_state.watchlist_text)
        if not lines:
            st.warning("관심종목 없음. 편집 모드를 켜세요.")
        else:
            st.caption(f"등록된 관심종목: **{len(lines)}개**")
            if st.button("🔄 전체 조회", type="primary", key="watchlist_scan"):
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
                rows = []
                progress = st.progress(0, text="조회 중...")
                for i, line in enumerate(lines):
                    progress.progress((i + 1) / len(lines),
                                       text=f"조회 중... {line}")
                    ticker = resolve_ticker(line, name_map)
                    if not ticker:
                        rows.append({"종목": line, "코드": "—", "종가": "—",
                                     "상태": "❓", "투경예고": "❓",
                                     "단기과열예고": "❓", "_정렬": 99})
                        continue
                    name = name_map[ticker]
                    try:
                        df = load_ohlcv(ticker, start, end)
                        if df.empty:
                            raise ValueError
                        idx = len(df) - 1
                        anomaly = detect_anomaly(df)
                        warn_ev = evaluate_warning(df, idx,
                                                    short_days=short_days,
                                                    short_mult=short_mult,
                                                    long_days=long_days,
                                                    long_mult=long_mult,
                                                    require_max15=require_max15)
                        oh_ev = evaluate_overheat(df, idx)
                    except Exception:
                        rows.append({"종목": name, "코드": ticker, "종가": "—",
                                     "상태": "❓", "투경예고": "❓",
                                     "단기과열예고": "❓", "_정렬": 99})
                        continue
                    curr = int(df["종가"].iloc[-1])
                    warn_label, warn_rank = status_label(warn_ev)
                    oh_label, oh_rank = status_label(oh_ev)
                    rank = min(warn_rank, oh_rank)
                    if anomaly["anomaly"] and rank >= 2:
                        rank = 1.5
                    rows.append({
                        "종목": name, "코드": ticker,
                        "종가": f"{curr:,}원",
                        "상태": "🚨 이상" if anomaly["anomaly"] else "정상",
                        "투경예고": warn_label,
                        "단기과열예고": oh_label,
                        "_정렬": rank,
                    })
                progress.empty()
                df_sum = pd.DataFrame(rows).sort_values("_정렬").drop(
                    columns=["_정렬"])
                alerts = (df_sum["투경예고"].str.contains("🔴").sum()
                          + df_sum["단기과열예고"].str.contains("🔴").sum())
                proximals = (df_sum["투경예고"].str.contains("🟡").sum()
                             + df_sum["단기과열예고"].str.contains("🟡").sum())
                anomalies = df_sum["상태"].str.contains("🚨").sum()
                msg_parts = []
                if alerts > 0:
                    msg_parts.append(f"🔴 예고해당 **{alerts}건**")
                if proximals > 0:
                    msg_parts.append(f"🟡 근접 **{proximals}건**")
                if anomalies > 0:
                    msg_parts.append(f"🚨 이상 **{anomalies}건**")
                if msg_parts:
                    if alerts > 0:
                        st.error(" / ".join(msg_parts))
                    else:
                        st.warning(" / ".join(msg_parts))
                else:
                    st.success("✅ 전체 정상")
                st.dataframe(df_sum, use_container_width=True, hide_index=True)
                st.caption(f"기준일: {end} | {len(lines)}개")


# ───────────────────────────────────────────────────────────
# 탭 3: 현재 지정종목
# ───────────────────────────────────────────────────────────
with tab3:
    st.caption("현재 한국거래소에 지정된 종목 리스트입니다. "
               "네이버 금융에서 실시간 조회하며, **지정이 해제되면 자동으로 리스트에서 빠집니다.** "
               "캐시는 10분이라 갱신이 느리면 아래 새로고침 버튼을 누르세요.")

    if st.button("🔄 지정종목 새로고침", key="designated_reload"):
        st.cache_data.clear()
        st.rerun()

    sub1, sub2, sub3 = st.tabs(["🚧 투자경고", "⚠️ 투자위험", "🔥 단기과열"])

    def render_designated(category: str, container):
        with container:
            with st.spinner(f"{category} 종목 조회 중..."):
                df, status = fetch_designated_stocks(category)

            if status != "ok":
                st.error(f"조회 실패: {status}")
                st.caption("네이버 금융 URL 구조가 바뀌었을 수 있습니다.")
                return

            if df.empty:
                st.info(f"현재 {category} 지정 종목이 없습니다.")
                return

            # 종목명 컬럼 찾기
            name_col = None
            for c in df.columns:
                if "종목" in str(c) or "기업" in str(c):
                    name_col = c
                    break

            df = df.copy()
            reverse_map = {v: k for k, v in name_map.items()}

            # ─── 자동 지정일 파싱 ───
            auto_dates = []
            auto_release = []
            auto_status = []

            if name_col:
                progress = st.progress(0, text="지정일 자동 조회 중...")
                for i, name in enumerate(df[name_col]):
                    progress.progress((i + 1) / len(df),
                                       text=f"지정일 조회 중... ({i+1}/{len(df)}) {name}")
                    code = reverse_map.get(str(name).strip())
                    if code:
                        date, st_msg = fetch_designation_date(code, category)
                    else:
                        date, st_msg = None, "no_code"
                    auto_dates.append(date if date else "—")
                    auto_release.append(
                        calculate_release_date(category, date) if date else "—"
                    )
                    auto_status.append(st_msg)
                progress.empty()

            df["지정일(자동)"] = auto_dates
            release_col_name = ("해제 예정일" if category == "단기과열"
                                else "해제 평가 시작일")
            df[release_col_name] = auto_release

            # 종목별 네이버 링크 컬럼 (클릭 시 공시 확인 가능)
            if name_col:
                def make_link(name):
                    code = reverse_map.get(str(name).strip())
                    if not code:
                        return ""
                    return f"https://finance.naver.com/item/news_notice.naver?code={code}"
                df["공시(네이버)"] = df[name_col].apply(make_link)

            # ─── 요약 ───
            ok_count = sum(1 for s in auto_status if s == "ok")
            total = len(df)
            st.markdown(f"**총 {total}개 종목 지정 중** "
                        f"(기준: {datetime.now():%Y-%m-%d %H:%M}) | "
                        f"자동 파싱 성공: {ok_count}/{total}")

            try:
                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "공시(네이버)": st.column_config.LinkColumn(
                            "공시(네이버)",
                            display_text="🔗 열기",
                            help="네이버 개별 종목 공시 페이지 (지정일 수동 확인용)",
                        ),
                    },
                )
            except Exception:
                st.dataframe(df, use_container_width=True, hide_index=True)

            # 안내
            if category == "단기과열":
                st.caption("📅 **해제 예정일**: 지정일 + 3거래일 (자동해제 확정). "
                           "지정종료일 종가가 지정일 전일보다 20%+ 상승 시 3거래일 연장 가능.")
            else:
                st.caption("📅 **해제 평가 시작일**: 지정일 + 10거래일 "
                           "(이 날 이후 주가 조건 충족 시 해제 가능. 자동해제 아님).")
            st.caption("※ 지정일은 네이버 공시 페이지에서 자동 파싱한 값입니다. "
                       "'—' 표시는 자동 조회 실패 — '🔗 열기'로 공시 확인 후 "
                       "아래 '수동 계산기'에서 직접 입력하세요.")

            # 관심종목 일괄 추가
            if name_col and _github_config():
                with st.expander("📥 이 리스트를 관심종목에 일괄 추가", expanded=False):
                    st.caption("현재 지정 종목들을 관심종목 리스트에 한번에 추가합니다. "
                               "이미 등록된 종목은 중복 추가되지 않습니다.")
                    if st.button(f"➕ {category} 전체 추가",
                                 key=f"add_all_{category}"):
                        names = df[name_col].dropna().astype(str).tolist()
                        result = add_to_watchlist(names)
                        st.success(result) if "✅" in result else st.info(result)

    render_designated("투자경고", sub1)
    render_designated("투자위험", sub2)
    render_designated("단기과열", sub3)

    # ─────────────────────────────────────────────────
    # 📊 지정 임계값 역산 분석
    # ─────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 📊 지정 임계값 역산 분석")
    st.caption("현재 지정된 종목들이 **지정 직전(T-1)에 어느 정도 상승해 있었는지**를 "
               "역산해서 경험적 임계값을 추정합니다. 규정 수치를 외우지 않아도 "
               "실제 지정 사례로부터 자동 교정된 기준을 얻을 수 있습니다.")

    st.warning(
        "⚠️ **해석 주의사항**\n\n"
        "- 여기서 나온 수치는 **경험적 참고치**이지 규정상 임계값이 아닙니다.\n"
        "- KRX 지정은 상승률 외에도 **회전율·변동성·이상매매 징후 등 불건전 요건**이 "
        "같이 걸려야 하므로, 상승률만으로 지정 여부가 결정되는 것은 아닙니다.\n"
        "- 특히 **단기과열은 상승률이 주 요건이 아니므로** 역산값이 다른 의미로 해석될 수 있습니다.\n"
        "- 샘플 수가 적으면(N<5) 통계적 신뢰도가 낮습니다."
    )

    if st.button("🧮 역산 분석 실행", type="primary", key="retro_analyze"):
        results_by_cat = {}
        for cat_name in ["투자경고", "투자위험", "단기과열"]:
            with st.spinner(f"{cat_name} 역산 분석 중..."):
                # 1. 지정 종목 리스트 재조회
                df_list, status = fetch_designated_stocks(cat_name)
                if status != "ok" or df_list.empty:
                    results_by_cat[cat_name] = None
                    continue

                # 2. 종목명 컬럼 찾기
                name_col = None
                for c in df_list.columns:
                    if "종목" in str(c) or "기업" in str(c):
                        name_col = c
                        break
                if not name_col:
                    results_by_cat[cat_name] = None
                    continue

                reverse_map = {v: k for k, v in name_map.items()}
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

                # 3. 각 종목별 역산
                per_stock = []
                for name in df_list[name_col].dropna().astype(str):
                    code = reverse_map.get(name.strip())
                    if not code:
                        continue
                    # 지정일 파싱
                    date, st_msg = fetch_designation_date(code, cat_name)
                    if not date or st_msg != "ok":
                        continue
                    # OHLCV 로드
                    try:
                        df_ohlcv = load_ohlcv(code, start, end)
                    except Exception:
                        continue
                    # 역산
                    analysis = analyze_retrospective_thresholds(df_ohlcv, date)
                    if analysis is None:
                        continue
                    per_stock.append({
                        "종목": name,
                        "코드": code,
                        "지정일": date,
                        "T-1 날짜": analysis["close_tm1_date"],
                        "T-1 종가": int(analysis["close_tm1"]),
                        "5일 상승률": analysis["ret_5d"],
                        "20일 상승률": analysis["ret_20d"],
                        "15일 최고가 달성": analysis["max15_reached"],
                    })

                results_by_cat[cat_name] = per_stock

        # 카테고리별 결과 표시
        for cat_name in ["투자경고", "투자위험", "단기과열"]:
            st.markdown(f"#### {cat_name}")
            samples = results_by_cat.get(cat_name)

            if samples is None or len(samples) == 0:
                st.info(f"역산할 {cat_name} 지정 종목이 없거나 지정일 파싱에 실패했습니다.")
                st.markdown("")
                continue

            # 통계 계산
            ret5_vals = [s["5일 상승률"] for s in samples if s["5일 상승률"] is not None]
            ret20_vals = [s["20일 상승률"] for s in samples if s["20일 상승률"] is not None]
            max15_count = sum(1 for s in samples if s["15일 최고가 달성"])

            def stats_str(vals, unit_label):
                if not vals:
                    return "—"
                s = pd.Series(vals)
                return (f"평균 **{(s.mean()-1)*100:+.1f}%** / "
                        f"중앙값 **{(s.median()-1)*100:+.1f}%** / "
                        f"범위 {(s.min()-1)*100:+.1f}% ~ {(s.max()-1)*100:+.1f}%")

            n = len(samples)
            if n < 5:
                st.caption(f"⚠️ 샘플 수 적음 (N={n}) — 참고용으로만 활용")
            else:
                st.caption(f"샘플 수: N={n}")

            # 통계 박스
            s5 = pd.Series(ret5_vals) if ret5_vals else None
            s20 = pd.Series(ret20_vals) if ret20_vals else None

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                st.markdown("**5일 상승률**")
                if s5 is not None:
                    st.markdown(f"중앙값: **{(s5.median()-1)*100:+.1f}%**")
                    st.caption(f"평균 {(s5.mean()-1)*100:+.1f}% / "
                               f"{(s5.min()-1)*100:+.1f}% ~ {(s5.max()-1)*100:+.1f}%")
            with col_b:
                st.markdown("**20일 상승률**")
                if s20 is not None:
                    st.markdown(f"중앙값: **{(s20.median()-1)*100:+.1f}%**")
                    st.caption(f"평균 {(s20.mean()-1)*100:+.1f}% / "
                               f"{(s20.min()-1)*100:+.1f}% ~ {(s20.max()-1)*100:+.1f}%")
            with col_c:
                st.markdown("**15일 최고가 달성**")
                st.markdown(f"**{max15_count}/{n}** "
                            f"({max15_count/n*100:.0f}%)")

            # 경험적 임계값 요약
            if s5 is not None and s20 is not None:
                st.info(
                    f"🎯 **경험적 {cat_name} 임계값 (중앙값 기준)**: "
                    f"5일 전 × {s5.median():.2f} · 20일 전 × {s20.median():.2f} · "
                    f"15일 최고가 달성률 {max15_count/n*100:.0f}%"
                )

            # 원시 데이터 테이블
            with st.expander(f"📋 {cat_name} 원시 데이터 ({n}개 종목)"):
                df_display = pd.DataFrame(samples).copy()
                df_display["5일 상승률"] = df_display["5일 상승률"].apply(
                    lambda v: f"{(v-1)*100:+.1f}%" if v else "—")
                df_display["20일 상승률"] = df_display["20일 상승률"].apply(
                    lambda v: f"{(v-1)*100:+.1f}%" if v else "—")
                df_display["T-1 종가"] = df_display["T-1 종가"].apply(
                    lambda v: f"{v:,}원")
                df_display["15일 최고가 달성"] = df_display["15일 최고가 달성"].apply(
                    lambda v: "✅" if v else "❌")
                st.dataframe(df_display, use_container_width=True, hide_index=True)

            st.markdown("")

    # ─────────────────────────────────────────────────
    # 수동 지정일 → 해제일 계산기
    # ─────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("### 🔧 수동 지정일 계산기")
    st.caption("위 표에 지정일이 '—'로 나오거나, 특정 종목의 해제일만 빠르게 계산하고 싶을 때 사용하세요.")

    mc1, mc2, mc3 = st.columns([2, 2, 3])
    with mc1:
        manual_category = st.selectbox(
            "카테고리",
            ["단기과열", "투자경고", "투자위험"],
            key="manual_calc_category",
        )
    with mc2:
        manual_date = st.date_input(
            "지정일",
            value=datetime.now().date(),
            key="manual_calc_date",
            help="해당 종목의 지정 공시 날짜 (YYYY-MM-DD)",
        )
    with mc3:
        manual_date_str = manual_date.strftime("%Y-%m-%d")
        release_str = calculate_release_date(manual_category, manual_date_str)
        label = ("해제 예정일 (확정)" if manual_category == "단기과열"
                 else "해제 평가 시작일 (조건부)")
        st.markdown("**계산 결과**")
        st.markdown(f"{label}: "
                    f"<span style='font-size:20px;color:#2980b9'>"
                    f"**{release_str}**</span>", unsafe_allow_html=True)

    if manual_category == "단기과열":
        st.caption("🟦 단기과열은 지정일 + 3거래일이 지나면 자동 해제됩니다 (연장 조건 제외).")
    else:
        st.caption(f"🟦 {manual_category}은 지정일 + 10거래일 이후부터 해제 평가를 받을 수 있습니다. "
                   "실제 해제는 주가 조건 충족 시점.")
    st.caption("※ 공휴일 미반영으로 실제 날짜와 ±1~2일 차이날 수 있습니다.")


# ───────────────────────────────────────────────────────────
# 탭 4: 진단 (실제 예고/지정 종목 입력 → 어느 기준에 걸리는지 전수 비교)
# ───────────────────────────────────────────────────────────
with tab4:
    st.markdown("### 🔬 종목 진단")
    st.caption("실제 예고/지정된 종목을 입력하면, 지정일 직전(T-1)의 "
               "여러 기간 상승률과 다양한 KRX 유형별 기준을 전수 비교해서 "
               "어느 기준이 실제로 작동했는지 역추적합니다.")

    dcol1, dcol2 = st.columns([2, 1])
    with dcol1:
        diag_input = st.text_input(
            "종목코드 6자리 또는 종목명",
            value="가온전선", key="diag_input",
            placeholder="예: 가온전선 또는 000500")
    with dcol2:
        diag_trigger_date = st.date_input(
            "예고/지정일 (T)",
            value=datetime.now().date(),
            key="diag_trigger_date",
            help="실제 공시된 예고/지정일. T-1 기준으로 역산합니다.",
        )

    if diag_input:
        d_ticker = resolve_ticker(diag_input, name_map)
        if not d_ticker:
            st.error("종목을 찾지 못했습니다.")
            st.stop()

        d_name = name_map[d_ticker]

        # OHLCV 로드 (60일 상승률도 보므로 충분한 과거 필요)
        d_end = (diag_trigger_date + timedelta(days=10)).strftime("%Y-%m-%d")
        d_start = (diag_trigger_date - timedelta(days=300)).strftime("%Y-%m-%d")
        try:
            d_df = load_ohlcv(d_ticker, d_start, d_end)
        except Exception as e:
            st.error(f"OHLCV 로딩 실패: {e}")
            st.stop()

        if d_df.empty:
            st.error("OHLCV 데이터가 비어 있습니다.")
            st.stop()

        # T-1 찾기
        d_dt = pd.Timestamp(diag_trigger_date)
        prior = d_df.index[d_df.index < d_dt]
        if len(prior) == 0:
            st.error("예고일 이전 거래 데이터가 없습니다.")
            st.stop()

        tm1 = prior[-1]
        idx = d_df.index.get_loc(tm1)
        if isinstance(idx, slice):
            idx = idx.stop - 1

        if idx < 60:
            st.warning(f"예고일 이전 데이터가 {idx}일뿐입니다. 장기 상승률 일부 계산 불가.")

        curr = int(d_df["종가"].iloc[idx])

        # ─── 헤더 ───
        st.markdown("---")
        st.markdown(f"### 📍 {d_name} ({d_ticker})")
        st.markdown(f"**예고/지정일(T):** {diag_trigger_date.strftime('%Y-%m-%d')} | "
                    f"**T-1 (역산 기준일):** {tm1.strftime('%Y-%m-%d')} | "
                    f"**T-1 종가:** {curr:,}원")

        # ─── 여러 기간 상승률 ───
        st.markdown("#### 📊 기간별 상승률")
        periods = [3, 5, 10, 15, 20, 40, 60]
        period_rows = []
        for p in periods:
            if idx < p:
                period_rows.append({
                    "기간": f"{p}일", "과거 종가": "—", "현재가": f"{curr:,}원",
                    "상승률": "데이터 부족",
                })
                continue
            past = int(d_df["종가"].iloc[idx - p])
            ratio = curr / past if past > 0 else 0
            pct = (ratio - 1) * 100
            period_rows.append({
                "기간": f"{p}일 전",
                "과거 종가": f"{past:,}원",
                "현재가": f"{curr:,}원",
                "상승률": f"{pct:+.1f}% (×{ratio:.2f})",
            })
        st.dataframe(pd.DataFrame(period_rows),
                     use_container_width=True, hide_index=True)

        # 15일·20일 최고가 도달 여부
        max15 = int(d_df["종가"].iloc[max(0, idx - 14): idx + 1].max())
        max20 = int(d_df["종가"].iloc[max(0, idx - 19): idx + 1].max())
        st.caption(
            f"📈 15일 최고가: **{max15:,}원** "
            f"{'✅ 달성' if curr >= max15 else '❌ 미달'} | "
            f"20일 최고가: **{max20:,}원** "
            f"{'✅ 달성' if curr >= max20 else '❌ 미달'}"
        )

        # ─── KRX 유형별 기준 전수 비교 ───
        st.markdown("---")
        st.markdown("#### 🎯 KRX 유형별 기준 전수 비교")
        st.caption("다양한 유형의 상승률 기준을 동시에 적용하여, "
                   "이 종목이 어느 유형의 기준에 해당하는지 확인합니다.")

        # 공식 또는 추정되는 유형별 기준
        # (시장감시규정 시행세칙 제3조의3 기반 + 해제요건으로 역추정)
        krx_types = [
            # (유형명, 기간1, 배수1, 기간2, 배수2, 15일최고가_필수, 주석)
            ("단기상승&불건전 (해제요건 역추정)", 5, 1.45, 15, 1.75, True,
             "해제요건: 5일 +45%, 15일 +75%"),
            ("현재 앱 기본값", 5, 1.60, 20, 2.00, True,
             "앱에서 쓰던 수치"),
            ("초단기급등 (추정)", 3, 1.30, 10, 1.50, True,
             "공개 자료 제한, 추정치"),
            ("단기급등 (추정)", 5, 1.45, 15, 1.75, True,
             "단기상승과 유사"),
            ("중장기급등 (해제요건 역추정, 위험종목)", 5, 1.60, 15, 2.00, True,
             "투자위험 해제요건: 5일 +60%, 15일 +100%"),
            ("장기급등 (추정)", 20, 1.60, 60, 2.00, False,
             "장기 관점, 15일 최고가 조건 완화"),
        ]

        comp_rows = []
        for (tname, p1, m1, p2, m2, need_max15, note) in krx_types:
            if idx < p1 or idx < p2:
                comp_rows.append({
                    "유형": tname,
                    "조건1": f"{p1}일×{m1:.2f}",
                    "조건1 결과": "데이터 부족",
                    "조건2": f"{p2}일×{m2:.2f}",
                    "조건2 결과": "데이터 부족",
                    "15일최고": "—",
                    "종합": "—",
                    "비고": note,
                })
                continue

            past1 = int(d_df["종가"].iloc[idx - p1])
            past2 = int(d_df["종가"].iloc[idx - p2])
            th1 = past1 * m1
            th2 = past2 * m2
            c1 = curr >= th1
            c2 = curr >= th2
            c3 = curr >= max15 if need_max15 else True

            comp_rows.append({
                "유형": tname,
                "조건1": f"{p1}일×{m1:.2f}",
                "조건1 결과": f"{'✅' if c1 else '❌'} 임계 {int(th1):,} / 현재 {curr:,}",
                "조건2": f"{p2}일×{m2:.2f}",
                "조건2 결과": f"{'✅' if c2 else '❌'} 임계 {int(th2):,} / 현재 {curr:,}",
                "15일최고": ("✅" if curr >= max15 else "❌") if need_max15 else "불요",
                "종합": "🔴 충족" if (c1 and c2 and c3) else "❌ 미충족",
                "비고": note,
            })

        st.dataframe(pd.DataFrame(comp_rows),
                     use_container_width=True, hide_index=True)

        st.info(
            "💡 **해석 가이드**\n\n"
            "- 실제 예고/지정된 종목인데 위 표에서 🔴 충족이 하나도 없다면 → "
            "상승률만으로 지정된 게 아니라 **CB/BW 공시 연계, 회전율, 이상매매 징후** 등 "
            "다른 요건이 주된 사유일 가능성.\n"
            "- 🔴 충족이 특정 유형에서만 나오면 → **그 유형이 실제 지정 사유**로 추정됨.\n"
            "- 기준 수치가 공개 자료로 완전히 공개되지 않아 일부는 **추정치**입니다."
        )

        # ─── 거래량·변동성 진단 ───
        st.markdown("---")
        st.markdown("#### 📦 거래량·변동성 진단 (불건전 요건 힌트)")

        if idx >= 40:
            # 최근 2일 vs 40일 평균 거래량
            avg2_vol = d_df["거래량"].iloc[idx - 1: idx + 1].mean()
            avg40_vol = d_df["거래량"].iloc[idx - 39: idx + 1].mean()
            vol_ratio = avg2_vol / avg40_vol if avg40_vol > 0 else 0

            # 변동성
            prev_close = d_df["종가"].shift(1)
            daily_vola = (d_df["고가"] - d_df["저가"]) / prev_close
            avg2_vola = daily_vola.iloc[idx - 1: idx + 1].mean()
            avg40_vola = daily_vola.iloc[idx - 39: idx + 1].mean()
            vola_ratio = avg2_vola / avg40_vola if avg40_vola > 0 else 0

            # 최근 5일 거래량 vs 40일 평균
            avg5_vol = d_df["거래량"].iloc[idx - 4: idx + 1].mean()
            vol5_ratio = avg5_vol / avg40_vol if avg40_vol > 0 else 0

            vd_rows = [
                {
                    "지표": "최근 2일 거래량 / 40일 평균",
                    "값": f"×{vol_ratio:.2f}",
                    "기준(단기과열)": "×6.0 이상",
                    "해당": "✅" if vol_ratio >= 6.0 else "❌",
                },
                {
                    "지표": "최근 5일 거래량 / 40일 평균",
                    "값": f"×{vol5_ratio:.2f}",
                    "기준(참고)": "×3~5 수준이면 거래 급증",
                    "해당": "🔥" if vol5_ratio >= 3.0 else "—",
                },
                {
                    "지표": "최근 2일 변동성 / 40일 평균",
                    "값": f"×{vola_ratio:.2f}",
                    "기준(단기과열)": "×1.5 이상",
                    "해당": "✅" if vola_ratio >= 1.5 else "❌",
                },
            ]
            st.dataframe(pd.DataFrame(vd_rows),
                         use_container_width=True, hide_index=True)
        else:
            st.info("데이터 부족(40거래일 미만)으로 거래량·변동성 진단 불가")

        # ─── 원시 OHLCV 다운로드 ───
        with st.expander("📋 T-1 기준 최근 30일 원시 OHLCV 데이터"):
            window_start = max(0, idx - 29)
            display_df = d_df.iloc[window_start: idx + 1].copy()
            display_df["종가"] = display_df["종가"].apply(lambda v: f"{int(v):,}")
            display_df["거래량"] = display_df["거래량"].apply(lambda v: f"{int(v):,}")
            display_df.index = display_df.index.strftime("%Y-%m-%d")
            st.dataframe(display_df[["종가", "거래량"]],
                         use_container_width=True)


st.markdown("---")
st.caption("📌 공개 주가 데이터 기반 자체 계산입니다. "
           "현재 지정종목 리스트는 네이버 금융에서 실시간 반영됩니다. "
           "최종 판단은 한국거래소 공식 공시로 확인하세요.")
