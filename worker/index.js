/**
 * Ask JEV — Cloudflare Worker.
 *
 * Serves ui/index.html (as a static asset) and proxies /api/ask and /api/preview to
 * TypeSafe with the owner's key, which lives in the TYPESAFE_API_KEY secret and never
 * reaches the browser. The question parser below is a line-for-line port of ask_jev.py;
 * keep the two in step.
 *
 *   npx wrangler secret put TYPESAFE_API_KEY
 *   npx wrangler deploy
 */

import LEVELS from "../levels.json" with { type: "json" };

const API_URL = "https://api.typesafe.ai/v1/systemone";
const MODEL = "jev-latest";

// Abuse caps: the key is the owner's, so keep one request small and one visitor slow.
const MAX_QUESTIONS = 5;
const MAX_QUESTION_CHARS = 500;
const MAX_CONTEXT_CHARS = 6000;

const YES_ABOVE = 0.7, NO_BELOW = 0.3, CLOSE_CALL_CONFIDENCE = 0.5;

class JevError extends Error {}

// ------------------------------------------------ natural language -> typed ----
// Three parsers (English, Chinese, Thai), a port of the same section in ask_jev.py.
// Score levels come from levels.json in the asker's language.

const CJK_RE = /[一-鿿㐀-䶿]/;
const THAI_RE = /[฀-๿]/;
const TRAD_CHARS = new Set("們這個說為會應該嗎還選風險難緊價麼於與國時經學問題發現務來對開關點動長車電馬鳥東門買賣讓認識語頁網後種線產測環紅綠烏龍幣費設計態導過進遠運連業專傳轉華達體臺灣廣場際標準積極權觀歡圖書數樓機檢驗資質貴賺錢銀鐵錯鍵願顯頭類實寫將尋層屬島師帶幫廠復從愛戰擇護擴斷舊曆條樣歷氣決況淨濟無熱營獨獲畫當療蓋處號術裝見規視覺許話誰課調談請論講謝證議讀變負財貨責賽軟較輕輸農遊適郵鄉醫釋裡間閱陽隨雙雜離靈領額顧飛飯館髮鬥魚麗齊齒龜兩親");
const SIMP_CHARS = new Set("们这个说为会应该吗还选风险难紧价么于与国时经学问题发现务来对开关点动长车电马鸟东门买卖让认识语页网后种线产测环红绿乌龙币费设计态导过进远运连业专传转华达体台湾广场际标准积极权观欢图书数楼机检验资质贵赚钱银铁错键愿显头类实写将寻层属岛师带帮厂复从爱战择护扩断旧历条样历气决况净济无热营独获画当疗盖处号术装见规视觉许话谁课调谈请论讲谢证议读变负财货责赛软较轻输农游适邮乡医释里间阅阳随双杂离灵领额顾飞饭馆发斗鱼丽齐齿龟两亲");

function detectLang(q) {
  if (THAI_RE.test(q)) return "th";
  if (CJK_RE.test(q)) {
    let trad = 0, simp = 0;
    for (const c of q) { if (TRAD_CHARS.has(c)) trad++; if (SIMP_CHARS.has(c)) simp++; }
    return trad > simp ? "zh-Hant" : "zh-Hans";
  }
  return "en";
}

function levelsFor(key, lang) {
  const table = LEVELS[lang] || LEVELS.en;
  return table[key] || table.default;
}

function firstKey(text, table) {
  for (const [key, pattern] of table) if (pattern.test(text)) return key;
  return "default";
}

const stripChars = (s, chars) => {
  let a = 0, b = s.length;
  while (a < b && chars.includes(s[a])) a++;
  while (b > a && chars.includes(s[b - 1])) b--;
  return s.slice(a, b);
};

function cleanOptions(parts, chars, maxLen) {
  const opts = parts.map((p) => stripChars(p, chars)).filter(Boolean);
  if (opts.length < 2 || opts.length > 20 || opts.some((o) => o.length > maxLen)) return [];
  return opts;
}

/** Drop the preamble before the first option, unless the second option starts with the same lead text. */
function stripLead(seg, prefixEnd, leadsRe, sepRe) {
  const leads = [...seg.slice(0, prefixEnd).matchAll(leadsRe)];
  if (!leads.length) return seg;
  const last = leads[leads.length - 1];
  const leadEnd = last.index + last[0].length;
  const leadText = seg.slice(0, leadEnd).trim();
  const rest = seg.slice(prefixEnd).split(sepRe);
  const second = rest.length > 1 ? rest.slice(1).join("").trim() : "";
  if (second.startsWith(leadText)) return seg;
  return seg.slice(leadEnd);
}

// ---- English --------------------------------------------------------------------

const AUX = "(?:is|are|am|was|were|do|does|did|should|shall|can|could|will|would|has|have|had|must|may|might|need|needs|ought)";
const YESNO_RE = new RegExp(`^\\s*${AUX}\\b`, "i");
const DEGREE_RE = /^\s*how\s+(\w+)/i;
const WHICH_RE = /^\s*(?:which|what)\b/i;
const OR_RE = /\s+or\s+/i;
const STEM_RE = new RegExp(`^\\s*${AUX}\\s+(?:we|i|you|they|it|one|the team)\\s+(?:(?:be\\s+)?(?:better|best|wiser|smarter|safer)\\s+)?(?:to\\s+)?`, "i");
const EN_LEVEL_KEYS = [
  ["likely", /^(?:likely|probable|plausible|realistic)$/i],
  ["risky", /^(?:risky|dangerous|safe|unsafe)$/i],
  ["hard", /^(?:hard|difficult|complex|complicated|easy|simple|big|large)$/i],
  ["urgent", /^(?:urgent|important|critical|pressing)$/i],
  ["good", /^(?:good|strong|well|ready|mature|solid|polished|healthy|effective|clear|bad|weak)$/i],
  ["valuable", /^(?:valuable|useful|worthwhile|profitable|much|many)$/i],
];

function splitOptionsEn(question) {
  const body = question.trim().replace(/[?.!\s]+$/, "").trim();
  if (!OR_RE.test(body) && !body.includes(",")) return [];
  let seg, m;
  if (/[:;]/.test(body)) {
    seg = body.slice(body.search(/[:;]/) + 1);
  } else if ((m = body.match(/\b(?:between|among|out of)\s+(.+)$/i))) {
    seg = m[1];
  } else if (WHICH_RE.test(body) && body.includes(",")) {
    seg = body.slice(body.indexOf(",") + 1);
  } else {
    seg = body.replace(STEM_RE, "");
    if (WHICH_RE.test(seg)) {
      const firstOr = seg.match(OR_RE);
      if (!firstOr) return [];
      const leads = [...seg.slice(0, firstOr.index).matchAll(/\b(?:is|are|be|to|use|using|choose|pick|prefer|with|take|go|do|for)\s+/gi)];
      if (!leads.length) return [];
      const last = leads[leads.length - 1];
      seg = seg.slice(last.index + last[0].length);
    }
  }
  const parts = seg.trim().split(/\s*,\s*(?:or|and)\s+|\s+or\s+|\s*,\s*/i)
    .map((p) => stripChars(p, " \"'").replace(/^(?:rather|instead|just|simply)\s+/i, ""));
  return cleanOptions(parts, " \"'", 80);
}

function parseEn(q) {
  let m;
  if ((m = q.match(DEGREE_RE))) return ["score", [], levelsFor(firstKey(m[1], EN_LEVEL_KEYS), "en")];
  if (YESNO_RE.test(q) && !WHICH_RE.test(q)) { const f = splitOptionsEn(q); return f.length ? ["choice", f, []] : ["noul", [], []]; }
  if (WHICH_RE.test(q)) return ["choice", splitOptionsEn(q), []];
  return [null, [], []];
}

// ---- Chinese (simplified and traditional) ----------------------------------------

const ZH_PUNCT = "？?。！!．.，,、 　\t";
const ZH_YESNO_TAIL = /(?:吗|嗎)$/;
const ZH_YESNO_MARK = /是否|是不是|能不能|该不该|該不該|要不要|应不应该|應不應該|会不会|會不會|可不可以|行不行|好不好|值不值得|有没有|有沒有|需不需要|适不适合|適不適合|对不对|對不對|能否|可否|应否|應否|会否|會否|合不合适|合不合適|划不划算|靠不靠谱|靠不靠譜|可行|算不算|是不是该|是不是該/;
const ZH_YESNO_LEAD = /^(?:应该|應該|该|該|要|能|可以|会|會|是|值得|适合|適合|需要|建议|建議|有必要|现在|現在|我们|我們|我)/;
const ZH_DEGREE = /有多|多大|多高|多低|多难|多難|多容易|多可能|多重要|多紧急|多緊急|多危险|多危險|多好|多强|多強|多成熟|多复杂|多複雜|多值得|多有用|多划算|多严重|多嚴重|多久|多少|程度|几成|幾成/;
const ZH_WHICH = /哪个|哪個|哪一个|哪一個|哪种|哪種|哪项|哪項|哪些|哪家|哪款|哪条|哪條|哪套|哪门|哪門|哪位|哪边|哪邊/;
const ZH_CHOICE_SEP = /还是|還是|或者|抑或|或(?![许許者])|、|，|,/;
const ZH_CHOICE_SEP_G = /还是|還是|或者|抑或|或(?![许許者])|、|，|,/g;
const ZH_CHOICE_MARK = /还是|還是|或者|抑或/;
const ZH_OPTION_TAIL = /(?:呢|吧|比较好|比較好|比较合适|比較合適|更合适|更合適|更好|更划算|更值得|最好|好|合适|合適|划算|更适合|更適合|适合|適合|比较|比較|更)+$/;
const ZH_LEADS = /(?:用|選|选|做|买|買|去|要|是|该|該|应该|應該|建议|建議|适合|適合|采用|採用|使用|考虑|考慮|先|优先|優先|学|學|投|加|换|換|改用|改成|改为|改為|叫|念|读|讀|吃|穿|坐|开|開|租|住|选择|選擇|挑|定|走|上|搞|写|寫|推|上线|上線|部署到|迁移到|遷移到|迁到|遷到|升级到|升級到|从|從|在|把|将|將|跟|和|与|與|留在|留|去|找|请|請|雇|僱|聘|发|發|寄|放|放在|搬到|搬去|投资|投資|买入|買入|卖|賣|读|讀|报|報|考|申请|申請|办|辦)/g;
const ZH_LEVEL_KEYS = [
  ["likely", /可能|把握|概率|機率|机率|几率|幾率|成功/],
  ["risky", /风险|風險|危险|危險|安全/],
  ["hard", /难|難|容易|复杂|複雜|简单|簡單|工作量|费力|費力|费劲|費勁|麻烦|麻煩/],
  ["urgent", /紧急|緊急|重要|急|优先|優先/],
  ["good", /好|强|強|成熟|完善|健康|清楚|清晰|准备|準備|靠谱|靠譜|质量|質量|品质|品質|满意|滿意/],
  ["valuable", /值得|有用|价值|價值|划算|收益|回报|回報|意义|意義/],
];

function splitOptionsZh(body) {
  body = stripChars(stripChars(body, ZH_PUNCT).replace(ZH_OPTION_TAIL, ""), ZH_PUNCT);
  let seg;
  if (/[：:；;]/.test(body)) {
    seg = body.slice(body.search(/[：:；;]/) + 1);
  } else {
    seg = body;
    const firstSep = seg.match(ZH_CHOICE_SEP);
    if (firstSep) {
      let stripped = stripLead(seg, firstSep.index, ZH_LEADS, ZH_CHOICE_SEP);
      const which = seg.slice(0, firstSep.index).match(ZH_WHICH);
      if (stripped === seg && which) stripped = seg.slice(seg.search(ZH_WHICH) + which[0].length);
      seg = stripped;
    }
  }
  seg = stripChars(seg, ZH_PUNCT).replace(ZH_OPTION_TAIL, "");
  const parts = seg.split(ZH_CHOICE_SEP_G).map((p) => (p.length > 2 ? p.replace(/^(?:是|用|选|選|去|要|做)/, "") : p));
  return cleanOptions(parts, ZH_PUNCT + "\"'“”‘’《》「」『』（）()", 40);
}

function parseZh(q, lang) {
  const body = stripChars(q.trim(), ZH_PUNCT);
  if (ZH_YESNO_TAIL.test(body)) return ["noul", [], []];
  if (ZH_DEGREE.test(body)) return ["score", [], levelsFor(firstKey(body, ZH_LEVEL_KEYS), lang)];
  if (ZH_CHOICE_MARK.test(body) || ZH_WHICH.test(body)) return ["choice", splitOptionsZh(body), []];
  if (ZH_YESNO_MARK.test(body) || ZH_YESNO_LEAD.test(body)) return ["noul", [], []];
  return [null, [], []];
}

// ---- Thai ---------------------------------------------------------------------------

const TH_PUNCT = "？?。！!．. ,，\t";
const TH_PARTICLES = /(?:\s*(?:ครับ|คะ|ค่ะ|นะ|น้า|เหรอ|หรอ|ล่ะ|หละ|จ๊ะ|จ้ะ|ฮะ|ครับผม|นะครับ|นะคะ))+$/;
const TH_NEG_TAIL = /(?:หรือไม่|หรือเปล่า|หรือยัง|รึเปล่า|รึยัง|รึไม่|ใช่หรือไม่|หรือ)$/;
const TH_YESNO_TAIL = /(?:ไหม|มั้ย|มั๊ย|ใช่ไหม|ใช่มั้ย|ดีไหม|ดีมั้ย|ได้ไหม|ได้มั้ย|ถูกไหม|ถูกต้องไหม|จริงไหม|จริงมั้ย|ดีกว่าไหม|ดีกว่ามั้ย)$/;
const TH_YESNO_LEAD = /^(?:ควร|ต้อง|จำเป็น|น่าจะ|เป็นไปได้|ใช่|มี|ได้|สมควร|เหมาะ|คุ้ม|เรา|ผม|ฉัน|บริษัท|ทีม)/;
const TH_DEGREE = /แค่ไหน|เพียงใด|ขนาดไหน|มากน้อยเพียงใด|มากน้อยแค่ไหน|มากแค่ไหน|เท่าไหร่|เท่าไร|ระดับไหน|กี่มากน้อย/;
const TH_WHICH = /อันไหน|แบบไหน|ตัวไหน|ทางไหน|ข้อไหน|อะไรดี|อันใด|ตัวเลือกไหน|ทางเลือกไหน|ไหนดี|ไหนเหมาะ|อะไรดีกว่า|ไหนคุ้ม/;
const TH_OR = /หรือว่า|หรือ(?!ไม่|เปล่า|ยัง)/;
const TH_LEADS = /(?:ควรใช้|ควรเลือก|ควรซื้อ|ควรไป|ควรทำ|ควรเริ่มจาก|ควรเรียน|ควรจ้าง|ควรย้ายไป|ควรเปลี่ยนไป|ควรจะ|ควร|ใช้|เลือก|ซื้อ|ไป|ทำ|เอา|เรียน|ลงทุน|เริ่มจาก|เริ่ม|จะ|คือ|เป็น|ด้วย|อยาก|ต้อง|ย้ายไป|เปลี่ยนไป|เปลี่ยนเป็น|ไปใช้|ลอง|จ้าง|เข้า|สมัคร|ขอ|รับ|เก็บ|ขาย|ให้|ดู|ฟัง|กิน|ตั้ง|เปิด|ปิด|ส่ง|จอง|ตัดสินใจ|เลือกเอา|ไปทาง|อยู่|เช่า|พัก|เรียนต่อ|ทำงานที่|ย้ายไปอยู่|ไปอยู่|ไปเที่ยว|ไปกิน)/g;
const TH_OPTION_TAIL = /\s*(?:ดีกว่ากัน|ดีกว่า|กันดี|ดี|มากกว่ากัน|มากกว่า|เหมาะกว่า|เหมาะสมกว่า|คุ้มกว่า|คุ้มค่ากว่า|อันไหน.*|แบบไหน.*|ตัวไหน.*|ทางไหน.*|ข้อไหน.*|อะไรดี.*|ไหนดี.*|ไหนเหมาะ.*|ไหนคุ้ม.*|กว่ากัน|กัน)+$/;
const TH_LEVEL_KEYS = [
  ["likely", /เป็นไปได้|น่าจะ|โอกาส|ความน่าจะเป็น|มีแนวโน้ม|สำเร็จ/],
  ["risky", /เสี่ยง|อันตราย|ปลอดภัย/],
  ["hard", /ยาก|ง่าย|ซับซ้อน|ยุ่งยาก|ใหญ่/],
  ["urgent", /เร่งด่วน|ด่วน|สำคัญ|จำเป็น|รีบ/],
  ["good", /ดี|พร้อม|แข็งแรง|มั่นคง|เรียบร้อย|สมบูรณ์|ชัดเจน|แย่|อ่อน|คุณภาพ|พอใจ/],
  ["valuable", /คุ้ม|มีประโยชน์|มีค่า|กำไร|ผลตอบแทน/],
];

function splitOptionsTh(body, withKap) {
  let seg;
  if (/[：:；;]/.test(body)) {
    seg = body.slice(body.search(/[：:；;]/) + 1);
  } else if (body.includes("ระหว่าง")) {
    seg = body.slice(body.indexOf("ระหว่าง") + "ระหว่าง".length);
    withKap = true;
  } else {
    seg = body;
    const firstOr = seg.match(TH_OR);
    if (firstOr) seg = stripLead(seg, firstOr.index, TH_LEADS, TH_OR);
  }
  seg = seg.trim().replace(TH_OPTION_TAIL, "");
  const sep = withKap ? /หรือว่า|หรือ|,|，|\/|\s+กับ\s+|กับ/ : /หรือว่า|หรือ|,|，|\//;
  return cleanOptions(seg.split(sep), " \"'“”‘’()（）", 80);
}

function parseTh(q) {
  const body = stripChars(q.trim(), TH_PUNCT).replace(TH_PARTICLES, "").trim();
  if (TH_NEG_TAIL.test(body)) return ["noul", [], []];
  if (TH_DEGREE.test(body)) return ["score", [], levelsFor(firstKey(body, TH_LEVEL_KEYS), "th")];
  const which = TH_WHICH.test(body);
  if (TH_OR.test(body) || which) return ["choice", splitOptionsTh(body, which), []];
  if (TH_YESNO_TAIL.test(body) || TH_YESNO_LEAD.test(body)) return ["noul", [], []];
  return [null, [], []];
}

// ---- dispatch ---------------------------------------------------------------------

/** Returns [question object for the API, note about how it was interpreted]. */
function typedQuestion(question, force, options, levels) {
  const q = question.trim();
  const lang = detectLang(q);
  const tag = lang === "en" ? "" : ` · ${lang}`;
  const criteriaOf = (opts) => Object.fromEntries(opts.map((o) => [o, null]));
  if (options && options.length) return [{ type: "choice", instructions: q, criteria: criteriaOf(options) }, "choice (options given)" + tag];
  if (levels && levels.length) return [{ type: "score", instructions: q, criteria: levels }, "score (levels given)" + tag];

  let kind = force || null;
  if (!kind) {
    [kind, options, levels] = lang === "th" ? parseTh(q) : lang.startsWith("zh") ? parseZh(q, lang) : parseEn(q);
  }

  if (kind === "noul") return [{ type: "noul", instructions: q }, "yes/no" + tag];
  if (kind === "choice") {
    if (!options || !options.length) options = lang === "th" ? parseTh(q)[1] : lang.startsWith("zh") ? parseZh(q, lang)[1] : splitOptionsEn(q);
    if (!options.length) throw new JevError("Could not find the options in that question. List them in Advanced → Options, or phrase it like 'X, Y, or Z?' / 'A、B 还是 C？' / 'A หรือ B'");
    return [{ type: "choice", instructions: q, criteria: criteriaOf(options) }, "pick one" + tag];
  }
  if (kind === "score") {
    if (!levels || !levels.length) {
      const adj = lang === "en" ? (q.match(DEGREE_RE) || [null, "much"])[1] : q;
      const table = lang === "en" ? EN_LEVEL_KEYS : lang === "th" ? TH_LEVEL_KEYS : ZH_LEVEL_KEYS;
      levels = levelsFor(firstKey(adj, table), lang);
    }
    return [{ type: "score", instructions: q, criteria: levels }, "how much (default levels; set Levels for sharper ones)" + tag];
  }
  throw new JevError("Jev answers yes/no, pick-one, or how-much questions only; it does not write explanations.\n" +
    "Rephrase, e.g.  'Is X true?'  |  'A, B, or C?'  |  'How risky is X?'\n" +
    "中文：'……吗？' | 'A、B 还是 C？' | '……有多大风险？'   ไทย: '…ไหม' | 'A หรือ B' | '…แค่ไหน'\n" +
    "or force a shape in Advanced with Options / Levels / Shape.");
}

// ----------------------------------------------------------------- answers ----

const f2 = (x) => Number(x).toFixed(2);

function readAnswer(ans) {
  if (ans.type === "noul") {
    const p = ans.noul;
    const verdict = p >= YES_ABOVE ? "YES" : p <= NO_BELOW ? "NO" : "UNSURE";
    return [`${verdict} (p(yes)=${f2(p)})`, ""];
  }
  if (ans.type === "choice") {
    const tag = ans.confidence >= CLOSE_CALL_CONFIDENCE ? "" : ", close call";
    const dist = Object.entries(ans.probabilities).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${k} ${f2(v)}`).join(" | ");
    return [`${ans.choice} (p=${f2(ans.probabilities[ans.choice])}, confidence ${f2(ans.confidence)}${tag})`, `options: ${dist}`];
  }
  if (ans.type === "score") {
    const keys = Object.keys(ans.legend);
    const top = keys.length - 1;
    const nearest = ans.legend[String(Math.round(ans.score))];
    const tag = ans.confidence >= CLOSE_CALL_CONFIDENCE ? "" : ", close call";
    const dist = keys.map((i) => `[${i}] ${ans.legend[i].slice(0, 38)} ${f2(ans.probabilities[i])}`).join(" | ");
    return [`${f2(ans.score)} on 0-${top} = "${nearest}" (confidence ${f2(ans.confidence)}${tag})`, `levels: ${dist}`];
  }
  return [JSON.stringify(ans), ""];
}

// ----------------------------------------------------------------- request ----

const splitCsv = (text) => (text || "").split(",").map((t) => t.trim()).filter(Boolean);

function parseBody(body) {
  const questions = (Array.isArray(body.questions) ? body.questions : []).map((q) => String(q).trim()).filter(Boolean);
  if (!questions.length) throw new JevError("Type a question first.");
  if (questions.length > MAX_QUESTIONS) throw new JevError(`At most ${MAX_QUESTIONS} questions per call on the public demo.`);
  if (questions.some((q) => q.length > MAX_QUESTION_CHARS)) throw new JevError(`Keep each question under ${MAX_QUESTION_CHARS} characters.`);
  const context = String(body.context || "").trim();
  if (context.length > MAX_CONTEXT_CHARS) throw new JevError(`Keep the context under ${MAX_CONTEXT_CHARS} characters on the public demo.`);
  const options = splitCsv(body.options);
  const levels = splitCsv(body.levels);
  if (levels.length && (levels.length < 2 || levels.length > 10)) throw new JevError("Levels needs between 2 and 10 entries.");
  const force = ["noul", "choice", "score"].includes(body.type) ? body.type : null;

  const state = context ? { notes: context }
    : { context: "No specific context was supplied. Judge from the question alone and general knowledge." };
  const typed = {}, shapes = [];
  questions.forEach((q, i) => { const [tq, note] = typedQuestion(q, force, options, levels); typed[`q${i + 1}`] = tq; shapes.push(note); });
  return { questions, state, typed, shapes };
}

async function callJev(state, questions, apiKey) {
  let delay = 500;
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await fetch(API_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
      body: JSON.stringify({ state, model: MODEL, questions }),
    });
    if (res.ok) return res.json();
    const detail = await res.text();
    if ((res.status === 429 || res.status === 529) && attempt < 2) { await new Promise((r) => setTimeout(r, delay)); delay *= 2; continue; }
    if (res.status === 401) throw new JevError("The TypeSafe key on this deployment is missing or invalid — the owner needs to fix it.");
    if (res.status === 422) throw new JevError(`TypeSafe rejected the question: ${detail}`);
    throw new JevError(`TypeSafe is busy (${res.status}). Try again in a moment.`);
  }
  throw new JevError("TypeSafe is busy. Try again in a moment.");
}

const json = (payload, status = 200) => new Response(JSON.stringify(payload), {
  status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
});

async function handleApi(request, env, path) {
  if (request.method === "GET") {
    if (path === "/api/history") return json({ client: true, public: true });
    if (path === "/api/contexts") return json([]);
    return json({ error: "not found" }, 404);
  }
  if (request.method !== "POST" || (path !== "/api/ask" && path !== "/api/preview")) return json({ error: "not found" }, 404);

  let body;
  try { body = await request.json(); } catch { return json({ error: "Body must be JSON." }, 400); }

  try {
    const { questions, state, typed, shapes } = parseBody(body);
    if (path === "/api/preview") return json({ request: { state, model: MODEL, questions: typed }, shapes });

    // One key for everyone: slow each visitor down, and cap the whole deployment.
    const ip = request.headers.get("cf-connecting-ip") || "unknown";
    const [perIp, global] = await Promise.all([env.IP_LIMIT.limit({ key: ip }), env.GLOBAL_LIMIT.limit({ key: "all" })]);
    if (!perIp.success) return json({ error: "Slow down — you have hit the per-visitor limit. Try again in a minute." }, 429);
    if (!global.success) return json({ error: "The public demo is busy right now. Try again in a minute." }, 429);
    if (!env.TYPESAFE_API_KEY) throw new JevError("The TypeSafe key is not configured on this deployment.");

    const resp = await callJev(state, typed, env.TYPESAFE_API_KEY);
    const results = questions.map((q, i) => {
      const answer = resp.answers[`q${i + 1}`];
      const [verdict, detail] = readAnswer(answer);
      return { question: q, shape: shapes[i], verdict, detail, typed_question: typed[`q${i + 1}`], answer };
    });
    return json({ model: resp.model, usage: resp.usage, state, results });
  } catch (e) {
    if (e instanceof JevError) return json({ error: e.message }, 400);
    console.error(e);
    return json({ error: `Something went wrong: ${e.message}` }, 500);
  }
}

export { typedQuestion, readAnswer, detectLang };   // for tests

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname.startsWith("/api/")) return handleApi(request, env, pathname);
    return json({ error: "not found" }, 404);   // everything else is served from ui/ as a static asset
  },
};
