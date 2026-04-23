"""
투경예고 / 단기과열예고 임계값 조회기
────────────────────────────────────────
종목을 입력하면 예고 발동 임계값을 표로 보여줍니다.
 - 각 조건의 기준값, 배수, 임계값(발동가), 현재가를 한눈에 비교
 - 한 조건이라도 미충족이면 예고 미해당
"""
import streamlit as st
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

st.set_page_config(page_title="예고 임계값 조회기", layout="centered")
st.title("🔍 투경예고 / 단기과열예고 임계값 조회")
st.caption("종목을 입력하면 예고 발동 기준가(임계값)와 현재가를 비교해 보여줍니다.")


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
# 임계값 계산
# ─────────────────────────────────────────────────────────────
def build_warning_table(df: pd.DataFrame, idx: int, curr: int) -> pd.DataFrame | None:
    """투자경고 3대 요건 표 생성"""
    if idx < 20:
        return None

    p5 = int(df["종가"].iloc[idx - 5])
    p20 = int(df["종가"].iloc[idx - 20])
    max15 = int(df["종가"].iloc[idx - 14: idx + 1].max())

    rows = [
        {"조건": "① 5일 전 대비 60% 상승",
         "기준값": f"{p5:,}원 (5일 전 종가)",
         "배수": "× 1.60",
         "임계값(발동가)": p5 * 1.6,
         "현재가": curr,
         "충족": curr >= p5 * 1.6},
        {"조건": "② 20일 전 대비 100% 상승",
         "기준값": f"{p20:,}원 (20일 전 종가)",
         "배수": "× 2.00",
         "임계값(발동가)": p20 * 2.0,
         "현재가": curr,
         "충족": curr >= p20 * 2.0},
        {"조건": "③ 15거래일 중 최고가",
         "기준값": f"{max15:,}원 (15일 최고가)",
         "배수": "× 1.00",
         "임계값(발동가)": max15,
         "현재가": curr,
         "충족": curr >= max15},
    ]
    return pd.DataFrame(rows)


def build_overheat_table(df: pd.DataFrame, idx: int, curr: int) -> pd.DataFrame | None:
    """단기과열 주가요건 표 생성"""
    if idx < 39:
        return None

    avg40 = df["종가"].iloc[idx - 39: idx + 1].mean()

    rows = [
        {"조건": "주가요건 (40일 평균 대비 130%)",
         "기준값": f"{int(avg40):,}원 (40일 평균 종가)",
         "배수": "× 1.30",
         "임계값(발동가)": avg40 * 1.3,
         "현재가": curr,
         "충족": curr >= avg40 * 1.3},
    ]
    return pd.DataFrame(rows)


def format_table(df: pd.DataFrame) -> pd.DataFrame:
    """표 숫자 포맷팅"""
    d = df.copy()
    d["임계값(발동가)"] = d["임계값(발동가)"].apply(lambda v: f"{int(v):,}원")
    d["현재가"] = d["현재가"].apply(lambda v: f"{int(v):,}원")
    d["충족"] = d["충족"].apply(lambda v: "✅" if v else "❌")
    return d


# ─────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────
user_input = st.text_input("📝 종목코드 6자리 또는 종목명",
                           value="009150",
                           placeholder="예: 삼성전자 또는 005930")

if not user_input:
    st.stop()

try:
    name_map = get_ticker_name_map()
except Exception as e:
    st.error(f"종목 리스트 로딩 실패: {e}")
    st.stop()

ticker = resolve_ticker(user_input, name_map)
if not ticker:
    st.error("종목을 찾지 못했습니다. 6자리 코드나 정확한 종목명을 입력하세요.")
    st.stop()

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
    st.stop()

date_list = df.index.strftime("%Y-%m-%d").tolist()[::-1]
selected_date = st.selectbox("📅 기준일", date_list, index=0,
                              help="기본값은 가장 최근 거래일입니다.")

base_idx = df.index.get_loc(pd.Timestamp(selected_date))
if isinstance(base_idx, slice):
    base_idx = base_idx.stop - 1

curr_p = int(df["종가"].iloc[base_idx])

# ═══════════════════════════════════════════════════════════
# 헤더
# ═══════════════════════════════════════════════════════════
st.markdown(f"### 📍 {name} ({ticker})")
st.markdown(f"**{selected_date} 종가:** <span style='font-size:24px;color:#d35400'>"
            f"{curr_p:,}원</span>", unsafe_allow_html=True)

st.markdown("---")

# ═══════════════════════════════════════════════════════════
# 투자경고 예고
# ═══════════════════════════════════════════════════════════
st.markdown("### ⚠️ 투자경고 예고")

warn_df = build_warning_table(df, base_idx, curr_p)
if warn_df is None:
    st.info("데이터 부족(20거래일 미만)")
else:
    all_pass = warn_df["충족"].all()
    if all_pass:
        st.error("🔴 **예고 해당** — 3대 요건 모두 충족")
    else:
        n_pass = int(warn_df["충족"].sum())
        st.success(f"🟢 **미해당** — 충족 요건: {n_pass} / 3")

    st.dataframe(format_table(warn_df), use_container_width=True,
                 hide_index=True)
    st.caption("※ 3개 요건 **모두** 충족 시 예고 해당. "
               "특수 지정예고(초단기·중기 등)는 별도 KRX 공시 확인 필요.")

st.markdown("---")

# ═══════════════════════════════════════════════════════════
# 단기과열 예고
# ═══════════════════════════════════════════════════════════
st.markdown("### 🔥 단기과열 예고 (주가요건)")

oh_df = build_overheat_table(df, base_idx, curr_p)
if oh_df is None:
    st.info("데이터 부족(40거래일 미만)")
else:
    if bool(oh_df["충족"].iloc[0]):
        st.error("🔴 **주가요건 충족** — 회전율·변동성 별도 확인 필요")
    else:
        threshold = int(oh_df["임계값(발동가)"].iloc[0])
        gap = threshold - curr_p
        st.success(f"🟢 **미해당** — 임계값까지 {gap:,}원 남음")

    st.dataframe(format_table(oh_df), use_container_width=True,
                 hide_index=True)
    st.caption("※ 단기과열은 주가 요건 외에 거래회전율·변동성 요건이 "
               "모두 충족되어야 실제 예고 지정. 본 표는 **주가 요건만** 판정합니다.")

st.markdown("---")
st.caption("📌 본 도구는 공개 주가 데이터로 요건을 자체 계산한 참고용입니다. "
           "최종 지정 여부는 한국거래소 공식 공시를 확인하세요.")
