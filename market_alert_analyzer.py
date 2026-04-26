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
# 데이터 로더
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def get_ticker_name_map() -> dict:
    import requests as _req
    from io import StringIO as _SIO
    result = {}
    for market_code in ["0", "1"]:  # 0=KOSPI, 1=KOSDAQ
        try:
            url = (f"https://kind.krx.co.kr/corpgeneral/corpList.do"
                   f"?method=download&searchType=13&marketType={market_code}")
            r = _req.get(url, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0"})
            df = pd.read_html(_SIO(r.content.decode("euc-kr", errors="replace")))[0]
            df.columns = df.columns.astype(str)
            name_col = [c for c in df.columns if "회사" in c or "종목" in c or "기업" in c]
            code_col = [c for c in df.columns if "종목코드" in c or "코드" in c]
            if name_col and code_col:
                for _, row in df.iterrows():
                    code = str(row[code_col[0]]).zfill(6)
                    name = str(row[name_col[0]])
                    result[code] = name
        except Exception:
            continue
    if result:
        return result
    raise RuntimeError("종목 리스트 로딩 실패")


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
    import json as _json
    import urllib.request as _urllib
    url = f"https://api.github.com/repos/{cfg['repo']}/contents/{cfg['path']}"
    body = {
        "message": f"Update watchlist ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
        "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
        "branch": cfg["branch"],
    }
    if sha:
        body["sha"] = sha
    try:
        data = _json.dumps(body, ensure_ascii=True).encode("utf-8")
        req = _urllib.Request(
            url, data=data, method="PUT",
            headers={
                "Authorization": f"Bearer {cfg['token']}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
            }
        )
        with _urllib.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                return True, "저장 완료"
            return False, f"HTTP 오류 {resp.status}"
    except Exception as e:
        return False, str(e)


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
    return f"저장 실패: {msg}"


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
# 종목명 매핑
# ─────────────────────────────────────────────────────────────
try:
    name_map = get_ticker_name_map()
except Exception as e:
    st.error(f"종목 리스트 로딩 실패: {e}")
    st.stop()


# ═══════════════════════════════════════════════════════════
# 탭 3개
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
                    name = name_map[ticker]
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
            value="", key="diag_input",
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
        else:
            d_name = name_map[d_ticker]

            d_end = (datetime.combine(diag_trigger_date, datetime.min.time())
                     + timedelta(days=10)).strftime("%Y-%m-%d")
            d_start = (datetime.combine(diag_trigger_date, datetime.min.time())
                       - timedelta(days=300)).strftime("%Y-%m-%d")
            try:
                d_df = load_ohlcv(d_ticker, d_start, d_end)
            except Exception as e:
                st.error(f"OHLCV 로딩 실패: {e}")
                d_df = pd.DataFrame()

            if d_df.empty:
                st.error("OHLCV 데이터가 비어 있습니다.")
            else:
                # T-1 찾기
                d_dt = pd.Timestamp(diag_trigger_date)
                prior = d_df.index[d_df.index < d_dt]
                if len(prior) == 0:
                    st.error("예고일 이전 거래 데이터가 없습니다.")
                else:
                    t1_idx = len(prior) - 1
                    t1_date = prior[-1]
                    t1_price = int(d_df["종가"].iloc[t1_idx])

                    st.markdown(f"#### 📍 {d_name} ({d_ticker})")
                    st.markdown(
                        f"예고/지정일(T): **{diag_trigger_date}** | "
                        f"T-1: **{t1_date.strftime('%Y-%m-%d')}** | "
                        f"T-1 종가: **{t1_price:,}원**"
                    )

                    # ─── 기간별 상승률 ───
                    st.markdown("---")
                    st.markdown("#### 📊 기간별 상승률")
                    periods = [3, 5, 10, 15, 20, 40, 60]
                    rate_rows = []
                    for p in periods:
                        if t1_idx >= p:
                            past_p = int(d_df["종가"].iloc[t1_idx - p])
                            ratio = t1_price / past_p if past_p else 0
                            pct = (ratio - 1) * 100
                            rate_rows.append({
                                "기간": f"{p}일 전",
                                "과거 종가": f"{past_p:,}원",
                                "T-1 종가": f"{t1_price:,}원",
                                "상승률": f"+{pct:.1f}% (×{ratio:.2f})",
                                "_ratio": ratio,
                                "_p": p,
                            })
                    if rate_rows:
                        st.dataframe(
                            pd.DataFrame(rate_rows).drop(columns=["_ratio", "_p"]),
                            use_container_width=True, hide_index=True)

                    # 15일 최고가 여부
                    if t1_idx >= 14:
                        max15 = int(d_df["종가"].iloc[t1_idx - 14: t1_idx + 1].max())
                        is_max15 = t1_price >= max15
                        st.markdown(
                            f"📈 **15일 최고가**: {max15:,}원 "
                            f"{'✅ 달성' if is_max15 else '❌ 미달성'}"
                        )

                    # ─── KRX 유형별 기준 전수 비교 ───
                    st.markdown("---")
                    st.markdown("#### 🎯 KRX 유형별 기준 전수 비교")
                    st.caption("※ KRX는 기준을 비공개. 아래는 역추정/추정값입니다.")

                    ratio_map = {r["_p"]: r["_ratio"] for r in rate_rows}

                    def chk(p, mult):
                        r = ratio_map.get(p)
                        if r is None:
                            return "—", "—"
                        ok = r >= mult
                        return f"{'✅' if ok else '❌'} {r*100-100:.1f}%", ok

                    types = [
                        ("단기상승&불건전(역추정)", 5, 1.45, 15, 1.75),
                        ("현재 앱 기본값",          5, 1.60, 20, 2.00),
                        ("중장기급등(역추정)",       5, 1.60, 15, 2.00),
                        ("장기급등(추정)",           20, 1.60, 40, 2.00),
                        ("초장기급등(추정)",         20, 1.60, 60, 2.00),
                    ]
                    type_rows = []
                    for label, p1, m1, p2, m2 in types:
                        r1_str, ok1 = chk(p1, m1)
                        r2_str, ok2 = chk(p2, m2)
                        both = (ok1 is True and ok2 is True)
                        type_rows.append({
                            "유형": label,
                            f"조건1 ({p1}일×{m1})": r1_str,
                            f"조건2 ({p2}일×{m2})": r2_str,
                            "종합": "🔴 충족" if both else "❌",
                        })
                    st.dataframe(pd.DataFrame(type_rows),
                                 use_container_width=True, hide_index=True)

                    # ─── 거래량·변동성 진단 ───
                    st.markdown("---")
                    st.markdown("#### 📦 거래량·변동성 진단 (불건전 요건 힌트)")

                    if t1_idx >= 40:
                        avg2_vol = d_df["거래량"].iloc[t1_idx - 1: t1_idx + 1].mean()
                        avg40_vol = d_df["거래량"].iloc[t1_idx - 39: t1_idx + 1].mean()
                        vol_ratio = avg2_vol / avg40_vol if avg40_vol > 0 else 0

                        prev_close = d_df["종가"].shift(1)
                        daily_vola = (d_df["고가"] - d_df["저가"]) / prev_close
                        avg2_vola = daily_vola.iloc[t1_idx - 1: t1_idx + 1].mean()
                        avg40_vola = daily_vola.iloc[t1_idx - 39: t1_idx + 1].mean()
                        vola_ratio = avg2_vola / avg40_vola if avg40_vola > 0 else 0

                        avg5_vol = d_df["거래량"].iloc[t1_idx - 4: t1_idx + 1].mean()
                        vol5_ratio = avg5_vol / avg40_vol if avg40_vol > 0 else 0

                        vd_rows = [
                            {"지표": "최근 2일 거래량 / 40일 평균",
                             "값": f"×{vol_ratio:.2f}",
                             "기준": "×6.0 이상(단기과열)",
                             "해당": "✅" if vol_ratio >= 6.0 else "❌"},
                            {"지표": "최근 5일 거래량 / 40일 평균",
                             "값": f"×{vol5_ratio:.2f}",
                             "기준": "×3~5 수준이면 거래 급증",
                             "해당": "🔥" if vol5_ratio >= 3.0 else "—"},
                            {"지표": "최근 2일 변동성 / 40일 평균",
                             "값": f"×{vola_ratio:.2f}",
                             "기준": "×1.5 이상(단기과열)",
                             "해당": "✅" if vola_ratio >= 1.5 else "❌"},
                        ]
                        st.dataframe(pd.DataFrame(vd_rows),
                                     use_container_width=True, hide_index=True)
                    else:
                        st.info("데이터 부족(40거래일 미만)으로 거래량·변동성 진단 불가")

                    # ─── 원시 OHLCV ───
                    with st.expander("📋 T-1 기준 최근 30일 원시 OHLCV 데이터"):
                        window_start = max(0, t1_idx - 29)
                        display_df = d_df.iloc[window_start: t1_idx + 1].copy()
                        display_df.index = display_df.index.strftime("%Y-%m-%d")
                        st.dataframe(display_df[["종가", "거래량"]],
                                     use_container_width=True)


st.markdown("---")
st.caption("📌 공개 주가 데이터 기반 자체 계산입니다. "
           "현재 지정종목 리스트는 네이버 금융에서 실시간 반영됩니다. "
           "최종 판단은 한국거래소 공식 공시로 확인하세요.")
