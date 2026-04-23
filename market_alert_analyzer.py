"""
투경예고 / 단기과열예고 임계값 조회기 + 관심종목 대시보드 + 현재 지정종목 + CB/BW 조회
────────────────────────────────────────────────────────────────────
 [1] 개별 종목 조회 (차트 포함)
 [2] 관심종목 대시보드 (GitHub 저장 + 근접 경고)
 [3] 현재 지정종목 (네이버 금융 실시간 반영, 지정해제 시 자동 제외)
 [4] CB/BW 조회 (DART OpenAPI, 하이브리드: 발행=실시간 / 잔액=분기)
"""
import streamlit as st
import pandas as pd
import requests
import base64
import altair as alt
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from io import StringIO

try:
    import OpenDartReader as _ODR
    _HAS_DART = True
    _DART_IMPORT_ERR = ""
except Exception as _e:
    _ODR = None
    _HAS_DART = False
    _DART_IMPORT_ERR = f"{type(_e).__name__}: {_e}"

st.set_page_config(page_title="예고 임계값 조회기", layout="wide")
st.title("🔍 투경예고 / 단기과열예고 조회")

PROXIMITY_PCT = 0.95


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
        # name_map이 비어있으면 검증 불가 → 그대로 통과
        return s if (not name_map or s in name_map) else None
    if not name_map:
        return None
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

    for fmt in ["%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"]:
        try:
            dt = pd.to_datetime(s, format=fmt)
            break
        except Exception:
            continue

    # 년도 없는 MM.DD 같은 경우 — 현재 연도 가정
    if dt is None:
        for fmt in ["%m.%d", "%m-%d", "%m/%d"]:
            try:
                dt = pd.to_datetime(f"{current_year}.{s.replace('-', '.').replace('/', '.')}",
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
# DART OpenAPI (CB/BW 조회)
# ─────────────────────────────────────────────────────────────
@st.cache_resource
def get_dart_client():
    """DART API 클라이언트. 반환: (client, error_msg)"""
    if not _HAS_DART:
        return None, f"OpenDartReader 로딩 실패: {_DART_IMPORT_ERR}"
    try:
        api_key = st.secrets["DART_API_KEY"]
    except Exception as e:
        return None, f"secrets에서 DART_API_KEY 읽기 실패: {type(e).__name__}: {e}"
    if not api_key or not str(api_key).strip():
        return None, "DART_API_KEY가 빈 값입니다"
    try:
        return _ODR(str(api_key).strip()), ""
    except Exception as e:
        return None, f"OpenDartReader 초기화 실패: {type(e).__name__}: {e}"


@st.cache_data(ttl=3600)
def fetch_cb_bw_disclosures(ticker: str, years_back: int = 5) -> pd.DataFrame:
    """
    최근 N년간 해당 종목의 CB/BW 발행결정 공시 목록 조회.
    kind='B' (주요사항보고) 중에서 'CB 발행결정' / 'BW 발행결정' 필터링.
    """
    dart, _ = get_dart_client()
    if dart is None:
        return pd.DataFrame()

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=365 * years_back)).strftime("%Y-%m-%d")

    try:
        df = dart.list(ticker, start=start, end=end, kind="B", final=True)
    except Exception:
        return pd.DataFrame()

    if df is None or len(df) == 0 or "report_nm" not in df.columns:
        return pd.DataFrame()

    # CB/BW 발행결정 공시만 추출
    mask = df["report_nm"].str.contains(
        "전환사채권\\s*발행결정|신주인수권부사채권\\s*발행결정",
        na=False, regex=True
    )
    result = df[mask].copy().reset_index(drop=True)
    if len(result) == 0:
        return result

    # 사채 종류 태그
    result["사채종류"] = result["report_nm"].apply(
        lambda s: "CB" if "전환사채" in str(s) else ("BW" if "신주인수권" in str(s) else "기타")
    )
    # DART 원문 URL
    result["원문URL"] = result["rcept_no"].apply(
        lambda rn: f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rn}"
    )
    return result


@st.cache_data(ttl=3600)
def fetch_debt_securities_latest(ticker: str) -> tuple[pd.DataFrame, str]:
    """
    가장 최근 정기보고서(분기/반기/사업)의 '채무증권 발행실적' 조회.
    반환: (DataFrame, 보고서설명). 실패 시 (빈 DF, 에러메시지)
    """
    dart, _ = get_dart_client()
    if dart is None:
        return pd.DataFrame(), "DART API 미설정"

    current_year = datetime.now().year
    # 최신 → 과거 순으로 시도. reprt_code: 사업(11011), 반기(11012), 1분기(11013), 3분기(11014)
    # 최근 것부터: 이번 연도 3분기 → 반기 → 1분기 → 전년도 사업 → 전년도 3분기 ...
    attempts = []
    for year in [current_year, current_year - 1, current_year - 2]:
        for code, label in [("11014", "3분기"), ("11012", "반기"),
                             ("11013", "1분기"), ("11011", "사업")]:
            attempts.append((year, code, label))

    last_err = ""
    for year, code, label in attempts:
        try:
            df = dart.report(ticker, "채무증권발행", year, reprt_code=code)
            if df is not None and len(df) > 0:
                return df.copy(), f"{year}년 {label}보고서"
        except Exception as e:
            last_err = str(e)[:80]
            continue

    return pd.DataFrame(), f"최근 3년 정기보고서에 채무증권 발행실적 없음 ({last_err})"


def filter_cb_bw_outstanding(df_debt: pd.DataFrame) -> pd.DataFrame:
    """
    채무증권 발행실적 DF에서 CB/BW 중 '미상환 잔액이 있는 것'만 필터.
    DART 응답 컬럼명이 버전에 따라 조금씩 다를 수 있어 방어적으로 탐색.
    """
    if df_debt is None or len(df_debt) == 0:
        return pd.DataFrame()

    # 증권종류명 컬럼 찾기
    kind_col = None
    for c in df_debt.columns:
        if "isu_nm" in str(c).lower() or "종류" in str(c) or "scrits_knd" in str(c).lower():
            kind_col = c
            break
    if kind_col is None:
        # fallback: 모든 행 포함
        result = df_debt.copy()
    else:
        mask = df_debt[kind_col].astype(str).str.contains(
            "전환사채|신주인수권부사채|CB|BW|전환|신주인수권",
            na=False, regex=True
        )
        result = df_debt[mask].copy()

    # 미상환 잔액 컬럼 찾기
    remain_col = None
    for c in result.columns:
        cl = str(c).lower()
        if "remndr" in cl or "미상환" in str(c) or "잔액" in str(c):
            remain_col = c
            break

    if remain_col is not None:
        def _to_num(x):
            try:
                s = str(x).replace(",", "").replace("원", "").strip()
                if s in ("", "-", "—", "nan"):
                    return 0
                return float(s)
            except Exception:
                return 0
        result["_잔액숫자"] = result[remain_col].apply(_to_num)
        result = result[result["_잔액숫자"] > 0].copy()
        result = result.drop(columns=["_잔액숫자"])

    return result.reset_index(drop=True)



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


def evaluate_warning(df, idx):
    curr = int(df["종가"].iloc[idx])
    if idx < 20:
        return {"status": None, "reason": "데이터 부족"}
    p5 = int(df["종가"].iloc[idx - 5])
    p20 = int(df["종가"].iloc[idx - 20])
    max15 = int(df["종가"].iloc[idx - 14: idx + 1].max())
    th1, th2, th3 = int(p5 * 1.6), int(p20 * 2.0), max15
    c1, c2, c3 = curr >= th1, curr >= th2, curr >= th3
    ratios = [curr / th1 if th1 else 0, curr / th2 if th2 else 0,
              curr / th3 if th3 else 0]
    return {
        "status": all([c1, c2, c3]), "current": curr,
        "thresholds": {"5일x1.6": th1, "20일x2.0": th2, "15일최고": th3},
        "criteria": [
            ("① 5일 전 × 1.6", th1, c1, ratios[0]),
            ("② 20일 전 × 2.0", th2, c2, ratios[1]),
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
# 종목명 매핑 (실패해도 앱 전체 정지시키지 않음)
# ─────────────────────────────────────────────────────────────
try:
    name_map = get_ticker_name_map()
    _name_map_err = ""
except Exception as e:
    name_map = {}
    _name_map_err = str(e)[:200]
    st.warning(
        f"⚠️ KRX 종목 리스트 로딩 실패 — 종목명 검색은 불가하지만 "
        f"**6자리 종목코드 직접 입력은 가능**합니다. (원인: {_name_map_err})"
    )


# ═══════════════════════════════════════════════════════════
# 탭 3개
# ═══════════════════════════════════════════════════════════
tab1, tab2, tab3, tab4 = st.tabs([
    "🎯 개별 종목 조회",
    "📋 관심종목 대시보드",
    "📌 현재 지정종목",
    "🏦 CB/BW 조회",
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
            name = name_map.get(ticker, ticker)
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

                warn_ev = evaluate_warning(df, base_idx)
                oh_ev = evaluate_overheat(df, base_idx)
                curr_p = int(df["종가"].iloc[base_idx])

                st.markdown(f"### 📍 {name} ({ticker})")
                st.markdown(f"**{selected_date} 종가:** "
                            f"<span style='font-size:22px;color:#d35400'>"
                            f"{curr_p:,}원</span>", unsafe_allow_html=True)
                st.markdown("---")

                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("#### ⚠️ 투자경고 예고")
                    if warn_ev["status"] is None:
                        st.info(warn_ev["reason"])
                    else:
                        label, _ = status_label(warn_ev)
                        if warn_ev["status"]:
                            st.error(f"{label} — 3대 요건 모두 충족")
                        elif "근접" in label:
                            st.warning(f"{label} — 근접 중")
                        else:
                            st.success(label)
                    if warn_ev.get("criteria"):
                        st.dataframe(fmt_warning_table(warn_ev),
                                     use_container_width=True, hide_index=True)

                with c2:
                    st.markdown("#### 🔥 단기과열 예고")
                    if oh_ev["status"] is None:
                        st.info(oh_ev["reason"])
                    else:
                        label, _ = status_label(oh_ev)
                        if oh_ev["status"]:
                            st.error(f"{label} — 3대 요건 모두 충족")
                        elif "근접" in label:
                            st.warning(f"{label} — 근접 중")
                        else:
                            st.success(label)
                    if oh_ev.get("criteria"):
                        st.dataframe(fmt_overheat_table(oh_ev),
                                     use_container_width=True, hide_index=True)

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
                    name = name_map.get(ticker, ticker)
                    try:
                        df = load_ohlcv(ticker, start, end)
                        if df.empty:
                            raise ValueError
                        idx = len(df) - 1
                        anomaly = detect_anomaly(df)
                        warn_ev = evaluate_warning(df, idx)
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
                st.caption("네이버 금융 URL 구조가 바뀌었을 수 있습니다. "
                           "상단 ▲ 아이콘의 'Manage app → View logs'에서 상세 로그 확인 가능.")
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

            # 지정일 컬럼 찾기 → 해제 판단일 계산 후 맨 뒤에 추가
            designated_col = None
            for c in df.columns:
                if "지정일" in str(c):
                    designated_col = c
                    break

            # 컬럼 제목
            release_col_name = ("해제 예정일" if category == "단기과열"
                                else "해제 평가 시작일")

            if designated_col:
                df = df.copy()
                df[release_col_name] = df[designated_col].apply(
                    lambda d: calculate_release_date(category, str(d))
                )
            else:
                df = df.copy()
                df[release_col_name] = "—"

            st.markdown(f"**총 {len(df)}개 종목 지정 중** "
                        f"(기준: {datetime.now():%Y-%m-%d %H:%M} 조회)")
            st.dataframe(df, use_container_width=True, hide_index=True)

            # 해제 판단일 설명
            if category == "단기과열":
                st.caption("📅 **해제 예정일**: 지정일 + 3거래일 (자동해제 확정). "
                           "단, 지정종료일 종가가 지정일 전일보다 20%+ 상승 시 3거래일 연장될 수 있음.")
            else:
                st.caption("📅 **해제 평가 시작일**: 지정일 + 10거래일 "
                           "(이 날 이후 주가 조건 충족 시 해제 가능. 자동해제 아님).")
            st.caption("※ 공휴일은 반영되지 않아 실제 날짜와 ±1~2일 차이날 수 있습니다.")

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


# ───────────────────────────────────────────────────────────
# 탭 4: CB/BW 조회 (DART OpenAPI)
# ───────────────────────────────────────────────────────────
with tab4:
    st.caption("DART 전자공시 기반 CB(전환사채) / BW(신주인수권부사채) 조회. "
               "**발행 공시는 실시간**, **미상환 잔액은 가장 최근 정기보고서 기준(분기 단위)**입니다.")

    # DART 클라이언트 상태 체크
    dart_client, dart_err = get_dart_client()
    if not _HAS_DART:
        st.error("❌ OpenDartReader 라이브러리 로딩 실패")
        st.code(_DART_IMPORT_ERR or "(알 수 없는 원인)")
        st.caption("`requirements.txt`에 `OpenDartReader>=0.2.1`이 있는지, "
                   "그리고 Python 버전 호환성을 확인하세요.")
        st.stop()
    if dart_client is None:
        st.error("❌ DART 클라이언트 초기화 실패")
        st.code(dart_err or "(알 수 없는 원인)")
        st.caption("Streamlit Secrets에 `DART_API_KEY = \"...\"` 형태로 올바르게 "
                   "추가되어 있는지 확인하세요. "
                   "키 발급: https://opendart.fss.or.kr/")
        st.stop()

    # 종목 입력
    col_in, col_yrs = st.columns([2, 1])
    with col_in:
        cb_input = st.text_input("종목코드 6자리 또는 종목명",
                                  value="", key="cb_input",
                                  placeholder="예: 삼성전자 또는 005930")
    with col_yrs:
        years_back = st.selectbox("발행 공시 조회 기간", [3, 5, 7, 10],
                                   index=1, key="cb_years")

    if cb_input.strip():
        ticker = resolve_ticker(cb_input, name_map)
        if not ticker:
            st.error("종목을 찾지 못했습니다.")
        else:
            name = name_map.get(ticker, ticker)
            st.markdown(f"### 📍 {name} ({ticker})")

            # ─── 섹션 1: 발행 공시 이력 ───
            with st.spinner("DART 발행공시 조회 중..."):
                df_disc = fetch_cb_bw_disclosures(ticker, years_back)

            st.markdown(f"#### 📋 최근 {years_back}년 CB/BW 발행 공시")
            if df_disc.empty:
                st.info(f"최근 {years_back}년 내 CB/BW 발행 공시 없음.")
            else:
                # 주요 컬럼만 표시
                show_cols = []
                for c in ["rcept_dt", "report_nm", "사채종류", "rcept_no"]:
                    if c in df_disc.columns:
                        show_cols.append(c)
                rename_map = {
                    "rcept_dt": "접수일",
                    "report_nm": "공시명",
                    "rcept_no": "접수번호",
                }
                df_show = df_disc[show_cols].rename(columns=rename_map)
                st.caption(f"총 **{len(df_disc)}건** 발행 공시")
                st.dataframe(df_show, use_container_width=True, hide_index=True)

                # DART 원문 링크
                with st.expander("🔗 DART 원문 바로가기", expanded=False):
                    for _, row in df_disc.iterrows():
                        rd = row.get("rcept_dt", "—")
                        rn = row.get("report_nm", "—")
                        url = row.get("원문URL", "#")
                        st.markdown(f"- [{rd}] {rn} → [DART 원문]({url})")

            st.markdown("---")

            # ─── 섹션 2: 미상환 잔액 (최근 정기보고서) ───
            st.markdown("#### 💰 현재 미상환 CB/BW 잔액")
            with st.spinner("최근 정기보고서 조회 중..."):
                df_debt, report_label = fetch_debt_securities_latest(ticker)

            if df_debt.empty:
                st.info(f"ℹ️ {report_label}")
                st.caption("정기보고서에 '채무증권 발행실적' 항목이 없거나, "
                           "해당 종목이 CB/BW를 발행한 적 없을 수 있습니다.")
            else:
                df_outstanding = filter_cb_bw_outstanding(df_debt)

                if df_outstanding.empty:
                    st.success(f"✅ **미상환 CB/BW 없음** (기준: {report_label})")
                    with st.expander("전체 채무증권 발행실적 보기 (참고)", expanded=False):
                        st.dataframe(df_debt, use_container_width=True, hide_index=True)
                else:
                    st.warning(f"⚠️ **미상환 CB/BW {len(df_outstanding)}건** "
                               f"(기준: {report_label})")
                    st.dataframe(df_outstanding, use_container_width=True,
                                 hide_index=True)

                    st.caption(
                        "💡 **잠재 출회주식수 계산법**: 미상환잔액 ÷ 현재 전환(행사)가액. "
                        "정확한 계산은 표의 '미상환잔액'과 '전환가액'을 확인하세요. "
                        "리픽싱은 이번 MVP에 미반영이지만, 정기보고서에는 기준일 시점의 "
                        "조정된 전환가액이 이미 반영되어 있어 분기 말 기준으로는 정확합니다."
                    )

            # 주의사항
            st.markdown("---")
            with st.expander("ℹ️ 데이터 정확성 주의", expanded=False):
                st.markdown(
                    "- **발행 공시**: DART 주요사항보고서 기준, 실시간 반영\n"
                    "- **미상환 잔액**: 최근 정기보고서(사업/반기/분기) 기준 — "
                    "최대 약 3개월까지 낡은 숫자일 수 있음\n"
                    "- **전환가액**: 정기보고서 기준일까지 반영된 리픽싱 결과. "
                    "이후 발생한 리픽싱은 이번 MVP에서 추적하지 않음\n"
                    "- **투자 판단 시** 반드시 DART 원문 공시와 최신 증권신고서·"
                    "사업보고서를 교차 확인하세요."
                )


st.markdown("---")
st.caption("📌 공개 데이터 기반 자체 계산입니다. "
           "주가·예고는 FinanceDataReader, 지정종목은 네이버 금융, "
           "CB/BW는 DART 전자공시 기반입니다. "
           "최종 판단은 한국거래소 및 DART 공식 공시로 확인하세요.")
