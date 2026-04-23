"""
시장경보(예고/지정) 실시간 리스트 & 요건 분석기
────────────────────────────────────────────────
실행: streamlit run market_alert_analyzer.py
"""
import streamlit as st
from pykrx import stock
import pandas as pd
import requests
from datetime import datetime, timedelta

st.set_page_config(page_title="시장경보 통합 분석기", layout="wide")
st.title("🚨 시장경보(예고/지정) 실시간 리스트 & 요건 분석")


# ─────────────────────────────────────────────────────────────
# 공용 유틸
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_nearest_business_day(base: datetime = None) -> str:
    """장 마감(15:30) 이전이거나 휴일이면 직전 영업일로 보정."""
    base = base or datetime.now()
    # 장중(15:30 이전)이면 전일 데이터가 안정적
    if base.hour < 15 or (base.hour == 15 and base.minute < 30):
        base = base - timedelta(days=1)
    # pykrx가 인식하는 가장 가까운 영업일 탐색
    for i in range(10):
        d = (base - timedelta(days=i)).strftime("%Y%m%d")
        try:
            if not stock.get_market_ohlcv(d, "005930").empty:
                return d
        except Exception:
            continue
    return base.strftime("%Y%m%d")


@st.cache_data(ttl=3600)
def get_ticker_name_map() -> dict:
    """전체 티커-종목명 매핑을 한 번에 생성 (캐시 1시간)."""
    mapping = {}
    for mkt in ("KOSPI", "KOSDAQ"):
        for t in stock.get_market_ticker_list(market=mkt):
            mapping[t] = stock.get_market_ticker_name(t)
    return mapping


def resolve_ticker(user_input: str, name_map: dict) -> str | None:
    """종목코드 6자리 또는 종목명을 받아 티커 반환."""
    s = user_input.strip()
    if s.isdigit() and len(s) == 6:
        return s if s in name_map else None
    # 정확히 일치하는 종목명 우선
    for t, n in name_map.items():
        if n == s:
            return t
    # 포함 검색 (종목명 일부 입력 허용)
    hits = [t for t, n in name_map.items() if s in n]
    return hits[0] if len(hits) == 1 else None


# ─────────────────────────────────────────────────────────────
# 시장경보 현황 — KRX 내부 엔드포인트 직접 호출
# ─────────────────────────────────────────────────────────────
KRX_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
KRX_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/",
}


@st.cache_data(ttl=600)
def fetch_krx_bld(bld: str, trd_dd: str, extra: dict = None) -> pd.DataFrame:
    """KRX bld 엔드포인트 호출 → DataFrame."""
    data = {"bld": bld, "trdDd": trd_dd, "share": "1", "money": "1",
            "csvxls_isNo": "false"}
    if extra:
        data.update(extra)
    r = requests.post(KRX_URL, headers=KRX_HEADERS, data=data, timeout=10)
    r.raise_for_status()
    js = r.json()
    # 응답 블록명은 엔드포인트별로 다름 (block1, output 등)
    for key in ("OutBlock_1", "output", "block1"):
        if key in js and js[key]:
            return pd.DataFrame(js[key])
    return pd.DataFrame()


@st.cache_data(ttl=600)
def get_market_alerts(trd_dd: str) -> pd.DataFrame:
    """
    시장경보 종목 현황(투자주의/경고/위험 + 단기과열).
    ※ bld 값은 KRX 정보데이터시스템에서 크롬 개발자도구로 확인한 값.
      거래소가 내부 경로를 바꾸면 여기만 수정하면 됨.
    """
    frames = []

    # 단기과열 종목 현황
    df1 = fetch_krx_bld("dbms/MDC/STAT/standard/MDCSTAT14001", trd_dd)
    if not df1.empty:
        df1["구분"] = "단기과열"
        frames.append(df1)

    # 시장경보 종목(투자주의/경고/위험) 현황
    df2 = fetch_krx_bld("dbms/MDC/STAT/standard/MDCSTAT14002", trd_dd)
    if not df2.empty:
        df2["구분"] = "시장경보"
        frames.append(df2)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ─────────────────────────────────────────────────────────────
# 1. 현재 시장경보 발령 종목 현황
# ─────────────────────────────────────────────────────────────
target_date = get_nearest_business_day()

st.subheader("📢 현재 시장경보 발령 종목 현황")
try:
    alert_df = get_market_alerts(target_date)
    if not alert_df.empty:
        st.dataframe(alert_df, use_container_width=True, hide_index=True)
        st.caption(f"기준일: {target_date} | 출처: KRX 정보데이터시스템")
    else:
        st.info("현재 발령된 시장경보 종목이 없거나, KRX 응답이 비어있습니다.")
except Exception as e:
    st.error(f"리스트 호출 오류: {e}")
    st.caption("⚠️ KRX가 내부 bld 경로를 변경했을 수 있습니다. "
               "data.krx.co.kr 에서 개발자도구로 최신 bld 값을 확인해 주세요.")

st.divider()

# ─────────────────────────────────────────────────────────────
# 2. 특정 종목 지정 요건 정밀 분석
# ─────────────────────────────────────────────────────────────
col_input, col_date = st.columns([2, 1])
with col_input:
    user_input = st.text_input("📝 분석할 종목코드 6자리 또는 종목명", "010820")

if user_input:
    with st.spinner("종목명 인덱스 로딩 중..."):
        name_map = get_ticker_name_map()

    ticker = resolve_ticker(user_input, name_map)
    if not ticker:
        st.error("종목을 찾지 못했습니다. 6자리 코드 또는 정확한 종목명을 입력하세요.")
        st.stop()

    name = name_map[ticker]

    # OHLCV 로드 (40일 평균 + 여유분)
    start = (datetime.strptime(target_date, "%Y%m%d")
             - timedelta(days=200)).strftime("%Y%m%d")
    df = stock.get_market_ohlcv_by_date(start, target_date, ticker)

    if df.empty:
        st.error("OHLCV 데이터를 불러오지 못했습니다.")
        st.stop()

    # 중복 인덱스 제거 + 정렬
    df = df[~df.index.duplicated(keep="last")].sort_index()

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
