"""
투경예고 / 단기과열예고 임계값 조회기 + 관심종목 대시보드
────────────────────────────────────────────────────────
탭 구성:
 [1] 개별 종목 조회  — 한 종목을 자세히 들여다봄
 [2] 관심종목 대시보드 — 등록한 여러 종목을 한번에 체크, 예고 해당은 맨 위로 정렬
"""
import streamlit as st
import pandas as pd
import FinanceDataReader as fdr
from datetime import datetime, timedelta
from pathlib import Path

st.set_page_config(page_title="예고 임계값 조회기", layout="wide")
st.title("🔍 투경예고 / 단기과열예고 조회")


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


@st.cache_data(ttl=60)
def load_watchlist() -> list:
    """watchlist.txt 파일에서 관심종목 불러오기 (없으면 빈 리스트)"""
    path = Path("watchlist.txt")
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


# ─────────────────────────────────────────────────────────────
# 임계값 계산
# ─────────────────────────────────────────────────────────────
def evaluate_stock(df: pd.DataFrame, idx: int) -> dict:
    """한 종목의 예고 상태를 종합 판정."""
    curr = int(df["종가"].iloc[idx])
    result = {"현재가": curr, "투경예고": None, "단기과열예고": None,
              "투경_상세": None, "단기_상세": None}

    # 투자경고
    if idx >= 20:
        p5 = int(df["종가"].iloc[idx - 5])
        p20 = int(df["종가"].iloc[idx - 20])
        max15 = int(df["종가"].iloc[idx - 14: idx + 1].max())
        th1, th2, th3 = int(p5 * 1.6), int(p20 * 2.0), max15
        c1, c2, c3 = curr >= th1, curr >= th2, curr >= th3
        result["투경예고"] = all([c1, c2, c3])
        result["투경_상세"] = {
            "① 5일×1.6": (th1, c1),
            "② 20일×2.0": (th2, c2),
            "③ 15일최고": (th3, c3),
        }

    # 단기과열
    if idx >= 39:
        avg40 = df["종가"].iloc[idx - 39: idx + 1].mean()
        th = int(avg40 * 1.3)
        result["단기과열예고"] = curr >= th
        result["단기_상세"] = {"40일평균×1.3": (th, curr >= th)}

    return result


def format_warning_table(ev: dict, curr: int) -> pd.DataFrame:
    rows = []
    for label, (th, ok) in ev["투경_상세"].items():
        rows.append({
            "조건": label,
            "임계값(발동가)": f"{th:,}원",
            "현재가": f"{curr:,}원",
            "충족": "✅" if ok else "❌",
        })
    return pd.DataFrame(rows)


def format_overheat_table(ev: dict, curr: int) -> pd.DataFrame:
    rows = []
    for label, (th, ok) in ev["단기_상세"].items():
        rows.append({
            "조건": label,
            "임계값(발동가)": f"{th:,}원",
            "현재가": f"{curr:,}원",
            "충족": "✅" if ok else "❌",
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────
# 공통: 종목명 매핑 로드
# ─────────────────────────────────────────────────────────────
try:
    name_map = get_ticker_name_map()
except Exception as e:
    st.error(f"종목 리스트 로딩 실패: {e}")
    st.stop()


# ═══════════════════════════════════════════════════════════
# 탭 구성
# ═══════════════════════════════════════════════════════════
tab1, tab2 = st.tabs(["🎯 개별 종목 조회", "📋 관심종목 대시보드"])

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
                with col_date_space:
                    date_list = df.index.strftime("%Y-%m-%d").tolist()[::-1]
                    selected_date = st.selectbox("기준일", date_list, index=0,
                                                  key="single_date")

                base_idx = df.index.get_loc(pd.Timestamp(selected_date))
                if isinstance(base_idx, slice):
                    base_idx = base_idx.stop - 1

                ev = evaluate_stock(df, base_idx)
                curr_p = ev["현재가"]

                st.markdown(f"### 📍 {name} ({ticker})")
                st.markdown(f"**{selected_date} 종가:** "
                            f"<span style='font-size:22px;color:#d35400'>"
                            f"{curr_p:,}원</span>", unsafe_allow_html=True)
                st.markdown("---")

                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("#### ⚠️ 투자경고 예고")
                    if ev["투경예고"] is None:
                        st.info("데이터 부족")
                    elif ev["투경예고"]:
                        st.error("🔴 예고 해당 — 3대 요건 모두 충족")
                    else:
                        n_ok = sum(1 for _, v in ev["투경_상세"].values() if v)
                        st.success(f"🟢 미해당 — 충족 {n_ok}/3")
                    if ev["투경_상세"]:
                        st.dataframe(format_warning_table(ev, curr_p),
                                     use_container_width=True, hide_index=True)

                with c2:
                    st.markdown("#### 🔥 단기과열 예고")
                    if ev["단기과열예고"] is None:
                        st.info("데이터 부족")
                    elif ev["단기과열예고"]:
                        st.error("🔴 주가요건 충족")
                    else:
                        st.success("🟢 미해당")
                    if ev["단기_상세"]:
                        st.dataframe(format_overheat_table(ev, curr_p),
                                     use_container_width=True, hide_index=True)
                    st.caption("※ 단기과열은 회전율·변동성 요건 별도 확인 필요")


# ───────────────────────────────────────────────────────────
# 탭 2: 관심종목 대시보드
# ───────────────────────────────────────────────────────────
with tab2:
    st.caption("관심종목 리스트는 저장소의 `watchlist.txt` 파일에서 관리합니다. "
               "아래 편집창에서 임시로 추가/변경해도 되지만, 영구 저장하려면 GitHub에서 파일을 수정하세요.")

    default_watchlist = load_watchlist()
    watch_text = st.text_area(
        "관심종목 (한 줄에 하나씩, 종목명 또는 6자리 코드)",
        value="\n".join(default_watchlist) if default_watchlist
              else "삼성전자\n009150\nSK하이닉스",
        height=150,
        help="예: 삼성전자 / 005930 / 퍼스텍 (혼용 가능)",
    )

    if st.button("🔄 전체 조회", type="primary"):
        lines = [ln.strip() for ln in watch_text.splitlines()
                 if ln.strip() and not ln.startswith("#")]
        if not lines:
            st.warning("관심종목을 입력하세요.")
            st.stop()

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

        summary_rows = []
        progress = st.progress(0, text="조회 중...")

        for i, line in enumerate(lines):
            progress.progress((i + 1) / len(lines), text=f"조회 중... {line}")
            ticker = resolve_ticker(line, name_map)
            if not ticker:
                summary_rows.append({
                    "종목": line, "코드": "—", "종가": "—",
                    "투경예고": "❓ 종목 못찾음", "단기과열예고": "❓",
                    "_정렬": 99,
                })
                continue

            name = name_map[ticker]
            try:
                df = load_ohlcv(ticker, start, end)
                if df.empty:
                    raise ValueError("empty")
                ev = evaluate_stock(df, len(df) - 1)
            except Exception as e:
                summary_rows.append({
                    "종목": name, "코드": ticker, "종가": "—",
                    "투경예고": f"❓ 조회실패", "단기과열예고": "❓",
                    "_정렬": 99,
                })
                continue

            # 예고 해당 종목을 맨 위로 정렬
            rank = 0 if (ev["투경예고"] or ev["단기과열예고"]) else 1

            def mark(v):
                if v is None:
                    return "—"
                return "🔴 해당" if v else "🟢 미해당"

            summary_rows.append({
                "종목": name,
                "코드": ticker,
                "종가": f"{ev['현재가']:,}원",
                "투경예고": mark(ev["투경예고"]),
                "단기과열예고": mark(ev["단기과열예고"]),
                "_정렬": rank,
            })

        progress.empty()

        df_sum = pd.DataFrame(summary_rows).sort_values("_정렬").drop(columns=["_정렬"])

        # 헤드라인 요약
        alert_count = df_sum["투경예고"].str.contains("🔴").sum() \
                    + df_sum["단기과열예고"].str.contains("🔴").sum()
        if alert_count > 0:
            st.error(f"🚨 예고 해당 건수: **{alert_count}건** (위쪽에 정렬됨)")
        else:
            st.success("✅ 관심종목 중 예고 해당 없음")

        st.dataframe(df_sum, use_container_width=True, hide_index=True)

        st.caption(f"기준일: {end} | 조회 종목 수: {len(lines)}개")

st.markdown("---")
st.caption("📌 공개 주가 데이터 기반 자체 계산 결과입니다. "
           "최종 지정 여부는 한국거래소 공식 공시를 확인하세요.")
