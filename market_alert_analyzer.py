"""
시장경보(예고/지정) 실시간 리스트 & 요건 분석기 (FDR 버전)
────────────────────────────────────────────────────────
실행 (로컬):    streamlit run market_alert_analyzer.py
배포 (Cloud):  GitHub 저장소에 올리면 자동 배포
"""
import streamlit as st
import pandas as pd
import requests
import FinanceDataReader as fdr
from datetime import datetime, timedelta

st.set_page_config(page_title="시장경보 통합 분석기", layout="wide")
st.title("🚨 시장경보(예고/지정) 실시간 리스트 & 요건 분석")


# ─────────────────────────────────────────────────────────────
# 공용 유틸
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_ticker_name_map() -> dict:
    """KRX 상장종목 전체 — 티커→종목명 매핑. FDR 한 번 호출로 끝."""
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
# KRX 시장경보 리스트 (best-effort)
# ─────────────────────────────────────────────────────────────
KRX_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/",
}

BLD_CANDIDATES_SHORT_OVERHEAT = [
    "dbms/MDC/STAT/standard/MDCSTAT14001",
    "dbms/MDC/STAT/standard/MDCSTAT13401",
    "dbms/MDC/STAT/standard/MDCSTAT08401",
]
BLD_CANDIDATES_MARKET_ALERT = [
    "dbms/MDC/STAT/standard/MDCSTAT14002",
    "dbms/MDC/STAT/standard/MDCSTAT13301",
    "dbms/MDC/STAT/standard/MDCSTAT08501",
]


@st.cache_data(ttl=600)
def try_krx_alerts() -> tuple[pd.DataFrame, str]:
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    frames = []
    for label, candidates in [("단기과열", BLD_CANDIDATES_SHORT_OVERHEAT),
                              ("시장경보", BLD_CANDIDATES_MARKET_ALERT)]:
        got = False
        for bld in candidates:
            if got:
                break
            for dd in (today, yesterday):
                try:
                    r = requests.post(
                        KRX_URL, headers=KRX_HEADERS, timeout=8,
                        data={"bld": bld, "trdDd": dd, "share": "1",
                              "money": "1", "csvxls_isNo": "false"},
                    )
                    if r.status_code != 200:
                        continue
                    js = r.json()
                    for key in ("OutBlock_1", "output", "block1"):
                        if key in js and js[key]:
                            df = pd.DataFrame(js[key])
                            df["구분"] = label
                            frames.append(df)
                            got = True
                            break
                    if got:
                        break
                except Exception:
                    continue

    if not frames:
        return pd.DataFrame(), "KRX 엔드포인트 응답 실패 — bld 경로가 변경된 것으로 보입니다."
    return pd.concat(frames, ignore_index=True), "ok"


# ─────────────────────────────────────────────────────────────
# 1. 시장경보 리스트
# ─────────────────────────────────────────────────────────────
st.subheader("📢 현재 시장경보 발령 종목 현황")
with st.expander("이 섹션이 비어있거나 오류가 나면 펼쳐보세요", expanded=False):
    st.markdown("""
KRX 정보데이터시스템은 공식 API가 없어 내부 경로(`bld`)를 호출합니다.
거래소가 경로를 바꾸면 이 섹션만 실패하고, 아래 개별 종목 분석은 정상 동작합니다.

**최신 `bld` 값 찾는 방법:**
1. `data.krx.co.kr` 접속 → 기본통계 → 주식 → 세부안내 → 단기과열종목 / 시장경보종목
2. 크롬에서 F12 → Network 탭 → 조회 버튼 클릭
3. `getJsonData.cmd` 요청의 Payload에서 `bld` 값을 복사해 코드 상단 `BLD_CANDIDATES_*` 리스트 맨 앞에 추가
""")

try:
    alert_df, status = try_krx_alerts()
    if status == "ok" and not alert_df.empty:
        st.dataframe(alert_df, use_container_width=True, hide_index=True)
        st.caption(f"기준일: {datetime.now().strftime('%Y-%m-%d')} | 출처: KRX")
    else:
        st.info(f"리스트 조회 실패: {status}")
except Exception as e:
    st.warning(f"리스트 조회 중 예외: {e}")

st.divider()

# ─────────────────────────────────────────────────────────────
# 2. 특정 종목 지정 요건 정밀 분석
# ─────────────────────────────────────────────────────────────
col_input, col_date = st.columns([2, 1])
with col_input:
    user_input = st.text_input("📝 분석할 종목코드 6자리 또는 종목명", "010820")

if user_input:
    try:
        with st.spinner("종목 리스트 로딩 중..."):
            name_map = get_ticker_name_map()
    except Exception as e:
        st.error(f"종목 리스트 로딩 실패: {e}")
        st.stop()

    ticker = resolve_ticker(user_input, name_map)
    if not ticker:
        st.error("종목을 찾지 못했습니다. 6자리 코드 또는 정확한 종목명을 입력하세요.")
        st.stop()

    name = name_map[ticker]

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

    try:
        df = load_ohlcv(ticker, start, end)
    except Exception as e:
        st.error(f"OHLCV 로딩 실패: {e}")
        st.stop()

    if df.empty:
        st.error("OHLCV 데이터가 비어 있습니다.")
        st.stop()

    with col_date:
        date_list = df.index.strftime("%Y-%m-%d").tolist()[::-1]
        selected_date = st.selectbox("📅 판단 기준일(T) 선택", date_list, index=0)

    base_idx = df.index.get_loc(pd.Timestamp(selected_date))
    if isinstance(base_idx, slice):
        base_idx = base_idx.stop - 1

    curr_p = int(df["종가"].iloc[base_idx])

    st.subheader(f"📍 {name} ({ticker}) 지정 요건 검토")
    st.markdown(f"**기준일 종가: {curr_p:,}원**")

    tab1, tab2 = st.tabs(["⚠️ 투자경고 지정 요건", "🔥 단기과열 지정 요건"])

    with tab1:
        st.write("### [투자경고 표준 3대 요건]")
        if base_idx < 20:
            st.warning(f"기준일 이전 영업일이 {base_idx}일뿐이라 20일 비교가 불가합니다.")
        else:
            t_5_p = int(df["종가"].iloc[base_idx - 5])
            t_20_p = int(df["종가"].iloc[base_idx - 20])
            max_15_p = int(df["종가"].iloc[base_idx - 14: base_idx + 1].max())

            c1 = curr_p >= int(t_5_p * 1.6)
            c2 = curr_p >= int(t_20_p * 2.0)
            c3 = curr_p >= max_15_p

            st.write(f"{'✅' if c1 else '❌'} 1. **5일 전({t_5_p:,}원)** 대비 60% 상승"
                     f" (기준가: {int(t_5_p*1.6):,}원)")
            st.write(f"{'✅' if c2 else '❌'} 2. **20일 전({t_20_p:,}원)** 대비 100% 상승"
                     f" (기준가: {int(t_20_p*2.0):,}원)")
            st.write(f"{'✅' if c3 else '❌'} 3. 당일 종가가 최근 **15거래일 중 최고가**"
                     f" (최고: {max_15_p:,}원)")

            if c1 and c2 and c3:
                st.error("🚨 투자경고 지정 가능성이 매우 높습니다 (모든 요건 충족).")
            else:
                st.warning("💡 일부 요건 미달 (특수 지정예고는 별도 공시 확인 필요).")

    with tab2:
        st.write("### [단기과열 주가 요건]")
        if base_idx < 39:
            st.warning(f"기준일 이전 영업일이 {base_idx}일뿐이라 40일 평균 계산이 불가합니다.")
        else:
            avg_40_p = df["종가"].iloc[base_idx - 39: base_idx + 1].mean()
            over_p_limit = int(avg_40_p * 1.3)
            check_over = curr_p >= over_p_limit

            st.write(f"최근 40거래일 평균 종가: {int(avg_40_p):,}원")
            st.write(f"지정 기준가 (평균 대비 130%): **{over_p_limit:,}원**")

            if check_over:
                diff = curr_p - over_p_limit
                st.error(f"✅ 주가 요건 충족 (현재가가 {diff:,}원 높음)")
            else:
                diff = over_p_limit - curr_p
                st.success(f"❌ 주가 요건 미달 (기준가까지 {diff:,}원 남음)")

            st.caption("※ 단기과열은 주가 외에 거래회전율·변동성 요건이 모두 "
                       "충족되어야 하며, 실제 지정은 거래소 공시로 최종 확인 필요.")
