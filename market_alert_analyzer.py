"""
투경예고 / 단기과열예고 임계값 조회기 + 관심종목 대시보드
────────────────────────────────────────────────────────
 [1] 개별 종목 조회
 [2] 관심종목 대시보드 (앱 내 편집 → GitHub 자동 저장)
"""
import streamlit as st
import pandas as pd
import requests
import base64
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


# ─────────────────────────────────────────────────────────────
# GitHub 연동 (watchlist.txt 읽기/쓰기)
# ─────────────────────────────────────────────────────────────
def _github_config():
    """st.secrets에서 GitHub 설정 읽기. 설정 없으면 None 반환."""
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
    """
    GitHub에서 watchlist.txt 내용과 SHA 가져오기.
    반환: (content, sha)  — sha는 나중에 업데이트할 때 필요
    실패 시 로컬 파일에서 읽기 시도, 그것도 실패하면 ("", None)
    """
    cfg = _github_config()
    if not cfg:
        # fallback: 로컬 파일
        p = Path(cfg["path"] if cfg else "watchlist.txt")
        if p.exists():
            return p.read_text(encoding="utf-8"), None
        return "", None

    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{cfg['path']}"
    headers = {"Authorization": f"Bearer {cfg['token']}",
               "Accept": "application/vnd.github+json"}
    params = {"ref": cfg["branch"]}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 200:
            js = r.json()
            content = base64.b64decode(js["content"]).decode("utf-8")
            return content, js["sha"]
        else:
            return "", None
    except Exception:
        return "", None


def github_put_watchlist(new_content: str, sha: str | None) -> tuple[bool, str]:
    """
    GitHub에 watchlist.txt 업데이트.
    반환: (성공 여부, 메시지)
    """
    cfg = _github_config()
    if not cfg:
        return False, "GitHub 연동 설정이 없습니다 (Streamlit Secrets 확인 필요)"

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
        else:
            return False, f"❌ 저장 실패: HTTP {r.status_code} — {r.text[:200]}"
    except Exception as e:
        return False, f"❌ 저장 실패: {e}"


def parse_watchlist(text: str) -> list:
    """텍스트에서 종목 추출 (주석·빈줄 제외)"""
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]


# ─────────────────────────────────────────────────────────────
# 임계값 계산
# ─────────────────────────────────────────────────────────────
def evaluate_stock(df: pd.DataFrame, idx: int) -> dict:
    curr = int(df["종가"].iloc[idx])
    result = {"현재가": curr, "투경예고": None, "단기과열예고": None,
              "투경_상세": None, "단기_상세": None}
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
    if idx >= 39:
        avg40 = df["종가"].iloc[idx - 39: idx + 1].mean()
        th = int(avg40 * 1.3)
        result["단기과열예고"] = curr >= th
        result["단기_상세"] = {"40일평균×1.3": (th, curr >= th)}
    return result


def fmt_detail_table(detail: dict, curr: int) -> pd.DataFrame:
    rows = [{"조건": k, "임계값(발동가)": f"{th:,}원",
             "현재가": f"{curr:,}원", "충족": "✅" if ok else "❌"}
            for k, (th, ok) in detail.items()]
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
                        st.dataframe(fmt_detail_table(ev["투경_상세"], curr_p),
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
                        st.dataframe(fmt_detail_table(ev["단기_상세"], curr_p),
                                     use_container_width=True, hide_index=True)
                    st.caption("※ 단기과열은 회전율·변동성 별도 확인 필요")


# ───────────────────────────────────────────────────────────
# 탭 2: 관심종목 대시보드
# ───────────────────────────────────────────────────────────
with tab2:
    # 세션 상태 초기화 — 앱 시작 시 GitHub에서 로드
    if "watchlist_text" not in st.session_state:
        content, sha = github_get_watchlist()
        st.session_state.watchlist_text = content or "삼성전자\nSK하이닉스\n009150"
        st.session_state.watchlist_sha = sha

    # 편집 모드 토글
    col_mode, col_reload = st.columns([3, 1])
    with col_mode:
        edit_mode = st.toggle("✏️ 편집 모드", value=False,
                               help="켜면 관심종목을 수정할 수 있습니다")
    with col_reload:
        if st.button("🔄 GitHub에서 새로고침", help="최신 저장 내용으로 갱신"):
            content, sha = github_get_watchlist()
            if content:
                st.session_state.watchlist_text = content
                st.session_state.watchlist_sha = sha
                st.success("최신 내용으로 갱신되었습니다.")
                st.rerun()

    if edit_mode:
        st.markdown("**📝 관심종목 편집** — 한 줄에 하나씩, 종목명 또는 6자리 코드")
        new_text = st.text_area(
            "편집창",
            value=st.session_state.watchlist_text,
            height=250,
            label_visibility="collapsed",
            help="'#'으로 시작하는 줄은 주석처럼 무시됩니다",
        )

        col_save, col_cancel = st.columns([1, 1])
        with col_save:
            if st.button("💾 GitHub에 저장", type="primary",
                         use_container_width=True):
                if _github_config() is None:
                    st.error("GitHub 연동이 설정되지 않았습니다. "
                             "Streamlit Cloud의 Secrets에 GITHUB_TOKEN 등을 등록해 주세요.")
                else:
                    ok, msg = github_put_watchlist(
                        new_text, st.session_state.watchlist_sha)
                    if ok:
                        st.session_state.watchlist_text = new_text
                        # 저장 후 최신 SHA 다시 받아오기
                        _, new_sha = github_get_watchlist()
                        st.session_state.watchlist_sha = new_sha
                        st.success(msg)
                        st.info("다른 기기에서 앱을 열면 이 내용이 반영됩니다.")
                    else:
                        st.error(msg)
        with col_cancel:
            if st.button("↩️ 변경 취소", use_container_width=True):
                st.rerun()

        st.caption("💡 저장 없이 페이지를 떠나면 변경사항은 사라집니다.")
    else:
        # 조회 모드 — 현재 리스트로 바로 조회
        lines = parse_watchlist(st.session_state.watchlist_text)

        if not lines:
            st.warning("관심종목이 없습니다. '편집 모드'를 켜서 추가하세요.")
        else:
            st.caption(f"등록된 관심종목: **{len(lines)}개**  "
                       "(편집하려면 위 '편집 모드' 토글을 켜세요)")

            if st.button("🔄 전체 조회", type="primary"):
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")

                summary_rows = []
                progress = st.progress(0, text="조회 중...")

                for i, line in enumerate(lines):
                    progress.progress((i + 1) / len(lines),
                                       text=f"조회 중... {line}")
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
                    except Exception:
                        summary_rows.append({
                            "종목": name, "코드": ticker, "종가": "—",
                            "투경예고": "❓ 조회실패", "단기과열예고": "❓",
                            "_정렬": 99,
                        })
                        continue

                    rank = 0 if (ev["투경예고"] or ev["단기과열예고"]) else 1

                    def mark(v):
                        if v is None:
                            return "—"
                        return "🔴 해당" if v else "🟢 미해당"

                    summary_rows.append({
                        "종목": name, "코드": ticker,
                        "종가": f"{ev['현재가']:,}원",
                        "투경예고": mark(ev["투경예고"]),
                        "단기과열예고": mark(ev["단기과열예고"]),
                        "_정렬": rank,
                    })

                progress.empty()

                df_sum = pd.DataFrame(summary_rows).sort_values(
                    "_정렬").drop(columns=["_정렬"])

                alert_count = (df_sum["투경예고"].str.contains("🔴").sum()
                               + df_sum["단기과열예고"].str.contains("🔴").sum())
                if alert_count > 0:
                    st.error(f"🚨 예고 해당 건수: **{alert_count}건** "
                             f"(위쪽에 정렬됨)")
                else:
                    st.success("✅ 관심종목 중 예고 해당 없음")

                st.dataframe(df_sum, use_container_width=True, hide_index=True)
                st.caption(f"기준일: {end} | 조회 종목 수: {len(lines)}개")

st.markdown("---")
st.caption("📌 공개 주가 데이터 기반 자체 계산 결과입니다. "
           "최종 지정 여부는 한국거래소 공식 공시를 확인하세요.")
