#!/usr/bin/env python3
"""ask_jev — ask TypeSafe's Jev a plain-English question, get a simple typed answer.

Jev does not write prose. It answers three shapes of question, so this script turns
a natural sentence into one of them and turns the typed answer back into one line:

  yes/no      "Should we add a CMS?"                         -> Noul   (probability of yes)
  pick one    "Astro, Next.js, or plain HTML?"                -> Choice (winner + distribution)
  how much    "How risky is migrating this week?"             -> Score  (position on described levels)

Usage:
  python3 ask_jev.py "Should a one-person startup add a CMS?"
  python3 ask_jev.py "Which fits best: Astro, Next.js, or plain HTML?" --context "one-person team, static marketing site"
  python3 ask_jev.py "How risky is migrating this week?" --context-file notes.md
  python3 ask_jev.py "Is the copy too salesy?" "Is the copy too long?" --context-file page.txt   # several questions, one call
  python3 ask_jev.py "Best channel?" --options email,linkedin,cold-call
  python3 ask_jev.py "How ready is this for launch?" --levels "Not started,Prototype,Works but rough,Polished,Shipped"
  python3 ask_jev.py "..." --json          # full answer for programmatic use
  python3 ask_jev.py "..." --dry-run       # show the typed question, no API call

Context is everything Jev knows: it sees --context / --context-file / stdin, nothing else.
Every call is appended to runs/ask_log.jsonl.
Needs TYPESAFE_API_KEY in the environment or in a .env file next to this script.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
HERE = Path(__file__).resolve().parent
LOG = HERE / "runs" / "ask_log.jsonl"

YES_ABOVE, NO_BELOW = 0.7, 0.3       # example thresholds for reading a Noul; tune per decision
CLOSE_CALL_CONFIDENCE = 0.5


class JevError(Exception):
    """Anything the caller should show to the user instead of a traceback."""


# ---------------------------------------------------------------- request ----

def load_api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        env = HERE / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("TYPESAFE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
    if not key:
        raise JevError("TYPESAFE_API_KEY not set. Get one at https://console.typesafe.ai/keys "
                       "and export it or put TYPESAFE_API_KEY=... in JEV/.env")
    return key


def call_jev(state, questions: dict, api_key: str, retries: int = 4) -> dict:
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    req = urllib.request.Request(
        API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    delay = 1.0
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code in (429, 529) and attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise JevError(f"TypeSafe API error {e.code}: {detail}")
    raise RuntimeError("unreachable")


# -------------------------------------------------- natural language -> typed ----
# Three parsers (English, Chinese, Thai) with the same job: decide whether a sentence
# is a yes/no, pick-one, or how-much question and pull out the options if any.
# Score levels come from levels.json in the asker's language. worker/index.js mirrors
# this section; keep the two in step (node worker/parity.test.mjs checks).

LEVELS = json.loads((HERE / "levels.json").read_text())

CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
THAI_RE = re.compile(r"[฀-๿]")
# Characters that differ between the two scripts; whichever set a question uses more wins.
TRAD_CHARS = set("們這個說為會應該嗎還選風險難緊價麼於與國時經學問題發現務來對開關點動長車電馬鳥東門買賣讓認識語頁網後種線產測環紅綠烏龍幣費設計態導過進遠運連業專傳轉華達體臺灣廣場際標準積極權觀歡圖書數樓機檢驗資質貴賺錢銀鐵錯鍵願顯頭類實寫將尋層屬島師帶幫廠復從愛戰擇護擴斷舊曆條樣歷氣決況淨濟無熱營獨獲畫當療蓋處號術裝見規視覺許話誰課調談請論講謝證議讀變負財貨責賽軟較輕輸農遊適郵鄉醫釋裡間閱陽隨雙雜離靈領額顧飛飯館髮鬥魚麗齊齒龜兩親")
SIMP_CHARS = set("们这个说为会应该吗还选风险难紧价么于与国时经学问题发现务来对开关点动长车电马鸟东门买卖让认识语页网后种线产测环红绿乌龙币费设计态导过进远运连业专传转华达体台湾广场际标准积极权观欢图书数楼机检验资质贵赚钱银铁错键愿显头类实写将寻层属岛师带帮厂复从爱战择护扩断旧历条样历气决况净济无热营独获画当疗盖处号术装见规视觉许话谁课调谈请论讲谢证议读变负财货责赛软较轻输农游适邮乡医释里间阅阳随双杂离灵领额顾飞饭馆发斗鱼丽齐齿龟两亲")


def detect_lang(q: str) -> str:
    if THAI_RE.search(q):
        return "th"
    if CJK_RE.search(q):
        trad = sum(c in TRAD_CHARS for c in q)
        simp = sum(c in SIMP_CHARS for c in q)
        return "zh-Hant" if trad > simp else "zh-Hans"
    return "en"


def levels_for(key: str, lang: str) -> list[str]:
    table = LEVELS.get(lang, LEVELS["en"])
    return table.get(key, table["default"])


def first_key(text: str, table: list[tuple[str, str]]) -> str:
    """The level-set key whose keyword pattern appears in the text, else 'default'."""
    for key, pattern in table:
        if re.search(pattern, text, re.I):
            return key
    return "default"


def strip_lead(seg: str, prefix_end: int, leads_re: re.Pattern, sep_re: re.Pattern) -> str:
    """Drop the preamble before the first option ("Should we use A or B" -> "A or B").

    Keeps it when the second option starts with the same lead text ("先做A还是先做B"):
    a shared prefix is part of the options, not the preamble.
    """
    leads = list(leads_re.finditer(seg[:prefix_end]))
    if not leads:
        return seg
    lead_text = seg[:leads[-1].end()]
    rest = sep_re.split(seg[prefix_end:], 1)
    second = rest[1].strip() if len(rest) > 1 else ""
    if second.startswith(lead_text.strip()):
        return seg
    return seg[leads[-1].end():]


def clean_options(parts: list[str], strip_chars: str, max_len: int) -> list[str]:
    opts = [p.strip(strip_chars) for p in parts]
    opts = [o for o in opts if o]
    if not 2 <= len(opts) <= 20 or any(len(o) > max_len for o in opts):
        return []
    return opts


# ---- English -------------------------------------------------------------------

AUX = r"(?:is|are|am|was|were|do|does|did|should|shall|can|could|will|would|has|have|had|must|may|might|need|needs|ought)"
YESNO_RE = re.compile(rf"^\s*{AUX}\b", re.I)
DEGREE_RE = re.compile(r"^\s*how\s+(\w+)", re.I)
WHICH_RE = re.compile(r"^\s*(?:which|what)\b", re.I)
OR_RE = re.compile(r"\s+or\s+", re.I)
EN_LEVEL_KEYS = [
    ("likely", r"^(?:likely|probable|plausible|realistic)$"),
    ("risky", r"^(?:risky|dangerous|safe|unsafe)$"),
    ("hard", r"^(?:hard|difficult|complex|complicated|easy|simple|big|large)$"),
    ("urgent", r"^(?:urgent|important|critical|pressing)$"),
    ("good", r"^(?:good|strong|well|ready|mature|solid|polished|healthy|effective|clear|bad|weak)$"),
    ("valuable", r"^(?:valuable|useful|worthwhile|profitable|much|many)$"),
]


def split_options_en(question: str) -> list[str]:
    """Pull 'A, B, or C' style options out of a sentence. Empty list if none found."""
    body = question.strip().rstrip("?.! ").strip()
    if not OR_RE.search(body) and "," not in body:
        return []

    if ":" in body or ";" in body:
        seg = re.split(r"[:;]", body, 1)[1]
    elif m := re.search(r"\b(?:between|among|out of)\s+(.+)$", body, re.I):
        seg = m.group(1)
    elif WHICH_RE.match(body) and "," in body:
        seg = body.split(",", 1)[1]
    else:
        # "Should we redesign or leave it?" -> "redesign or leave it"
        seg = re.sub(rf"^\s*{AUX}\s+(?:we|i|you|they|it|one|the team)\s+(?:(?:be\s+)?(?:better|best|wiser|smarter|safer)\s+)?(?:to\s+)?",
                     "", body, flags=re.I)
        if WHICH_RE.match(seg):
            # "Which is faster to build with Astro or Next.js" -> text after the last lead verb before the first "or"
            first_or = OR_RE.search(seg)
            if not first_or:
                return []
            prefix = seg[:first_or.start()]
            leads = list(re.finditer(r"\b(?:is|are|be|to|use|using|choose|pick|prefer|with|take|go|do|for)\s+", prefix, re.I))
            if not leads:
                return []
            seg = seg[leads[-1].end():]

    parts = re.split(r"\s*,\s*(?:or|and)\s+|\s+or\s+|\s*,\s*", seg.strip(), flags=re.I)
    parts = [re.sub(r"^(?:rather|instead|just|simply)\s+", "", p.strip(" \"'"), flags=re.I) for p in parts]
    return clean_options(parts, " \"'", 80)


def parse_en(q: str) -> tuple[str | None, list[str], list[str]]:
    """Return (kind, options, levels); kind None means 'not a shape Jev can answer'."""
    if m := DEGREE_RE.match(q):
        return "score", [], levels_for(first_key(m.group(1), EN_LEVEL_KEYS), "en")
    if YESNO_RE.match(q) and not WHICH_RE.match(q):
        found = split_options_en(q)
        return ("choice", found, []) if found else ("noul", [], [])
    if WHICH_RE.match(q):
        found = split_options_en(q)
        return ("choice", found, []) if found else ("choice", [], [])
    return None, [], []


# ---- Chinese (simplified and traditional) ---------------------------------------

ZH_PUNCT = "？?。！!．.，,、 　\t"
ZH_YESNO_TAIL = re.compile(r"(?:吗|嗎)$")
ZH_YESNO_MARK = re.compile(r"是否|是不是|能不能|该不该|該不該|要不要|应不应该|應不應該|会不会|會不會|可不可以|行不行|好不好|"
                           r"值不值得|有没有|有沒有|需不需要|适不适合|適不適合|对不对|對不對|能否|可否|应否|應否|会否|會否|"
                           r"合不合适|合不合適|划不划算|靠不靠谱|靠不靠譜|可行|算不算|是不是该|是不是該")
ZH_YESNO_LEAD = re.compile(r"^(?:应该|應該|该|該|要|能|可以|会|會|是|值得|适合|適合|需要|建议|建議|有必要|现在|現在|我们|我們|我)")
ZH_DEGREE = re.compile(r"有多|多大|多高|多低|多难|多難|多容易|多可能|多重要|多紧急|多緊急|多危险|多危險|多好|多强|多強|"
                       r"多成熟|多复杂|多複雜|多值得|多有用|多划算|多严重|多嚴重|多久|多少|程度|几成|幾成")
ZH_WHICH = re.compile(r"哪个|哪個|哪一个|哪一個|哪种|哪種|哪项|哪項|哪些|哪家|哪款|哪条|哪條|哪套|哪门|哪門|哪位|哪边|哪邊")
ZH_CHOICE_SEP = re.compile(r"还是|還是|或者|抑或|或(?![许許者])|、|，|,")
ZH_CHOICE_MARK = re.compile(r"还是|還是|或者|抑或")
ZH_OPTION_TAIL = re.compile(r"(?:呢|吧|比较好|比較好|比较合适|比較合適|更合适|更合適|更好|更划算|更值得|最好|好|合适|合適|划算|"
                            r"更适合|更適合|适合|適合|比较|比較|更)+$")
ZH_LEADS = re.compile(r"(?:用|選|选|做|买|買|去|要|是|该|該|应该|應該|建议|建議|适合|適合|采用|採用|使用|考虑|考慮|先|优先|優先|"
                      r"学|學|投|加|换|換|改用|改成|改为|改為|叫|念|读|讀|吃|穿|坐|开|開|租|住|选择|選擇|挑|定|走|上|搞|写|寫|推|"
                      r"上线|上線|部署到|迁移到|遷移到|迁到|遷到|升级到|升級到|从|從|在|把|将|將|跟|和|与|與|留在|留|去|找|请|請|"
                      r"雇|僱|聘|发|發|寄|放|放在|搬到|搬去|投资|投資|买入|買入|卖|賣|读|讀|报|報|考|申请|申請|办|辦)")
ZH_LEVEL_KEYS = [
    ("likely", r"可能|把握|概率|機率|机率|几率|幾率|成功"),
    ("risky", r"风险|風險|危险|危險|安全"),
    ("hard", r"难|難|容易|复杂|複雜|简单|簡單|工作量|费力|費力|费劲|費勁|麻烦|麻煩"),
    ("urgent", r"紧急|緊急|重要|急|优先|優先"),
    ("good", r"好|强|強|成熟|完善|健康|清楚|清晰|准备|準備|靠谱|靠譜|质量|質量|品质|品質|满意|滿意"),
    ("valuable", r"值得|有用|价值|價值|划算|收益|回报|回報|意义|意義"),
]


def split_options_zh(body: str) -> list[str]:
    body = ZH_OPTION_TAIL.sub("", body.strip(ZH_PUNCT)).strip(ZH_PUNCT)
    if re.search(r"[：:；;]", body):
        seg = re.split(r"[：:；;]", body, 1)[1]
    else:
        seg = body
        first_sep = ZH_CHOICE_SEP.search(seg)
        if first_sep:
            stripped = strip_lead(seg, first_sep.start(), ZH_LEADS, ZH_CHOICE_SEP)
            if stripped == seg and ZH_WHICH.search(seg[:first_sep.start()]):
                stripped = seg[ZH_WHICH.search(seg).end():]
            seg = stripped
    seg = ZH_OPTION_TAIL.sub("", seg.strip(ZH_PUNCT))
    parts = [re.sub(r"^(?:是|用|选|選|去|要|做)", "", p) if len(p) > 2 else p for p in ZH_CHOICE_SEP.split(seg)]
    return clean_options(parts, ZH_PUNCT + "\"'“”‘’《》「」『』（）()", 40)


def parse_zh(q: str, lang: str) -> tuple[str | None, list[str], list[str]]:
    body = q.strip().rstrip(ZH_PUNCT)
    if ZH_YESNO_TAIL.search(body):
        return "noul", [], []
    if ZH_DEGREE.search(body):
        return "score", [], levels_for(first_key(body, ZH_LEVEL_KEYS), lang)
    if ZH_CHOICE_MARK.search(body) or ZH_WHICH.search(body):
        return "choice", split_options_zh(body), []
    if ZH_YESNO_MARK.search(body) or ZH_YESNO_LEAD.match(body):
        return "noul", [], []
    return None, [], []


# ---- Thai ------------------------------------------------------------------------

TH_PUNCT = "？?。！!．. ,，\t"
TH_PARTICLES = re.compile(r"(?:\s*(?:ครับ|คะ|ค่ะ|นะ|น้า|เหรอ|หรอ|ล่ะ|หละ|จ๊ะ|จ้ะ|ฮะ|ครับผม|นะครับ|นะคะ))+$")
TH_NEG_TAIL = re.compile(r"(?:หรือไม่|หรือเปล่า|หรือยัง|รึเปล่า|รึยัง|รึไม่|ใช่หรือไม่|หรือ)$")
TH_YESNO_TAIL = re.compile(r"(?:ไหม|มั้ย|มั๊ย|ใช่ไหม|ใช่มั้ย|ดีไหม|ดีมั้ย|ได้ไหม|ได้มั้ย|ถูกไหม|ถูกต้องไหม|จริงไหม|จริงมั้ย|ดีกว่าไหม|ดีกว่ามั้ย)$")
TH_YESNO_LEAD = re.compile(r"^(?:ควร|ต้อง|จำเป็น|น่าจะ|เป็นไปได้|ใช่|มี|ได้|สมควร|เหมาะ|คุ้ม|เรา|ผม|ฉัน|บริษัท|ทีม)")
TH_DEGREE = re.compile(r"แค่ไหน|เพียงใด|ขนาดไหน|มากน้อยเพียงใด|มากน้อยแค่ไหน|มากแค่ไหน|เท่าไหร่|เท่าไร|ระดับไหน|กี่มากน้อย")
TH_WHICH = re.compile(r"อันไหน|แบบไหน|ตัวไหน|ทางไหน|ข้อไหน|อะไรดี|อันใด|ตัวเลือกไหน|ทางเลือกไหน|ไหนดี|ไหนเหมาะ|อะไรดีกว่า|ไหนคุ้ม")
TH_OR = re.compile(r"หรือว่า|หรือ(?!ไม่|เปล่า|ยัง)")
TH_LEADS = re.compile(r"(?:ควรใช้|ควรเลือก|ควรซื้อ|ควรไป|ควรทำ|ควรเริ่มจาก|ควรเรียน|ควรจ้าง|ควรย้ายไป|ควรเปลี่ยนไป|ควรจะ|ควร|"
                      r"ใช้|เลือก|ซื้อ|ไป|ทำ|เอา|เรียน|ลงทุน|เริ่มจาก|เริ่ม|จะ|คือ|เป็น|ด้วย|อยาก|ต้อง|ย้ายไป|เปลี่ยนไป|"
                      r"เปลี่ยนเป็น|ไปใช้|ลอง|จ้าง|เข้า|สมัคร|ขอ|รับ|เก็บ|ขาย|ให้|ดู|ฟัง|กิน|ตั้ง|เปิด|ปิด|ส่ง|จอง|ตัดสินใจ|"
                      r"เลือกเอา|ไปทาง|อยู่|เช่า|พัก|เรียนต่อ|ทำงานที่|ย้ายไปอยู่|ไปอยู่|ไปเที่ยว|ไปกิน)")
TH_OPTION_TAIL = re.compile(r"\s*(?:ดีกว่ากัน|ดีกว่า|กันดี|ดี|มากกว่ากัน|มากกว่า|เหมาะกว่า|เหมาะสมกว่า|คุ้มกว่า|คุ้มค่ากว่า|"
                            r"อันไหน.*|แบบไหน.*|ตัวไหน.*|ทางไหน.*|ข้อไหน.*|อะไรดี.*|ไหนดี.*|ไหนเหมาะ.*|ไหนคุ้ม.*|กว่ากัน|กัน)+$")
TH_LEVEL_KEYS = [
    ("likely", r"เป็นไปได้|น่าจะ|โอกาส|ความน่าจะเป็น|มีแนวโน้ม|สำเร็จ"),
    ("risky", r"เสี่ยง|อันตราย|ปลอดภัย"),
    ("hard", r"ยาก|ง่าย|ซับซ้อน|ยุ่งยาก|ใหญ่"),
    ("urgent", r"เร่งด่วน|ด่วน|สำคัญ|จำเป็น|รีบ"),
    ("good", r"ดี|พร้อม|แข็งแรง|มั่นคง|เรียบร้อย|สมบูรณ์|ชัดเจน|แย่|อ่อน|คุณภาพ|พอใจ"),
    ("valuable", r"คุ้ม|มีประโยชน์|มีค่า|กำไร|ผลตอบแทน"),
]


def split_options_th(body: str, with_kap: bool) -> list[str]:
    if re.search(r"[：:；;]", body):
        seg = re.split(r"[：:；;]", body, 1)[1]
    elif "ระหว่าง" in body:
        seg = body.split("ระหว่าง", 1)[1]
        with_kap = True
    else:
        seg = body
        first_or = TH_OR.search(seg)
        if first_or:
            seg = strip_lead(seg, first_or.start(), TH_LEADS, TH_OR)
    seg = TH_OPTION_TAIL.sub("", seg.strip())
    sep = r"หรือว่า|หรือ|,|，|/|\s+กับ\s+|กับ" if with_kap else r"หรือว่า|หรือ|,|，|/"
    return clean_options(re.split(sep, seg), " \"'“”‘’()（）", 80)


def parse_th(q: str) -> tuple[str | None, list[str], list[str]]:
    body = TH_PARTICLES.sub("", q.strip().strip(TH_PUNCT)).strip()
    if TH_NEG_TAIL.search(body):
        return "noul", [], []
    if TH_DEGREE.search(body):
        return "score", [], levels_for(first_key(body, TH_LEVEL_KEYS), "th")
    which = bool(TH_WHICH.search(body))
    if TH_OR.search(body) or which:
        return "choice", split_options_th(body, which), []
    if TH_YESNO_TAIL.search(body) or TH_YESNO_LEAD.match(body):
        return "noul", [], []
    return None, [], []


# ---- dispatch ---------------------------------------------------------------------

def typed_question(question: str, force: str | None, options: list[str] | None,
                   levels: list[str] | None) -> tuple[dict, str]:
    """Return (question object for the API, note about how it was interpreted)."""
    q = question.strip()
    lang = detect_lang(q)
    tag = "" if lang == "en" else f" · {lang}"
    if options:
        return {"type": "choice", "instructions": q, "criteria": {o: None for o in options}}, "choice (options given)" + tag
    if levels:
        return {"type": "score", "instructions": q, "criteria": levels}, "score (levels given)" + tag

    kind = force
    if kind is None:
        if lang == "th":
            kind, options, levels = parse_th(q)
        elif lang.startswith("zh"):
            kind, options, levels = parse_zh(q, lang)
        else:
            kind, options, levels = parse_en(q)

    if kind == "noul":
        return {"type": "noul", "instructions": q}, "yes/no" + tag
    if kind == "choice":
        if not options:
            options = parse_th(q)[1] if lang == "th" else parse_zh(q, lang)[1] if lang.startswith("zh") else split_options_en(q)
        if not options:
            raise JevError("Could not find the options in that question. List them with --options a,b,c "
                           "or phrase it like 'X, Y, or Z?' / 'A、B 还是 C？' / 'A หรือ B'")
        return {"type": "choice", "instructions": q, "criteria": {o: None for o in options}}, "pick one" + tag
    if kind == "score":
        if not levels:
            adj = (DEGREE_RE.match(q) or [None, "much"])[1] if lang == "en" else q
            table = EN_LEVEL_KEYS if lang == "en" else TH_LEVEL_KEYS if lang == "th" else ZH_LEVEL_KEYS
            levels = levels_for(first_key(adj, table), lang)
        return {"type": "score", "instructions": q, "criteria": levels}, "how much (default levels; pass --levels for sharper ones)" + tag

    raise JevError("Jev answers yes/no, pick-one, or how-much questions only; it does not write explanations.\n"
                   "Rephrase, e.g.  'Is X true?'  |  'A, B, or C?'  |  'How risky is X?'\n"
                   "中文：'……吗？' | 'A、B 还是 C？' | '……有多大风险？'   ไทย: '…ไหม' | 'A หรือ B' | '…แค่ไหน'\n"
                   "or force a shape with --type noul|choice|score plus --options / --levels.")


# ----------------------------------------------------------------- answers ----

def read_answer(question: str, ans: dict) -> tuple[str, str]:
    """One-line verdict plus an optional detail line."""
    if ans["type"] == "noul":
        p = ans["noul"]
        verdict = "YES" if p >= YES_ABOVE else "NO" if p <= NO_BELOW else "UNSURE"
        return f"{verdict} (p(yes)={p:.2f})", ""
    if ans["type"] == "choice":
        conf = ans["confidence"]
        tag = "" if conf >= CLOSE_CALL_CONFIDENCE else ", close call"
        dist = " | ".join(f"{k} {v:.2f}" for k, v in sorted(ans["probabilities"].items(), key=lambda kv: -kv[1]))
        return f"{ans['choice']} (p={ans['probabilities'][ans['choice']]:.2f}, confidence {conf:.2f}{tag})", f"options: {dist}"
    if ans["type"] == "score":
        top = len(ans["legend"]) - 1
        nearest = ans["legend"][str(round(ans["score"]))]
        conf = ans["confidence"]
        tag = "" if conf >= CLOSE_CALL_CONFIDENCE else ", close call"
        dist = " | ".join(f"[{i}] {ans['legend'][i][:38]} {ans['probabilities'][i]:.2f}" for i in ans["legend"])
        return f"{ans['score']:.2f} on 0-{top} = \"{nearest}\" (confidence {conf:.2f}{tag})", f"levels: {dist}"
    return json.dumps(ans), ""


def build_state(context: str | None, context_file: str | None, use_stdin: bool) -> object:
    parts: dict = {}
    if context_file:
        path = Path(context_file)
        text = path.read_text()
        if path.suffix == ".json":
            data = json.loads(text)
            parts["context"] = data.get("state", data) if isinstance(data, dict) else data
        else:
            parts["context"] = text
    if use_stdin:
        parts["stdin"] = sys.stdin.read()
    if context:
        parts["notes"] = context
    if not parts:
        return {"context": "No specific context was supplied. Judge from the question alone and general knowledge."}
    return parts


def log_run(record: dict) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


# --------------------------------------------------------------------- ask ----

def ask(questions_text: list[str], state, force: str | None = None, options: list[str] | None = None,
        levels: list[str] | None = None, dry_run: bool = False) -> dict:
    """Turn plain questions into typed ones, call Jev, read and log the answers.

    Raises JevError for anything the caller should show the user.
    """
    if levels and not 2 <= len(levels) <= 10:
        raise JevError("--levels needs between 2 and 10 entries")
    questions, notes = {}, {}
    for i, q in enumerate(questions_text):
        qid = f"q{i + 1}"
        questions[qid], notes[qid] = typed_question(q, force, options, levels)
    if dry_run:
        return {"request": {"state": state, "model": MODEL, "questions": questions},
                "shapes": [notes[f"q{i + 1}"] for i in range(len(questions_text))]}

    resp = call_jev(state, questions, load_api_key())
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for i, q in enumerate(questions_text):
        qid = f"q{i + 1}"
        ans = resp["answers"][qid]
        verdict, detail = read_answer(q, ans)
        results.append({"question": q, "shape": notes[qid], "verdict": verdict, "detail": detail,
                        "typed_question": questions[qid], "answer": ans})
        log_run({"ts": stamp, "model": resp["model"], "question": q, "shape": notes[qid],
                 "state": state, "typed_question": questions[qid], "answer": ans, "verdict": verdict})
    return {"model": resp["model"], "usage": resp["usage"], "state": state, "results": results}


# ------------------------------------------------------------------- main ----

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="+", help="one or more plain-English questions (all share the same context)")
    ap.add_argument("--context", "-c", help="facts Jev should consider (it sees nothing else)")
    ap.add_argument("--context-file", "-f", help="text or JSON file used as context (JSON: its 'state' key if present)")
    ap.add_argument("--stdin", action="store_true", help="read extra context from stdin")
    ap.add_argument("--options", "-o", help="comma-separated options, forces a pick-one question")
    ap.add_argument("--levels", "-l", help="comma-separated ordered levels (2-10), forces a how-much question")
    ap.add_argument("--type", "-t", choices=["noul", "choice", "score"], help="force the question shape")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--verbose", "-v", action="store_true", help="also print the typed question sent to Jev")
    ap.add_argument("--dry-run", action="store_true", help="print the request and exit without calling the API")
    args = ap.parse_args()

    options = [o.strip() for o in args.options.split(",")] if args.options else None
    levels = [l.strip() for l in args.levels.split(",")] if args.levels else None
    state = build_state(args.context, args.context_file, args.stdin)

    try:
        if args.dry_run:
            print(json.dumps(ask(args.question, state, args.type, options, levels, dry_run=True)["request"], indent=2))
            return
        if args.verbose:
            typed = ask(args.question, state, args.type, options, levels, dry_run=True)["request"]["questions"]
            print("typed question(s) sent to Jev:", file=sys.stderr)
            print(json.dumps(typed, indent=2), file=sys.stderr)
        out = ask(args.question, state, args.type, options, levels)
    except JevError as e:
        sys.exit(str(e))

    if args.json:
        print(json.dumps(out, indent=2))
        return
    for r in out["results"]:
        print(f"JEV: {r['verdict']}   <- \"{r['question']}\"  [{r['shape']}]")
        if r["detail"]:
            print(f"     {r['detail']}")
    sys.stdout.flush()
    print(f"     (model {out['model']}, {out['usage']['input_tokens']} in / {out['usage']['output_tokens']} out, "
          f"logged to runs/ask_log.jsonl)", file=sys.stderr)


if __name__ == "__main__":
    main()
