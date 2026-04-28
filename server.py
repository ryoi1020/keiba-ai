"""
CC 競馬予想 AI - サーバー
起動方法: python3 server.py
"""

import json
import os
import re
import sqlite3
import urllib.request
import urllib.error
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ========== 設定 ==========
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
PORT = int(os.environ.get("PORT", 11000))
DB_PATH = os.path.join(os.path.dirname(__file__), "predictions.db")

VENUE_NAMES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "中山",
    "06": "東京", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}
# ==========================


# ===== DB初期化 =====
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        race_date TEXT NOT NULL,
        venue TEXT NOT NULL,
        race_num TEXT NOT NULL,
        honmei_num INTEGER,
        honmei_name TEXT,
        niban_num INTEGER,
        niban_name TEXT,
        sanban_num INTEGER,
        sanban_name TEXT,
        grade_tan TEXT,
        grade_baren TEXT,
        grade_sanren TEXT,
        budget INTEGER,
        raw_json TEXT
    );
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_date TEXT NOT NULL,
        venue TEXT NOT NULL,
        race_num TEXT NOT NULL,
        first INTEGER,
        second INTEGER,
        third INTEGER,
        recorded_at TEXT NOT NULL,
        UNIQUE(race_date, venue, race_num)
    );
    CREATE TABLE IF NOT EXISTS bet_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id INTEGER,
        kenshu TEXT,
        kumiawase TEXT,
        kin INTEGER,
        odds REAL,
        hit INTEGER DEFAULT 0,
        payout INTEGER DEFAULT 0,
        FOREIGN KEY(prediction_id) REFERENCES predictions(id)
    );
    """)
    con.commit()
    con.close()


def save_prediction(race_date, venue, race_num, pred_json):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    honmei = pred_json.get("honmei", [])
    kaikata = pred_json.get("kaikata", [])
    budget = sum(k.get("kin", 0) for k in kaikata)

    h = honmei[0] if len(honmei) > 0 else {}
    o = honmei[1] if len(honmei) > 1 else {}
    s = honmei[2] if len(honmei) > 2 else {}
    grade = pred_json.get("grade", {})

    cur.execute("""
        INSERT INTO predictions
        (created_at, race_date, venue, race_num,
         honmei_num, honmei_name, niban_num, niban_name, sanban_num, sanban_name,
         grade_tan, grade_baren, grade_sanren, budget, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now().isoformat(),
        race_date, venue, race_num,
        h.get("num"), h.get("name"),
        o.get("num"), o.get("name"),
        s.get("num"), s.get("name"),
        grade.get("tan"), grade.get("baren"), grade.get("sanren"),
        budget,
        json.dumps(pred_json, ensure_ascii=False)
    ))
    pred_id = cur.lastrowid

    for k in kaikata:
        cur.execute("""
            INSERT INTO bet_results (prediction_id, kenshu, kumiawase, kin, odds)
            VALUES (?,?,?,?,?)
        """, (pred_id, k.get("kenshu"), k.get("kumiawase"), k.get("kin", 0), float(k.get("odds", 0) or 0)))

    con.commit()
    con.close()
    return pred_id


def save_result(race_date, venue, race_num, first, second, third):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO results (race_date, venue, race_num, first, second, third, recorded_at)
        VALUES (?,?,?,?,?,?,?)
    """, (race_date, venue, race_num, first, second, third, datetime.now().isoformat()))

    cur.execute("""
        SELECT br.id, br.prediction_id, br.kenshu, br.kumiawase, br.kin, br.odds
        FROM bet_results br
        JOIN predictions p ON br.prediction_id = p.id
        WHERE p.race_date=? AND p.venue=? AND p.race_num=?
    """, (race_date, venue, race_num))
    bets = cur.fetchall()

    for bet_id, pred_id, kenshu, kumiawase, kin, odds in bets:
        hit = check_hit(kenshu, kumiawase, first, second, third)
        payout = int(kin * odds) if hit else 0
        cur.execute("UPDATE bet_results SET hit=?, payout=? WHERE id=?", (1 if hit else 0, payout, bet_id))

    con.commit()
    con.close()


def check_hit(kenshu, kumiawase, first, second, third):
    """券種別の的中判定（馬番ベース）"""
    nums = [int(x) for x in re.findall(r'\d+', kumiawase or "")]
    if not nums:
        return False
    top3 = {first, second, third}

    if "3連単" in kenshu:
        return len(nums) >= 3 and nums[0] == first and nums[1] == second and nums[2] == third
    if "3連複" in kenshu:
        return len(nums) >= 3 and set(nums[:3]) == top3
    if "馬単" in kenshu:
        return len(nums) >= 2 and nums[0] == first and nums[1] == second
    if "馬連" in kenshu:
        return len(nums) >= 2 and set(nums[:2]) == {first, second}
    if "ワイド" in kenshu:
        # 1〜3着のうち2頭が選んだ組み合わせに含まれていれば的中
        return len(nums) >= 2 and len(set(nums[:2]) & top3) == 2
    if "複勝" in kenshu:
        return len(nums) >= 1 and nums[0] in top3
    if "単勝" in kenshu:
        return len(nums) >= 1 and nums[0] == first
    return False


def get_stats():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("""
        SELECT
            substr(p.race_date, 1, 7) as month,
            COUNT(DISTINCT p.id) as races,
            SUM(br.kin) as total_bet,
            SUM(br.payout) as total_payout,
            SUM(br.hit) as hits,
            COUNT(br.id) as bets
        FROM predictions p
        JOIN bet_results br ON br.prediction_id = p.id
        GROUP BY month
        ORDER BY month DESC
        LIMIT 12
    """)
    monthly = [{"month": r[0], "races": r[1], "bet": r[2] or 0,
                "payout": r[3] or 0, "hits": r[4] or 0, "bets": r[5] or 0} for r in cur.fetchall()]

    cur.execute("""
        SELECT kenshu,
            COUNT(*) as bets,
            SUM(hit) as hits,
            SUM(kin) as total_bet,
            SUM(payout) as total_payout
        FROM bet_results
        GROUP BY kenshu
        ORDER BY total_bet DESC
    """)
    by_kenshu = [{"kenshu": r[0], "bets": r[1], "hits": r[2] or 0,
                  "bet": r[3] or 0, "payout": r[4] or 0} for r in cur.fetchall()]

    cur.execute("""
        SELECT p.id, p.race_date, p.venue, p.race_num,
               p.honmei_name, p.grade_tan,
               SUM(br.kin) as bet,
               SUM(br.payout) as payout,
               SUM(br.hit) as hits,
               r.first, r.second, r.third
        FROM predictions p
        LEFT JOIN bet_results br ON br.prediction_id = p.id
        LEFT JOIN results r ON r.race_date=p.race_date AND r.venue=p.venue AND r.race_num=p.race_num
        GROUP BY p.id
        ORDER BY p.created_at DESC
        LIMIT 30
    """)
    recent = [{"id": r[0], "date": r[1], "venue": r[2], "race": r[3],
               "honmei": r[4], "grade": r[5], "bet": r[6] or 0, "payout": r[7] or 0,
               "hits": r[8] or 0, "result": f"{r[9]}-{r[10]}-{r[11]}" if r[9] else None}
              for r in cur.fetchall()]

    cur.execute("""
        SELECT COUNT(DISTINCT p.id), SUM(br.kin), SUM(br.payout), SUM(br.hit), COUNT(br.id)
        FROM predictions p JOIN bet_results br ON br.prediction_id=p.id
    """)
    row = cur.fetchone()
    total = {"races": row[0] or 0, "bet": row[1] or 0, "payout": row[2] or 0,
             "hits": row[3] or 0, "bets": row[4] or 0}

    con.close()
    return {"monthly": monthly, "by_kenshu": by_kenshu, "recent": recent, "total": total}


# ===== スクレイピング =====
def fetch_html(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ja,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as res:
        raw = res.read()
    # netkeibaはEUC-JP/UTF-8混在のためbestエフォートでデコード
    for enc in ("utf-8", "euc-jp", "cp932"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def strip_html(html):
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL)
    html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.replace('&nbsp;', ' ').strip()


def build_race_id(race_date_str: str, jcd: str, rno: str) -> str:
    """race_date(YYYY-MM-DD) + 場コード + R番号 から netkeiba race_id を構築

    仕様: YYYYMMJJRR（年4桁 + 月2桁 + 場コード2桁 + R番号2桁）
    """
    d = race_date_str.replace("-", "")
    if len(d) >= 6:
        yyyy = d[:4]
        mm = d[4:6]
    else:
        yyyy = datetime.now().strftime("%Y")
        mm = datetime.now().strftime("%m")
    rr = str(rno).zfill(2)
    jj = str(jcd).zfill(2)
    return f"{yyyy}{mm}{jj}{rr}"


def fetch_all_pages(rno, jcd, race_date, race_id_override: str = ""):
    """netkeibaから出走表ページを取得（並列）"""
    import concurrent.futures
    race_id = race_id_override.strip() or build_race_id(race_date, jcd, rno)
    urls = {
        "shutuba": f"https://race.netkeiba.com/race/shutuba.html?race_id={race_id}",
        "odds":    f"https://race.netkeiba.com/odds/index.html?race_id={race_id}",
    }

    def fetch_one(item):
        key, url = item
        try:
            result = strip_html(fetch_html(url))
            print(f"  取得OK: {key} ({len(result)}文字)")
            return key, result
        except Exception as e:
            print(f"  取得失敗: {key} - {e}")
            return key, ""

    pages = {"race_id": race_id}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        for key, text in executor.map(fetch_one, urls.items()):
            pages[key] = text
    return pages


# ===== Claude API =====
def claude_api(prompt, max_tokens=1000):
    payload = json.dumps({
        "model": "claude-sonnet-4-5",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}]
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01"
        }
    )
    with urllib.request.urlopen(req, timeout=60) as res:
        data = json.loads(res.read().decode("utf-8"))
    return "".join(b["text"] for b in data["content"] if b["type"] == "text").strip()


def call_claude_parse(pages):
    """netkeibaのHTMLテキストから出走表を抽出"""
    prompt = f"""以下はnetkeibaの競馬出走表ページから取得したテキストです。
全出走馬の情報を抽出し、以下の形式で出走表テキストを作成してください。

出力形式（1馬1行）:
[馬番]番 [馬名] [性齢] 斤量[XX.X] 騎手[騎手名] 厩舎[厩舎名] 馬体重[XXX] オッズ[X.X] 人気[X番人気] 前走[着順]着

情報が不明な項目は省略してOKです。
出走馬の頭数が分からない場合は記載されている分だけ出力してください。
出走表テキストのみ出力し、説明文は不要です。

=== 出走表 ===
{pages.get('shutuba','')[:9000]}

=== オッズ・人気 ===
{pages.get('odds','')[:3000]}"""
    return claude_api(prompt, 1500)


def call_claude_predict(shutsuba_text, venue, weather):
    prompt = f"""あなたは競馬予想AIです。以下の出走表を nakuratoru.github.io 準拠の7ステップ予想ロジックで分析し、JSONのみで返答してください。

会場: {venue}
天候・馬場状態: {weather or '不明'}

出走表:
{shutsuba_text}

═══════════════════════════════════════════
【ステップ1：まず全馬の「負ける理由」を考える（敗因先行）】
═══════════════════════════════════════════
全出走馬について、なぜ負けるかを先に列挙する。これが予想の出発点。
- 人気馬でも負ける具体的なリスクを列挙する：斤量増・距離不安・騎手乗り替わり・調教不足・馬体重大幅増減・前走からの間隔・トラックバイアス
- 確証バイアスへの警告：「前走1着だから◎」「1番人気だから◎」は絶対禁止
- 穴馬の激走パターンを積極的に探す：先行力・コース替わり・距離短縮/延長適性・斤量減・騎手強化・前走不利あり

═══════════════════════════════════════════
【ステップ2：各馬に勝率(p1)を割り当てる】
═══════════════════════════════════════════
- 全出走馬の p1（%）の合計が必ず 100% になるよう調整する
- 評価軸：父馬・騎手・斤量・クラス・性齢・コース適性を総合評価
- 人気（オッズ）は最後に確認するだけ。絶対にオッズ順に選ばない
- 1頭が30%超になることは稀。極端に偏らせない

═══════════════════════════════════════════
【ステップ3：◎○▲を決定】
═══════════════════════════════════════════
- 最高勝率=◎（本命）、2番手=○（対抗）、3番手=▲
- ただし上位馬のスコアが3%以内で拮抗している場合は穴馬（人気薄）を優先して◎に置く
- ◎を選んだら honmeiPct には◎のp1（%）を必ず数値文字列で記載（例 "32.5"）

═══════════════════════════════════════════
【ステップ4：グレード判定】
═══════════════════════════════════════════
期待値（p1×想定オッズ÷100）で各券種にグレードを付ける：
- 期待値4倍以上 = S
- 期待値2〜4倍 = A
- 期待値2倍未満 = B
単勝(tan)・馬連(baren)・3連単(sanren)それぞれにグレードを付与する。

═══════════════════════════════════════════
【ステップ5：パターン判定】
═══════════════════════════════════════════
- パターンA（◎が1番人気）：少額・絞る。単勝＋3連単1点で合計300〜400円。race_pattern:"A"
- パターンB（◎が2番人気以下）：穴狙い・積極買い。合計600〜1000円。race_pattern:"B"
- パターンC（混戦：上位3頭がp1±3%以内）：3連複ボックスのみ200円。race_pattern:"C"
race_pattern には必ず "A" "B" "C" のいずれかを記載すること。

═══════════════════════════════════════════
【ステップ6：敗因シナリオを6つ生成】
═══════════════════════════════════════════
本命（◎）が負ける具体的なリスクを6パターン、必ず生成する：
1. 展開・ペースが想定と異なるリスク（先行争い・前残り/差し決着）
2. 斤量・距離・コース適性の不安
3. 調教内容・前走パターン・状態面（連戦・休み明け・馬体重増減）
4. 馬場状態（良/稍重/重/不良）が◎に向かない場合のリスク
5. 先行争いの兼ね合い（同型馬・隊列）
6. 確証バイアスの警告：人気先行で過信していないか自問
各シナリオには馬名・馬番を必ず含めること。

═══════════════════════════════════════════
【ステップ7：買い目を決定】
═══════════════════════════════════════════
基本構成：単勝◎、3連単◎→○→▲、3連複◎○▲。
パターン別：

▼パターンA（合計300〜400円）
- 単勝 ◎ 200円
- 3連単 ◎→○→▲ 1点 100〜200円

▼パターンB（合計600〜1000円）
- 単勝 ◎ 200円
- 馬連 ◎-○ 200円
- 3連複 ◎○▲ 200円
- 3連単 ◎→○→▲ 1点 100〜200円
- 必要なら3連単もう1点（例 ◎→▲→○）

▼パターンC（合計200円）
- 3連複 ◎○▲ボックス 200円のみ

【見送り判定】
miken:true は「本命勝率（p1）20%未満 かつ 単勝グレードB」を両方満たす場合のみ。それ以外は miken:false。

期待値ベースで金額を算出し、自信のない組み合わせは絶対に載せない（点数を増やして薄めない）。
各買い目の konkyo には期待値・自信度・具体的な根拠を記載する。

═══════════════════════════════════════════
【出力形式】以下のJSONのみで返答（コードブロック不要）
═══════════════════════════════════════════
{{
  "raceName": "レース名",
  "venue": "会場名",
  "raceNum": "R番号",
  "raceType": "コース・距離（例:芝1600m）",
  "weather": "天候・馬場（例:晴れ・良）",
  "honmeiPct": "本命勝率（◎のp1。例 32.5）※必須",
  "honmei": [
    {{"mark":"◎","num":5,"name":"馬名","sex":"牡4","weight":"57.0","jockey":"騎手名","pct":"32.5","pop":"1番人気"}},
    {{"mark":"○","num":8,"name":"馬名","sex":"牝4","weight":"55.0","jockey":"騎手名","pct":"21.0","pop":"3番人気"}},
    {{"mark":"▲","num":2,"name":"馬名","sex":"牡5","weight":"57.0","jockey":"騎手名","pct":"14.5","pop":"2番人気"}}
  ],
  "shutsuba": [
    {{"num":1,"name":"馬名","sex":"牡4","weight":"57.0","jockey":"騎手名","stable":"厩舎","odds":5.2,"pop":3,"prevRank":2,"horseWeight":480,"p1":12.5}}
  ],
  "grade": {{
    "tan": "単勝グレード（例:S32）",
    "baren": "馬連グレード（例:A6.5）",
    "sanren": "3連単グレード（例:S15.4）"
  }},
  "haiin": [
    "敗因シナリオ1（展開・ペース。馬名・馬番必須）",
    "敗因シナリオ2（斤量・距離・コース適性）",
    "敗因シナリオ3（調教・前走・状態面）",
    "敗因シナリオ4（馬場状態の影響）",
    "敗因シナリオ5（先行争いの兼ね合い）",
    "敗因シナリオ6（確証バイアスの警告）"
  ],
  "race_pattern": "A",
  "miken": false,
  "budget": "予算（例:800円）",
  "kaikata": [
    {{"kenshu":"単勝","kumiawase":"5","odds":"3.0","kuchi":1,"kin":200,"konkyo":"期待値X.X倍／自信度高／単勝◎"}},
    {{"kenshu":"3連単","kumiawase":"5→8→2","odds":"45.0","kuchi":1,"kin":200,"konkyo":"期待値X.X倍／3連単◎→○→▲"}},
    {{"kenshu":"3連複","kumiawase":"2-5-8","odds":"15.0","kuchi":1,"kin":200,"konkyo":"期待値X.X倍／3連複◎○▲"}}
  ]
}}

shutsuba配列には全出走馬を含め、各馬に p1 フィールド（数値%）を必ず付与すること。p1の合計は100%。
honmeiPct は必ず数値文字列（例 "32.5"）で記載すること。"""
    raw = claude_api(prompt, 3500)
    clean = re.sub(r"```json|```", "", raw).strip()
    return json.loads(clean)


# ===== HTTPサーバー =====
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [{self.address_string()}] {fmt % args}")

    def send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self.send_json(200, {"status": "ok", "api_key_set": bool(API_KEY)})
            return

        if parsed.path == "/stats":
            self.send_json(200, get_stats())
            return

        if parsed.path in ("/", "/index.html"):
            html_path = os.path.join(os.path.dirname(__file__), "index.html")
            if os.path.exists(html_path):
                with open(html_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json(404, {"error": "index.html が見つかりません"})
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        # /scrape : netkeibaから出走表を取得
        if parsed.path == "/scrape":
            try:
                data = json.loads(body)
                rno = data.get("rno", "")
                jcd = data.get("jcd", "")
                race_date = data.get("race_date", "")
                race_id   = data.get("race_id", "")

                if race_id or (rno and jcd and race_date):
                    print(f"  netkeiba取得中: race_id={race_id} jcd={jcd} rno={rno} date={race_date}")
                    pages = fetch_all_pages(rno, jcd, race_date, race_id_override=race_id)
                else:
                    url = data.get("url", "").strip()
                    if not url:
                        self.send_json(400, {"error": "race_id または rno/jcd/race_date が必要です"})
                        return
                    html = fetch_html(url)
                    pages = {"shutuba": strip_html(html)}

                shutsuba_text = call_claude_parse(pages)
                print(f"  出走表抽出完了:\n{shutsuba_text}")
                self.send_json(200, {"shutsuba_text": shutsuba_text, "race_id": pages.get("race_id", ""), "parsed": True})

            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_json(500, {"error": str(e)})
            return

        # /predict : 予想生成
        if parsed.path == "/predict":
            try:
                data = json.loads(body)
                shutsuba_text = data.get("shutsuba_text", "").strip()
                venue   = data.get("venue", "")
                weather = data.get("weather", "")
                race_date = data.get("race_date", date.today().isoformat())
                race_num  = data.get("race_num", "")

                if not shutsuba_text:
                    self.send_json(400, {"error": "出走表テキストが空です"})
                    return

                print(f"  AI予想生成中... (venue={venue})")
                result = call_claude_predict(shutsuba_text, venue, weather)

                pred_id = save_prediction(race_date, venue, race_num, result)
                result["prediction_id"] = pred_id
                print(f"  予想保存完了 id={pred_id}")

                self.send_json(200, result)

            except json.JSONDecodeError as e:
                self.send_json(500, {"error": f"AIの返答をパースできませんでした: {e}"})
            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_json(500, {"error": str(e)})
            return

        # /result : レース結果を登録
        if parsed.path == "/result":
            try:
                data = json.loads(body)
                race_date = data.get("race_date", "")
                venue     = data.get("venue", "")
                race_num  = data.get("race_num", "")
                first  = data.get("first")
                second = data.get("second")
                third  = data.get("third")

                if not (first and second and third):
                    self.send_json(400, {"error": "1〜3着の馬番を指定してください"})
                    return

                save_result(race_date, venue, race_num, int(first), int(second), int(third))
                self.send_json(200, {"ok": True, "first": first, "second": second, "third": third})

            except Exception as e:
                import traceback; traceback.print_exc()
                self.send_json(500, {"error": str(e)})
            return

        self.send_json(404, {"error": "Not found"})


def main():
    init_db()
    if not API_KEY:
        print("=" * 55)
        print("  警告: ANTHROPIC_API_KEY が設定されていません")
        print("=" * 55)

    print(f"\nCC 競馬予想 AI サーバー起動中...")
    print(f"  PORT: {PORT}")
    print(f"  DB: {DB_PATH}")
    print(f"  停止するには Ctrl+C を押してください\n")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを停止しました")


if __name__ == "__main__":
    main()
