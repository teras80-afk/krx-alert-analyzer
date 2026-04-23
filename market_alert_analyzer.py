"""
투경예고 / 단기과열예고 종목 조회기
────────────────────────────────────────
종목 하나를 입력하면 현재 해당 종목의
 - 투자경고 예고 해당 여부
 - 단기과열 예고 해당 여부
를 계산해서 한눈에 보여줍니다.
"""
import streamlit as st
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta

st.set_page_config(page_title="예고 상태 조회기", layout="centered")
st.title("🔍 투경예고 / 단기과열예고 종목 조회")
st.caption("종목을 입력하면 최근 주가 데이터로 예고 해당 여부를 즉시 판정합니다.")


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
# 예고 판정 로직
# ─────────────────────────────────────────────────────────────
def check_warning_alert(df: pd.DataFrame, idx: int) -> dict:
    """투자경고 예고 판정 — 3대 요건 모두 충족 시 해당"""
    if idx < 20:
        return {"status": None, "reason": "데이터 부족(20거래일 미만)"}

    curr = int(df["종가"].iloc[idx])
    p5 = int(df["종가"].iloc[idx - 5])
    p20 = int(df["종가"].iloc[idx - 20])
    max15 = int(df["종가"].iloc[idx - 14: idx + 1].max())

    c1 = curr >= int(p5 * 1.6)
    c2 = curr >= int(p20 * 2.0)
    c3 = curr >= max15

    return {
        "status": all([c1, c2, c3]),
        "current": curr,
        "criteria": [
            {"label": "5일 전 대비 60% 상승",
             "base": p5, "threshold": int(p5 * 1.6), "pass": c1},
            {"label": "20일 전 대비 100% 상승",
             "base": p20, "threshold": int(p20 * 2.0), "pass": c2},
            {"label": "15거래일 중 최고가",
             "base": max15, "threshold": max15, "pass": c3},
        ],
    }


def check_overheat_alert(df: pd.DataFrame, idx: int) -> dict:
    """단기과열 예고 판정 — 주가 요건 (거래회전율·변동성은 별도)"""
    if idx < 39:
        return {"status": None, "reason": "데이터 부족(40거래일 미만)"}

    curr = int(df["종가"].iloc[idx])
    avg40 = df["종가"].iloc[idx - 39: idx + 1].mean()
    threshold = int(avg40 * 1.3)

    return {
        "status": curr >= threshold,
        "current": curr,
        "avg40": int(avg40),
        "threshold": threshold,
    }


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

# 기준일 선택
date_list = df.index.strftime("%Y-%m-%d").tolist()[::-1]
selected_date = st.selectbox("📅 기준일", date_list, index=0,
                              help="기본값은 가장 최근 거래일입니다.")

base_idx = df.index.get_loc(pd.Timestamp(selected_date))
if isinstance(base_idx, slice):
    base_idx = base_idx.stop - 1

# ═══════════════════════════════════════════════════════════
# 결과 표시 — 한눈에 보이게
# ═══════════════════════════════════════════════════════════
st.markdown(f"### 📍 {name} ({ticker})")
curr_p = int(df["종가"].iloc[base_idx])
st.markdown(f"**{selected_date} 종가:** {curr_p:,}원")

st.markdown("---")

warn = check_warning_alert(df, base_idx)
overheat = check_overheat_alert(df, base_idx)

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### ⚠️ 투자경고 예고")
    if warn["status"] is None:
        st.info(warn["reason"])
    elif warn["status"]:
        st.error("🔴 **예고 해당**\n\n3대 요건 모두 충족")
    else:
        n_pass = sum(1 for c in warn["criteria"] if c["pass"])
        st.success(f"🟢 **미해당**\n\n충족 요건: {n_pass} / 3")

with col2:
    st.markdown("#### 🔥 단기과열 예고")
    if overheat["status"] is None:
        st.info(overheat["reason"])
    elif overheat["status"]:
        st.error("🔴 **주가요건 충족**\n\n(회전율·변동성 별도 확인 필요)")
    else:
        diff = overheat["threshold"] - overheat["current"]
        st.success(f"🟢 **미해당**\n\n기준가까지 {diff:,}원 남음")

st.markdown("---")

# 상세 내역
with st.expander("📋 투자경고 예고 상세 요건", expanded=False):
    if warn["status"] is None:
        st.info(warn["reason"])
    else:
        for c in warn["criteria"]:
            mark = "✅" if c["pass"] else "❌"
            st.write(f"{mark} **{c['label']}**  "
                     f"기준가 {c['threshold']:,}원 vs 현재가 {curr_p:,}원")
        st.caption("※ 3개 요건 모두 충족 시 '예고 해당'으로 판정. "
                   "특수 지정예고(초단기·중기 등)는 별도 KRX 공시 확인 필요.")

with st.expander("📋 단기과열 예고 상세 요건", expanded=False):
    if overheat["status"] is None:
        st.info(overheat["reason"])
    else:
        st.write(f"- 40거래일 평균 종가: **{overheat['avg40']:,}원**")
        st.write(f"- 지정 기준가(평균×130%): **{overheat['threshold']:,}원**")
        st.write(f"- 현재 종가: **{overheat['current']:,}원**")
        st.caption("※ 단기과열은 주가 요건 외에 거래회전율·변동성 요건이 "
                   "모두 충족되어야 실제 예고 지정. 주가만으로는 부분 판정입니다.")

st.markdown("---")
st.caption("📌 본 도구는 공개 주가 데이터로 요건을 자체 계산합니다. "
           "최종 지정 여부는 한국거래소 공식 공시를 확인하세요.")
