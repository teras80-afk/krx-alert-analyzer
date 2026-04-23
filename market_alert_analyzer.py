"""
투경예고 / 단기과열예고 임계값 조회기 + 관심종목 대시보드
────────────────────────────────────────────────────────
 [1] 개별 종목 조회
 [2] 관심종목 대시보드 (앱 내 편집 → GitHub 자동 저장)

단기과열 판정 — KRX 일반종목 3대 요건 모두 구현:
 ① 주가: 당일 종가 ≥ 40일 평균 종가 × 1.3
 ② 회전율: 최근 2일 평균 거래량 ≥ 40일 평균 거래량 × 6
 ③ 변동성: 최근 2일 평균 (고-저)/전일종가 ≥ 40일 평균 × 1.5
"""
import streamlit as st
import pandas as pd
import requests
import base64
import FinanceDataReader as fdr
from datetime import datetime, timedelta

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


def github_get_watchlist() -> tuple[str, str | None]:
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


def github_put_watchlist(new_content: str, sha: str | None) -> tuple[bool, str]:
    cfg = _github_config()
    if not cfg:
        return False, "GitHub 연동 설정이 없습니다"
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{cfg['path']}"
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Accept": "application/vnd.github+json"}
    body = {
        "message": f"Update watchlist via app ({datetime.now():%Y-%m-%d %H:%M})",
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": cfg["branch"],
    }
    if sha:
        body["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=body, timeout=10)
        if r.status_code in (200, 201):
            return True, "✅ GitHub에 저장 완료!"
        return False, f"❌ HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"❌ {e}"


def parse_watchlist(text: str) -> list:
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]


# ─────────────────────────────────────────────────────────────
# 예고 판정 로직 (핵심)
# ─────────────────────────────────────────────────────────────
def evaluate_warning(df: pd.DataFrame, idx: int) -> dict:
    """투자경고 3대 요건 판정"""
    curr = int(df["종가"].iloc[idx])
    if idx < 20:
        return {"status": None, "reason": "데이터 부족(20거래일 미만)"}

    p5 = int(df["종가"].iloc[idx - 5])
    p20 = int(df["종가"].iloc[idx - 20])
    max15 = int(df["종가"].iloc[idx - 14: idx + 1].max())

    th1, th2, th3 = int(p5 * 1.6), int(p20 * 2.0), max15
    c1, c2, c3 = curr >= th1, curr >= th2, curr >= th3

    return {
        "status": all([c1, c2, c3]),
        "current": curr,
        "criteria": [
            ("① 5일 전 × 1.6", th1, c1),
            ("② 20일 전 × 2.0", th2, c2),
            ("③ 15일 최고가", th3, c3),
        ],
    }


def evaluate_overheat(df: pd.DataFrame, idx: int) -> dict:
    """단기과열 3대 요건 판정 (일반종목 기준)"""
    if idx < 40:  # 40일 평균 + 최근 2일 + 변동성용 전일종가 1일
        return {"status": None, "reason": "데이터 부족(40거래일 미만)"}

    curr = int(df["종가"].iloc[idx])

    # ① 주가: 당일 종가 >= 40일 평균 종가 × 1.3
    avg40_close = df["종가"].iloc[idx - 39: idx + 1].mean()
    price_th = int(avg40_close * 1.3)
    c1 = curr >= price_th

    # ② 회전율: 최근 2일 평균 거래량 >= 40일 평균 거래량 × 6
    avg2_vol = df["거래량"].iloc[idx - 1: idx + 1].mean()
    avg40_vol = df["거래량"].iloc[idx - 39: idx + 1].mean()
    vol_ratio = avg2_vol / avg40_vol if avg40_vol > 0 else 0
    c2 = vol_ratio >= 6.0

    # ③ 변동성: 최근 2일 평균 변동성(고-저)/전일종가 >= 40일 평균 × 1.5
    prev_close = df["종가"].shift(1)
    daily_vola = (df["고가"] - df["저가"]) / prev_close
    avg2_vola = daily_vola.iloc[idx - 1: idx + 1].mean()
    avg40_vola = daily_vola.iloc[idx - 39: idx + 1].mean()
    vola_ratio = avg2_vola / avg40_vola if avg40_vola > 0 else 0
    c3 = vola_ratio >= 1.5

    return {
        "status": all([c1, c2, c3]),
        "current": curr,
        "criteria": [
            ("① 주가 (40일평균 × 1.3)",
             f"{price_th:,}원", f"{curr:,}원", c1),
            ("② 회전율 (40일평균 × 6배)",
             f"{avg40_vol * 6:,.0f}주", f"{avg2_vol:,.0f}주 ({vol_ratio:.2f}배)", c2),
            ("③ 변동성 (40일평균 × 1.5배)",
             f"{avg40_vola * 1.5 * 100:.2f}%",
             f"{avg2_vola * 100:.2f}% ({vola_ratio:.2f}배)", c3),
        ],
    }


def fmt_warning_table(ev: dict) -> pd.DataFrame:
    rows = [{"조건": k, "임계값(발동가)": f"{th:,}원",
             "현재가": f"{ev['current']:,}원",
             "충족": "✅" if ok else "❌"}
            for (k, th, ok) in ev["criteria"]]
    return pd.DataFrame(rows)


def fmt_overheat_table(ev: dict) -> pd.DataFrame:
    rows = [{"조건": k, "기준값": th, "현재값": cur,
             "충족": "✅" if ok else "❌"}
            for (k, th, cur, ok) in ev["criteria"]]
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────────────
# 종목명 매핑
# ─────────────────────────────────────────────────────────────
try:
    name_map = get_ticker_name_map()
except Exception as e:
    st.error(f"종목 리스트 로딩 실패: {e}")
    st.stop()


# ═══════════════════════════════════════════════════════════
# 탭
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
                    elif warn_ev["status"]:
                        st.error("🔴 예고 해당 — 3대 요건 모두 충족")
                    else:
                        n_ok = sum(1 for _, _, ok in warn_ev["criteria"] if ok)
                        st.success(f"🟢 미해당 — 충족 {n_ok}/3")
                    if warn_ev.get("criteria"):
                        st.dataframe(fmt_warning_table(warn_ev),
                                     use_container_width=True, hide_index=True)

                with c2:
                    st.markdown("#### 🔥 단기과열 예고")
                    if oh_ev["status"] is None:
                        st.info(oh_ev["reason"])
                    elif oh_ev["status"]:
                        st.error("🔴 예고 해당 — 주가·회전율·변동성 모두 충족")
                    else:
                        n_ok = sum(1 for _, _, _, ok in oh_ev["criteria"] if ok)
                        if n_ok >= 2:
                            st.warning(f"🟡 일부 충족 ({n_ok}/3) — 예고 미해당")
                        else:
                            st.success(f"🟢 미해당 ({n_ok}/3)")
                    if oh_ev.get("criteria"):
                        st.dataframe(fmt_overheat_table(oh_ev),
                                     use_container_width=True, hide_index=True)


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
        if st.button("🔄 GitHub에서 새로고침"):
            content, sha = github_get_watchlist()
            if content:
                st.session_state.watchlist_text = content
                st.session_state.watchlist_sha = sha
                st.success("최신 내용으로 갱신되었습니다.")
                st.rerun()

    if edit_mode:
        st.markdown("**📝 관심종목 편집** — 한 줄에 하나씩")
        new_text = st.text_area(
            "편집창",
            value=st.session_state.watchlist_text,
            height=250,
            label_visibility="collapsed",
        )
        col_save, col_cancel = st.columns([1, 1])
        with col_save:
            if st.button("💾 GitHub에 저장", type="primary",
                         use_container_width=True):
                if _github_config() is None:
                    st.error("GitHub 연동 미설정. Secrets 확인 필요.")
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
            if st.button("↩️ 변경 취소", use_container_width=True):
                st.rerun()
    else:
        lines = parse_watchlist(st.session_state.watchlist_text)

        if not lines:
            st.warning("관심종목이 없습니다. 편집 모드를 켜세요.")
        else:
            st.caption(f"등록된 관심종목: **{len(lines)}개**")

            if st.button("🔄 전체 조회", type="primary"):
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

                rows = []
                progress = st.progress(0, text="조회 중...")

                for i, line in enumerate(lines):
                    progress.progress((i + 1) / len(lines), text=f"조회 중... {line}")
                    ticker = resolve_ticker(line, name_map)
                    if not ticker:
                        rows.append({"종목": line, "코드": "—", "종가": "—",
                                     "투경예고": "❓", "단기과열예고": "❓",
                                     "_정렬": 99})
                        continue

                    name = name_map[ticker]
                    try:
                        df = load_ohlcv(ticker, start, end)
                        if df.empty:
                            raise ValueError
                        idx = len(df) - 1
                        warn_ev = evaluate_warning(df, idx)
                        oh_ev = evaluate_overheat(df, idx)
                    except Exception:
                        rows.append({"종목": name, "코드": ticker, "종가": "—",
                                     "투경예고": "❓", "단기과열예고": "❓",
                                     "_정렬": 99})
                        continue

                    curr = int(df["종가"].iloc[-1])

                    def mark(ev, count_total=3):
                        if ev["status"] is None:
                            return "—"
                        if ev["status"]:
                            return "🔴 해당"
                        n_ok = sum(1 for row in ev["criteria"]
                                   if row[-1])
                        return f"🟢 {n_ok}/{count_total}"

                    rank = 0 if (warn_ev["status"] or oh_ev["status"]) else 1

                    rows.append({
                        "종목": name, "코드": ticker,
                        "종가": f"{curr:,}원",
                        "투경예고": mark(warn_ev),
                        "단기과열예고": mark(oh_ev),
                        "_정렬": rank,
                    })

                progress.empty()
                df_sum = pd.DataFrame(rows).sort_values("_정렬").drop(columns=["_정렬"])

                alerts = (df_sum["투경예고"].str.contains("🔴").sum()
                          + df_sum["단기과열예고"].str.contains("🔴").sum())
                if alerts > 0:
                    st.error(f"🚨 예고 해당: **{alerts}건**")
                else:
                    st.success("✅ 예고 해당 없음")

                st.dataframe(df_sum, use_container_width=True, hide_index=True)
                st.caption(f"기준일: {end} | 조회 {len(lines)}개")

st.markdown("---")
st.caption("📌 공개 주가 데이터 기반 자체 계산입니다. "
           "회전율은 거래량 비율로 근사 계산하며, 변동성은 (고-저)/전일종가 기준입니다. "
           "최종 지정 여부는 한국거래소 공식 공시로 확인하세요.")
