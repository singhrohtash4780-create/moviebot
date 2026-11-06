# Telegram channel -> Firebase Realtime Database
# pip install python-telegram-bot firebase-admin pillow
import os, io, re, json, uuid, socket, base64, asyncio, time, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import firebase_admin
from firebase_admin import credentials, db
from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import Application, MessageHandler, CommandHandler, filters
from PIL import Image, ImageFilter, ImageOps
from telegram.error import RetryAfter

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_URL = os.environ["DB_URL"]
# Backup channel/group ke ID (-100...), comma se alag karo: BACKUP_ID=-1001111,-1002222   (khali ho to backup band)
BACKUP_IDS = [int(x) for x in re.findall(r"-?\d+", os.environ.get("BACKUP_ID", ""))]

_key = os.environ.get("FIREBASE_KEY")   # cloud (Render) par poora JSON text; apne computer par file
firebase_admin.initialize_app(
    credentials.Certificate(json.loads(_key)) if _key else credentials.Certificate("serviceAccountKey.json"),
    {"databaseURL": DB_URL},
)

FIELDS = {"movie name": "name", "series name": "name", "release year": "year",
          "released year": "year", "language": "language", "quality": "quality",
          "format": "format", "season": "season", "episode": "episode",
          "year": "year", "released": "year", "languages": "language", "audio": "language",
          "first aired": "year", "release date": "year", "released on": "year", "release": "year", "aired": "year", "genres": "genre", "genre": "genre", "type": "type", "status": "status"}

LINE_KV = re.compile(r"^\W*([A-Za-z][A-Za-z /&]{1,28}?)\s*(?:[:\u00f7=~\u27a4\u00bb\u2192]|[-\u2013\u2014])\s*(.+)$")

def parse_details(text):
    """Caption ki 'Key: value' lines padho. Separator : ya ÷ ya - ya ~ chalega; shuru ke emoji/symbol hat jate hain."""
    out = {}
    for line in (text or "").splitlines():
        m = LINE_KV.match(line.strip())
        if not m:
            continue
        k = re.sub(r"\s+", " ", m.group(1).strip().lower())
        raw = m.group(2).strip()
        v = re.sub(r"\s*#\w+", "", raw).strip(" |")          # #Official jaise tag hatao
        if not v:
            continue
        if k in FIELDS:
            out[FIELDS[k]] = v
        elif k in ("also", "also includes", "includes", "alias", "also known as"):
            out["alias"] = [x.strip() for x in re.split(r"[,/|]", raw) if x.strip()]    # Also: Jannat 2, Jannat 3
        elif re.fullmatch(r"(file |full |movie |series |film |drama |show |serial |anime )?(name|title)", k):
            out["name"] = raw.strip(" |")      # File Name / Full Name / Name / Title / Drama Name ...
    return out

def guess_name(text, d, has_buttons):
    """Caption mein 'Name:' nahi: pehli line (emoji hata kar) ko naam maano, par tabhi jab post movie-post jaisi ho
    (language/format/quality/year jaisi koi detail ya neeche quality ke button hon)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines or ":" in lines[0]:
        return None
    name = re.sub(r"^[\W_]+", "", lines[0]).strip(" -~|*_")
    if len(name) < 2:
        return None
    if name.isupper():
        name = name.title()           # VOICEMAILS FOR ISABELLE -> Voicemails For Isabelle
    looks_like_post = has_buttons or any(k in d for k in ("language", "format", "quality", "year", "season", "episode", "genre", "type"))
    return name if looks_like_post else None

def parse_pipe(text):
    p = [x.strip() for x in (text or "").split("|")]
    return {"title": p[0] or "Untitled",
            "language": p[1] if len(p) > 1 else "",
            "quality": p[2] if len(p) > 2 else ""}

def make_label(text, default_season=0):
    # File ke naam/caption se label aur season nikalta hai
    t = text or ""
    q = re.search(r"(2160p|1080p|720p|480p|360p)", t, re.I)
    ep = re.search(r"(?<![A-Za-z])(?:S\d+)?E(\d{1,3})(?!\d)", t, re.I)
    se = (re.search(r"(?<![A-Za-z])S(\d{1,2})(?=E\d|[^A-Za-z0-9]|$)", t, re.I)
          or re.search(r"Season[ ._-]*(\d{1,2})", t, re.I))
    season = int(se.group(1)) if se else default_season
    pt = re.search(r"(?<![A-Za-z])Part[ ._-]*(\d{1,2})(?!\d)", t, re.I)
    tag = ("E" + ep.group(1)) if ep else (("Part " + str(int(pt.group(1)))) if pt else "")
    parts = [tag, q.group(1).lower() if q else ""]
    return (" ".join(x for x in parts if x) or "Watch"), season

# ---- File ke naam se movie dhundhne ke helpers ----
STOP = re.compile(r"[ ._-]*(?:S\d{1,2}E\d|S\d{1,2}(?![A-Za-z])|Season|(?<![A-Za-z])E\d{1,3}(?!\d)|(?:19|20)\d{2}|2160p|1080p|720p|480p|360p)", re.I)

# Naam ke shuru ka site ka tag: "(Movies4u.Bid).", "[1TamilMV] -", "www.site.com -" (movie ka naam nahi hota)
LEAD_TAG = re.compile(r"^\s*(?:[\(\[\{][^\)\]\}]*[A-Za-z][^\)\]\}]*[\)\]\}]|www\.[A-Za-z0-9-]+\.[A-Za-z]{2,}"
                      r"|[A-Za-z0-9-]+\.(?:com|net|org|in|me|tv|bid|xyz|to|cc|lol|site|club|live)(?![A-Za-z0-9]))[\s._-]*", re.I)

def strip_tag(s):
    s = s or ""
    for _ in range(3):
        m = LEAD_TAG.match(s)
        if not m or not s[m.end():].strip():
            break
        s = s[m.end():]
    return s

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def junk_name(fname):
    # Hash ya sirf number jaisa naam ("d73cdf6d...357.mkv", "5_6269527456059755565.mov"): isme movie ka naam nahi hota
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", fname or "").strip()
    return bool(stem) and (bool(re.fullmatch(r"[0-9a-fA-F_\-\s]{12,}", stem)) or len(re.sub(r"[^A-Za-z]", "", stem)) < 3)

def file_name_key(fname):
    if junk_name(fname):
        return ""
    base = strip_tag(re.sub(r"\.[A-Za-z0-9]{2,4}$", "", fname or ""))
    m = STOP.search(base)
    return norm(base[:m.start()] if m else base)

def title_key(title):
    t = strip_tag(title)
    t = re.sub(r"\s*-\s*Season\s*\d+\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*\((?:19|20)\d{2}(?:\s*[-\u2013~]\s*(?:19|20)\d{2})?\)\s*$", "", t)
    return norm(t)

def base_key(k):
    # "gangs of wasseypur part 1 2" aur "gangs of wasseypur part 2" dono -> "gangs of wasseypur"
    k = re.sub(r"\bpart \d+(?: \d+)*\b", " ", k or "")
    return re.sub(r"\s+", " ", k).strip()

NAMES = None
YEARS = {}      # card id -> saal (title ke (2002) se): same naam ki alag saal wali movies/series alag rahein   # poster_post_id -> (naam_key, season)
AUTO = set()   # bina-poster wale card (file se bane) ke ID
ALIASES = {}   # poster_post_id -> [doosre naam ki keys] (jaise "jannat 2" bhi is card mein aaye)

def card_year(title):
    m = re.search(r"\(((?:19|20)\d{2})\)", title or "")
    return m.group(1) if m else ""

def load_names():
    global NAMES, AUTO, ALIASES, YEARS
    data = db.reference("movies").get() or {}
    items = data.items() if isinstance(data, dict) else enumerate(data)
    items = [(k, v) for k, v in items if v and v.get("title")]
    NAMES = {int(k): (title_key(v.get("title")), v.get("season", 0) or 0) for k, v in items}
    YEARS = {int(k): card_year(v.get("title")) for k, v in items}
    # auto card: "auto" flag ho, ya card ka apna ID uski file ka ID ho (file se bana tha, poster se nahi)
    AUTO = {int(k) for k, v in items
            if v.get("auto") or str(k) in (v.get("files") or {})
            or not (v.get("poster") or v.get("language") or v.get("quality") or v.get("format"))}
    ALIASES = {}
    for k, v in items:
        al = v.get("alias") or []
        al = list(al.values()) if isinstance(al, dict) else al
        keys = [title_key(a) for a in al if a and title_key(a)]
        if keys:
            ALIASES[int(k)] = keys

def ckey(k):
    # spaces hata kar milao: "i m game" == "im game" (file mein I.m.Game, card mein Im Game)
    return (k or "").replace(" ", "")

def same_name(a, b):
    # Naam barabar, ya ek naam doosre ka kata hua hissa ho ("...exit of go" / "...exit of goodbyes"): Telegram lamba naam kaat deta hai
    ca, cb = ckey(a), ckey(b)
    if not ca or not cb:
        return False
    if ca == cb:
        return True
    lo, hi = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
    ls, hs = (a, b) if len(a) <= len(b) else (b, a)
    # Kata hua naam: chhota naam lambe ka shuru ka hissa ho aur kaat beech word mein ho ("...exit of go" / "...exit of goodbyes")
    if len(lo) >= 15 and hs.startswith(ls) and len(hs) > len(ls) and hs[len(ls)] != " ":
        return True
    # Beech/shuru ke 1-2 chhote words gayab (jaise "the"): chhote naam ke saare words, usi order mein, lambe naam mein hon
    wa, wb = a.split(), b.split()
    ws, wl = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(ws) < 3 or len(wl) - len(ws) > 2:
        return False
    it = iter(wl)
    if not all(w in it for w in ws):             # order mein subsequence
        return False
    left = list(wl)
    for w in ws:
        left.remove(w)
    # bhaag/season ka number ya "part" gayab ho to alag movie hai (Kung Fu Panda / Kung Fu Panda 2)
    return not any(re.search(r"\d", w) or w in ("part", "chapter", "season", "vol", "volume", "episode", "returns") for w in left)

def alias_hit(k, aliases):
    return any(k == a or k.startswith(a + " ") or same_name(k, a) for a in aliases)

def merge_autos():
    """Bina-poster wale card ki files, usi naam ke poster wale card mein daalo aur khali card hata do."""
    load_names()
    merged = 0
    for i in sorted(AUTO):
        k, sn = NAMES.get(i, (None, 0))
        real = [j for j, (kk, ss) in NAMES.items() if j not in AUTO and same_name(kk, k) and ss == sn] if k else []
        if not real and k:
            real = [j for j, al in ALIASES.items() if j not in AUTO and alias_hit(k, al)]
        if not real and k:       # poster wala card nahi: kata hua naam apne lambe naam wale bina-poster card mein milao
            real = [j for j in AUTO if j != i and NAMES.get(j) and NAMES[j][1] == sn
                    and len(ckey(NAMES[j][0])) > len(ckey(k)) and same_name(NAMES[j][0], k)]
        if not real:
            continue
        tgt = max(real, key=lambda j: (len(ckey(NAMES[j][0])), j))
        ex = db.reference(f"movies/{i}/files").get() or {}
        for fid, ef in (ex.items() if isinstance(ex, dict) else enumerate(ex)):
            if ef and not is_duplicate_file(tgt, fid, ef.get("uid", ""), ef.get("file", ""), ef.get("size", "")):
                db.reference(f"movies/{tgt}/files/{fid}").set(ef)
        db.reference(f"movies/{i}").delete()
        db.reference(f"posters/{i}").delete()
        db.reference(f"index/{i}").delete()
        merged += 1
    if merged:
        load_names()
        touch_meta()
    return merged

def pretty_title(ident):
    base = strip_tag(re.sub(r"\.[A-Za-z0-9]{2,4}$", "", ident or ""))
    m = STOP.search(base)
    raw = base[:m.start()] if m else base
    return re.sub(r"[._]+", " ", raw).strip() or "Untitled"

def find_target(ident, label_text, mid):
    # File ke naam/caption se movie dhundho (XO.Kitty.S02E01 -> "XO, Kitty")
    if NAMES is None:
        load_names()
    fkey = file_name_key(ident)
    _, fseason = make_label(label_text)
    if not fkey:   # naam hi nahi mila: sabse nayi poster wali movie/drama (poster wali na ho to koi bhi sabse nayi)
        older = [i for i in NAMES if i < mid]
        older = [i for i in older if i not in AUTO] or older
        return (max(older), NAMES[max(older)][1]) if older else (None, 0)
    cands = [i for i, al in ALIASES.items() if i < mid and alias_hit(fkey, al)]     # /alias se jode doosre naam
    if not cands:
        cands = [i for i, (k, s) in NAMES.items()
                 if i < mid and k and (k == fkey or k.startswith(fkey + " ") or fkey.startswith(k + " "))]
    if not cands:
        # Part 1/Part 2/"Part 1-2" jaise numbers ke bina naam se milao
        fb = base_key(fkey)
        if len(fb) >= 3:
            cands = [i for i, (k, sn) in NAMES.items() if i < mid and k and base_key(k) == fb]
    if not cands:
        ck = ckey(fkey)
        if len(ck) >= 3:       # spaces ka farq ("I.m.Game" vs "Im Game")
            cands = [i for i, (k, s) in NAMES.items() if i < mid and k and same_name(k, fkey)]
    if not cands:
        # File ke naam ke aage site ka tag ho (jaise "Movies4u_Foo_The_Paradise") to movie ka naam beech mein bhi dhundho
        padded = " " + fkey + " "
        cands = [i for i, (k, s) in NAMES.items() if i < mid and len(k) >= 4 and (" " + k + " ") in padded]
        if cands:
            best = max(len(NAMES[i][0]) for i in cands)          # sabse lamba naam jeete ("the paradise" > "paradise")
            cands = [i for i in cands if len(NAMES[i][0]) == best]
    if not cands:
        return None, 0
    if all(i in AUTO for i in cands):                # sirf bina-poster card mile: spaces hata kar poster wala card dhundo
        ck = ckey(fkey)
        extra = [i for i, (k, s) in NAMES.items() if i < mid and i not in AUTO and k and same_name(k, fkey)]
        if extra:
            cands = extra
    fy = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", ident or "")
    if fy and len(cands) > 1:                        # file ke naam mein saal ho to usi saal wala card chuno
        ym = [i for i in cands if YEARS.get(i) == fy.group(1)]
        if ym:
            cands = ym
    real = [i for i in cands if i not in AUTO]       # poster wale card ko pehle chuno
    if real:
        cands = real
    pref = [i for i in cands if fseason and NAMES[i][1] == fseason]
    zero = [i for i in cands if NAMES[i][1] == 0]
    target = max(pref) if pref else (max(zero) if zero else max(cands))
    return target, NAMES[target][1]

# ---- Poster: patti hatao, landscape ko blur canvas par rakho, chhota WebP banao ----
POSTER_W, POSTER_H = 200, 300

def strip_bottom_bar(img):
    """Neeche ki safed patti (jisme doosre channel ka naam hota hai) ho to hata do.
    Patti na ho ya shak ho to image jaisi ki taisi lauta do."""
    g = img.convert("L")
    w, h = g.size
    px = g.load()
    xs = range(0, w, 2)

    def bright_frac(y):
        return sum(1 for x in xs if px[x, y] > 225) / len(xs)

    limit = int(h * 0.22)
    y = h - 1
    while y > h - limit and bright_frac(y) > 0.12:
        y -= 1
    bar = h - 1 - y
    if bar < h * 0.03 or bar > h * 0.2:          # patti jaisi nahi lagi
        return img
    if max(bright_frac(max(0, y - k)) for k in range(1, 6)) > 0.12:   # upar bhi safed: asli image safed hai
        return img

    def row_mean(r):
        return sum(px[x, r] for x in xs) / len(xs)

    below = sum(row_mean(min(h - 1, y + k)) for k in (1, 2, 3)) / 3
    above = sum(row_mean(max(0, y - k)) for k in (1, 2, 3)) / 3
    if below - above < 50:                         # patti ka upar ka kinara tez (sharp) hona chahiye, gradient nahi
        return img
    return img.crop((0, 0, w, max(1, y - 2)))     # 2 px extra, taaki kinara na bache

def make_poster(img):
    """PIL image -> 'data:image/webp;base64,...' (240x360 tak, chhoti file)"""
    img = strip_bottom_bar(img.convert("RGB"))
    w, h = img.size
    if w > h:     # landscape: poora poster beech mein, peeche usi ka blur
        out = ImageOps.fit(img, (POSTER_W, POSTER_H)).filter(ImageFilter.GaussianBlur(10))
        fg = img.copy()
        fg.thumbnail((POSTER_W, POSTER_H))
        out.paste(fg, ((POSTER_W - fg.width) // 2, (POSTER_H - fg.height) // 2))
    else:         # portrait: pehle jaisa
        out = img.copy()
        out.thumbnail((POSTER_W, POSTER_H))
    buf = io.BytesIO()
    try:
        out.save(buf, "WEBP", quality=50, method=6)
        mime = "image/webp"
    except Exception:                      # WebP support na ho to JPEG
        buf = io.BytesIO()
        out.save(buf, "JPEG", quality=50, optimize=True)
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()

def index_set(cid, card):
    """Halka index (sirf naam, quality, date): app list isse banati hai, poora movie data nahi mangti."""
    db.reference(f"index/{cid}").set({"title": card.get("title", ""), "quality": card.get("quality", ""), "date": card.get("date", "")})

def touch_meta():
    # App har kuch minute mein sirf yahi chhoti value padhta hai; badli ho tabhi poori list laata hai
    db.reference("meta").update({"updated": int(time.time() * 1000)})

# Quality ka darja (bada = behtar). Naya poster behtar quality ka aaye to purana card upgrade hota hai
QUAL_RANK = {"cam": 1, "hdcam": 1, "hdts": 2, "hdtc": 2, "hdrip": 3, "dvdrip": 3, "webrip": 4, "webdl": 5, "bluray": 6}
QUAL_RE = re.compile(r"WEB-?DL|WEB-?Rip|Blu-?Ray|HDRip|DVD-?Rip|HDTC|HDTS|HDCAM|CAM", re.I)
# True ho (env KEEP_OLD_FILES=1) to upgrade par purani files bhi card mein rehti hain; warna sirf nayi quality ki files
KEEP_OLD_FILES = os.environ.get("KEEP_OLD_FILES", "").strip().lower() in ("1", "yes", "true")

def quality_rank(q):
    ranks = [QUAL_RANK.get(re.sub(r"[^a-z]", "", m.group(0).lower()), 0) for m in QUAL_RE.finditer(q or "")]
    return max(ranks) if ranks else 0

def is_duplicate_file(target, msg_id, uid, fname, size):
    """Ye file (ya uski copy/forward) is movie mein pehle se judi hai?  file_unique_id forward/repost par bhi wahi rehta hai."""
    files = db.reference(f"movies/{target}/files").get() or {}
    items = files.items() if isinstance(files, dict) else enumerate(files)
    for fid, ef in items:
        if not ef or str(fid) == str(msg_id):      # wahi post dobara/edit: duplicate nahi, update hai
            continue
        if uid and ef.get("uid") == uid:
            return fid
        if not ef.get("uid") and fname and ef.get("file") == fname and ef.get("size") == size:   # purani entries (uid nahi)
            return fid
    return None

def mb(n):
    if not n:
        return ""
    return f"{n / 1024**3:.1f} GB" if n >= 1024**3 else f"{n / 1024**2:.0f} MB"

MAIN_CHAT = None
def remember_main(cid):
    global MAIN_CHAT
    if MAIN_CHAT != cid:
        MAIN_CHAT = cid
        try:
            db.reference("config/main_chat").set(cid)
        except Exception:
            pass

def main_chat():
    return MAIN_CHAT or db.reference("config/main_chat").get()

async def backup_copy(bot, msg):
    """Main channel ki nayi post backup channel/group dono mein copy karo. Ek fail ho to bhi baaki aur main kaam na ruke."""
    for cid in BACKUP_IDS:
        for attempt in range(4):
            try:
                res = await bot.copy_message(chat_id=cid, from_chat_id=msg.chat.id, message_id=msg.message_id,
                                              reply_markup=msg.reply_markup)   # neeche ke buttons bhi saath jayein
                try:      # copy ka ID yaad rakho, taaki /delete backup se bhi hata sake
                    db.reference(f"backup/{msg.message_id}/{cid}").set(res.message_id)
                except Exception as e:
                    print("Backup ID save nahi hua:", type(e).__name__)
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                print("Backup copy fail:", cid, type(e).__name__, str(e)[:120])
                break

async def backup_edit(bot, msg):
    """Main channel ki post edit hui to backup channel/group ki copy bhi usi tarah badlo (text/caption/buttons)."""
    try:
        bmap = db.reference(f"backup/{msg.message_id}").get()
    except Exception:
        return
    if not isinstance(bmap, dict):
        return                                   # is post ki copy ka ID yaad nahi (purani post)
    for cid, bm in bmap.items():
        for attempt in range(3):
            try:
                if msg.text is not None:
                    await bot.edit_message_text(chat_id=int(cid), message_id=int(bm), text=msg.text,
                                                entities=msg.entities, reply_markup=msg.reply_markup)
                else:
                    await bot.edit_message_caption(chat_id=int(cid), message_id=int(bm), caption=msg.caption or "",
                                                   caption_entities=msg.caption_entities, reply_markup=msg.reply_markup)
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    print("Backup edit fail:", cid, type(e).__name__, str(e)[:100])
                break

async def on_channel_post(update, context):
    msg = update.channel_post or update.edited_channel_post
    if not msg.chat.username:
        print("Channel public hona chahiye (username set karo)")
        return
    if update.channel_post and msg.chat.id not in BACKUP_IDS:
        remember_main(msg.chat.id)
    if update.channel_post and BACKUP_IDS and msg.chat.id not in BACKUP_IDS:
        await backup_copy(context.bot, msg)      # nayi post backup channel/group mein bhi
    elif update.edited_channel_post and BACKUP_IDS and msg.chat.id not in BACKUP_IDS:
        await backup_edit(context.bot, msg)      # edit hui post backup mein bhi badlo
    base = f"https://t.me/{msg.chat.username}/"
    movies = db.reference("movies")

    # 1) Poster + details wali post (nayi movie/series)
    if msg.photo and msg.caption:
        d = parse_details(msg.caption)
        if "name" not in d:
            g = guess_name(msg.caption, d, bool(msg.reply_markup and msg.reply_markup.inline_keyboard))
            if g:
                d["name"] = g
        ym = re.search(r"(?:19|20)\d{2}", d.get("year", "") or "")      # "2022-9-9" -> "2022"
        if ym:
            d["year"] = ym.group(0)
        else:
            d.pop("year", None)
        if "name" in d:
            title = d["name"] + (f" ({d['year']})" if d.get("year") else "")
            nums = re.findall(r"\d+", d.get("season", ""))
            dseason = int(nums[0]) if len(nums) == 1 else 0   # "1 - 2" ho to 0 (season file ke naam se aayega)
            if dseason:
                title += f" - Season {dseason}"
            f = await msg.photo[-1].get_file()
            img = Image.open(io.BytesIO(bytes(await f.download_as_bytearray())))
            poster = make_poster(img)
            # Post ke neeche ke buttons (480p/720p/1080p) agar hain to unhe hi files banao
            btns = []
            if msg.reply_markup and msg.reply_markup.inline_keyboard:
                for row in msg.reply_markup.inline_keyboard:
                    for b in row:
                        if b.url:
                            btns.append((b.text.strip(), b.url))
            if btns:
                en = re.findall(r"\d+", d.get("episode", ""))
                if len(en) == 1:
                    title += f" - Episode {int(en[0])}"
            if NAMES is None:
                load_names()
            ny = str(d.get("year", "") or "")
            same = []
            for i, (k, sn) in NAMES.items():
                if i == msg.message_id or i in AUTO or k != title_key(title) or sn != dseason or "Episode" in title:
                    continue
                if not db.reference(f"movies/{i}/title").get():
                    continue
                oy = YEARS.get(i, "")
                if ny and oy and ny != oy:       # naam same par saal alag (Gunaah 2002 vs Gunaah 2024): alag movie/series
                    continue
                same.append(i)
            if same:
                tgt = same[0]
                added = 0
                upgraded = False
                new_r = quality_rank(d.get("quality", ""))
                old_q = db.reference(f"movies/{tgt}/quality").get() or ""
                if new_r and quality_rank(old_q) and new_r > quality_rank(old_q):   # dono ka tag pata ho tabhi      # behtar quality ka poster aaya: purane card ko upgrade karo
                    upd = {"quality": d.get("quality", ""), "link": base + str(msg.message_id), "date": msg.date.isoformat()}
                    for fld in ("language", "format", "alias"):
                        if d.get(fld):
                            upd[fld] = d[fld]
                    movies.child(str(tgt)).update(upd)
                    if not KEEP_OLD_FILES:
                        movies.child(str(tgt)).child("files").delete()     # purani (kam quality) files app se hatao, channel mein rehti hain
                    db.reference("posters").child(str(tgt)).set(poster)
                    index_set(tgt, {"title": db.reference(f"movies/{tgt}/title").get() or title, "quality": upd["quality"], "date": upd["date"]})
                    upgraded = True
                if btns:     # card pehle se hai par buttons nahi judi thi: ab jod do (naye card ki jagah)
                    ex = db.reference(f"movies/{tgt}/files").get() or {}
                    have = {(f.get("label"), f.get("link")) for f in (ex.values() if isinstance(ex, dict) else ex) if f}
                    for i, (text, url) in enumerate(btns):
                        if (text, url) in have:
                            continue
                        movies.child(str(tgt)).child("files").child(f"{msg.message_id}_{i}").set(
                            {"label": text, "season": 0, "link": url, "size": ""})
                        added += 1
                if upgraded:
                    touch_meta()
                    print("Card upgrade hui:", title, "|", old_q, "->", d.get("quality", ""), "| purani files", "rakhi" if KEEP_OLD_FILES else "hata di")
                elif added:
                    touch_meta()
                    print("Card pehle se thi, ab buttons jod diye:", title, "| buttons:", added)
                else:
                    print("Ye movie/season pehle se hai, dobara nahi jodi:", title, "(post", tgt, ")",
                          "| is post mein buttons:", len(btns))
                return
            card = {
                "title": title,
                "language": d.get("language", ""),
                "quality": d.get("quality", ""),
                "format": d.get("format", ""),
                "season": dseason,
                "link": base + str(msg.message_id),
                "date": msg.date.isoformat(),
                "poster": True,       # poster post se bani card (language/quality na ho tab bhi bina-poster card na maani jaye)
            }
            if d.get("alias"):
                card["alias"] = d["alias"]
            movies.child(str(msg.message_id)).update(card)
            index_set(msg.message_id, card)
            db.reference("posters").child(str(msg.message_id)).set(poster)
            for i, (text, url) in enumerate(btns):
                movies.child(str(msg.message_id)).child("files").child(f"{msg.message_id}_{i}").set(
                    {"label": text, "season": 0, "link": url, "size": ""})
            if NAMES is None:
                load_names()
            NAMES[msg.message_id] = (title_key(title), dseason)
            YEARS[msg.message_id] = str(d.get("year", "") or "")
            touch_meta()
            mg = merge_autos()           # pehle aayi bina-poster files ab is card mein aa jayein
            print("Saved:", title, "| buttons:", len(btns), "| merge:", mg if mg else "-")
            return

    # Header post: sticker / animation / bina details wali photo (jaise "1080P", "SEASON 02")
    if msg.sticker or msg.animation or msg.photo:
        db.reference("heads").child(str(msg.message_id)).set(True)
        print("Header post yaad rakhi:", msg.message_id)
        return

    # 2) File wali post
    media = msg.video or msg.document
    if not media:
        return
    name = getattr(media, "file_name", "") or ""
    caption = msg.caption or ""
    # Caption mein "File Name : ..." ho to wahi asli naam hai (file ka naam hash jaisa ho sakta hai)
    doc_name = name                      # file ka apna naam (hash/kuch bhi ho sakta hai)
    has_name = bool(doc_name) and not junk_name(doc_name)     # file ya caption mein asli naam mila?
    m_fn = (re.search(r"file[\s_]*name\s*[:\-\u2013=]\s*(.+)", caption, re.I)
            or re.search(r"^\W*file\s*[:\-\u2013=]\s*(.+)", caption, re.I | re.M))
    if m_fn and m_fn.group(1).strip():
        name = m_fn.group(1).strip()
        has_name = True
    elif junk_name(doc_name) or not doc_name:
        # Caption mein "Title'-" / "Title:" / "Name:" ke baad movie ka naam (agli line mein bhi ho sakta hai)
        m_t = re.search(r"^\W*(?:title|movie\s*name|name)\s*['\u2019`]*\s*[:\-\u2013=]*\s*([^\n]+)", caption, re.I | re.M)
        t = m_t.group(1).strip(" \t-:'\u2019") if m_t else ""
        if t and not re.fullmatch(r"(?:movie|series|hindi|english)\b.*", t, re.I):
            q = re.search(r"(2160|1080|720|480|360)p", caption, re.I)
            name = t + (f" {q.group(1)}p" if q else "")
            has_name = True
    first = caption.strip().splitlines()[0] if caption.strip() else ""
    ident = name if file_name_key(name) else first      # naam kahan se len
    target, cseason = find_target(ident, name + " " + caption, msg.message_id)
    if not target and doc_name and doc_name != name and file_name_key(doc_name):    # caption ka naam na mile to file ka apna naam bhi try karo
        target, cseason = find_target(doc_name, doc_name + " " + caption, msg.message_id)

    if not target and not has_name:      # file ya caption mein koi naam nahi: sabse nayi poster wali movie/drama ke saath jodo
        target, cseason = find_target("", name + " " + caption, msg.message_id)
        if target:
            print("Naam nahi mila, sabse nayi poster ke saath jodi:", target)

    # Koi movie match nahi hui par naam saaf hai: nayi card banao (thumbnail ko poster banakar)
    if not target and has_name and file_name_key(ident):
        poster = ""
        thumb = getattr(media, "thumbnail", None)
        if thumb:
            try:
                f = await thumb.get_file()
                img = Image.open(io.BytesIO(bytes(await f.download_as_bytearray())))
                poster = make_poster(img)
            except Exception as e:
                print("Thumbnail nahi mila:", type(e).__name__)
        title = pretty_title(ident)
        movies.child(str(msg.message_id)).set({
            "title": title, "language": "", "quality": "", "format": "",
            "season": 0, "link": base + str(msg.message_id),
            "date": msg.date.isoformat(), "auto": True})
        if poster:
            db.reference("posters").child(str(msg.message_id)).set(poster)
        NAMES[msg.message_id] = (title_key(title), 0)
        AUTO.add(msg.message_id)
        index_set(msg.message_id, {"title": title, "quality": "", "date": msg.date.isoformat()})
        target, cseason = msg.message_id, 0
        print("Nayi card bani:", title)

    if target:
        label, season = make_label(name + " " + caption, cseason)
        uid = getattr(media, "file_unique_id", "") or ""
        dup = is_duplicate_file(target, msg.message_id, uid, name or first, mb(media.file_size))
        if dup:
            print("Pehle se judi hai, dobara nahi jodi:", label, "(post", dup, ")")
            return
        heads, n = [], msg.message_id - 1
        while len(heads) < 3 and db.reference(f"heads/{n}").get():
            heads.insert(0, n)
            n -= 1
        entry = {"label": label, "season": season, "file": name or first, "size": mb(media.file_size),
                 "link": base + str(msg.message_id), "uid": uid}
        if heads:
            entry["head"] = heads
        movies.child(str(target)).child("files").child(str(msg.message_id)).set(entry)
        touch_meta()
        print("File jodi gayi:", label, "-> movie", target)
        return

    print("Chhodi gayi: file ka naam ya caption nahi mila ->", name)


async def safe_copy(bot, chat_id, user, num, caption=None):
    for attempt in range(4):
        try:
            kw = {"caption": caption} if caption else {}
            await bot.copy_message(chat_id=chat_id, from_chat_id="@" + user, message_id=int(num), **kw)
            return True
        except RetryAfter as e:      # Telegram ne kaha thoda ruko: ruk kar dobara koshish
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            print("Copy fail:", type(e).__name__, str(e)[:120])
            return False
    return False

async def on_start(update, context):
    # App ka quality button: https://t.me/<bot>?start=<movie_id>-<season>-<quality>
    if not context.args:
        await update.message.reply_text("Namaste! Movie app mein quality button dabaoge to files yahin aa jayengi.")
        return
    try:
        mid, season, q = context.args[0].split("-", 2)
        season = int(season)
    except ValueError:
        await update.message.reply_text("Ye link sahi nahi hai.")
        return
    m = db.reference(f"movies/{mid}").get() or {}
    files = [f for f in (m.get("files") or {}).values()
             if (f.get("season", 0) or 0) == season
             and q.lower() in str(f.get("label", "")).lower()
             and re.match(r"https://t\.me/[^/]+/\d+$", str(f.get("link", "")))]
    if not files:
        await update.message.reply_text("Is quality ki files nahi mili.")
        return
    files.sort(key=lambda f: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(f["label"]))])
    await update.message.reply_text(f"{m.get('title', '')} - {q}: {len(files)} file bhej raha hu, thoda ruko...")
    for f in files:
        user, num = re.match(r"https://t\.me/([^/]+)/(\d+)", f["link"]).groups()
        # Pehle us file ke upar wale sticker/image (jaise 1080P, SEASON 02), phir file
        heads = f.get("head") or []
        if isinstance(heads, dict):
            heads = list(heads.values())
        for h in heads:
            await safe_copy(context.bot, update.effective_chat.id, user, h)
            await asyncio.sleep(1)
        # Caption mein file ka poora naam (Telegram upar naam kaat deta hai)
        cap = (f.get("file") or f"{m.get('title', '')} {f.get('label', '')}").strip()[:1000]
        await safe_copy(context.bot, update.effective_chat.id, user, num, cap)
        await asyncio.sleep(1.5)
    await update.message.reply_text("Ho gaya.")

# ---- Admin commands (sirf tum, Telegram se): app ka naam, logo aur links badlo, file edit kiye bina ----
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)

def make_logo(img):
    img = ImageOps.fit(img.convert("RGB"), (128, 128))
    buf = io.BytesIO()
    try:
        img.save(buf, "WEBP", quality=80)
        mime = "image/webp"
    except Exception:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()

async def admin_ok(update):
    uid = update.effective_user.id if update.effective_user else 0
    if not ADMIN_ID:
        await update.message.reply_text("ADMIN_ID set nahi hai. /myid se apna ID dekho aur use ADMIN_ID mein daalo.")
        return False
    if uid != ADMIN_ID:
        await update.message.reply_text("Ye sirf admin ke liye hai.")
        return False
    return True

async def on_myid(update, context):
    await update.message.reply_text(f"Tumhara Telegram ID: {update.effective_user.id}")

async def on_settitle(update, context):
    if not await admin_ok(update):
        return
    title = " ".join(context.args).strip()
    if not title:
        await update.message.reply_text("Aise likho: /settitle Naya Naam")
        return
    db.reference("config/site").update({"title": title[:40]})
    touch_meta()
    await update.message.reply_text("Naam badal diya: " + title[:40])

async def on_setlink(update, context):
    if not await admin_ok(update):
        return
    if len(context.args) != 2 or context.args[0].lower() not in ("request", "help", "channel"):
        await update.message.reply_text("Aise likho:\n/setlink request https://t.me/tumhara_group\n/setlink help https://t.me/tumhara_username\n/setlink channel https://t.me/tumhara_channel\nButton hatane ke liye link ki jagah off likho.")
        return
    key, url = context.args[0].lower(), context.args[1]
    if url.lower() == "off":
        url = ""
    elif not re.match(r"https://(t\.me|telegram\.me)/", url):
        await update.message.reply_text("Link https://t.me/ se shuru hona chahiye.")
        return
    db.reference("config/site").update({key: url})
    touch_meta()
    await update.message.reply_text(f"{key} button ho gaya: " + (url or "band"))

async def on_setlogo(update, context):
    if not await admin_ok(update):
        return
    m = update.message
    src = m if m.photo else (m.reply_to_message if m.reply_to_message and m.reply_to_message.photo else None)
    if not src:
        await update.message.reply_text("Logo ki photo bhejo aur caption mein /setlogo likho (ya photo par reply karke /setlogo likho).")
        return
    f = await src.photo[-1].get_file()
    img = Image.open(io.BytesIO(bytes(await f.download_as_bytearray())))
    db.reference("config/site").update({"logo": make_logo(img)})
    touch_meta()
    await update.message.reply_text("Logo badal diya. App mein 3 minute ke andar dikhega.")

async def on_alias(update, context):
    # /alias https://t.me/channel/57 Jannat 2  -> "Jannat 2" naam ki files bhi us poster wale card mein aayen
    if not await admin_ok(update):
        return
    mt = re.search(r"/(\d+)\s*$", context.args[0]) if context.args else None
    name = " ".join(context.args[1:]).strip()
    if not mt or not title_key(name):
        await update.message.reply_text("Aise likho: /alias https://t.me/tumhara_channel/57 Jannat 2\n(link poster post ka, uske baad wo naam jiski files is card mein chahiye)")
        return
    ref = db.reference(f"movies/{mt.group(1)}")
    card = ref.get()
    if not card:
        await update.message.reply_text("Ye poster post app mein nahi mili.")
        return
    al = card.get("alias") or []
    al = list(al.values()) if isinstance(al, dict) else list(al)
    if name not in al:
        al.append(name)
    ref.child("alias").set(al)
    n = merge_autos()
    await update.message.reply_text(f"Ab '{name}' ki files is card mein aayengi. {n} purana card mila diya.\nApp mein 3 minute ke andar badal jayega.")

async def on_help(update, context):
    if not await admin_ok(update):
        return
    await update.message.reply_text(
        "Admin commands (bot ko private chat mein bhejo):\n\n"
        "/start - bot shuru karo\n"
        "/myid - apna Telegram ID dekho\n"
        "/settitle Naya Naam - app ka naam badlo\n"
        "/setlogo - photo bhejo, caption mein /setlogo likho (app ka logo)\n"
        "/setlink request|help|channel LINK - app ke buttons ke link badlo (band karne ke liye off)\n"
        "/remove POST_LINK - sirf APP se hatao (range: /remove LINK/40-50)\n"
        "/delete POST_LINK - APP, CHANNEL aur BACKUP, teeno se hatao (range: /delete LINK/40-50)\n"
        "/merge - bina-poster wale faltu cards ko poster wale card mein milao\n"
        "/alias POSTER_LINK Naam - doosre naam ki files bhi us card mein lao (jaise Jannat 2)\n"
        "/setq FILE_POST_LINK 480p - file ki quality haath se set karo (label \"Watch\" ho to)\n"
        "/setname LINK Naya.Naam.mkv - file ka naam badlo (kai file: har line mein LINK aur naam)\n"
        "/rebuildindex - app ke liye halka index banao (purani movies ke liye ek baar)\n"
        "/help - ye list\n\n"
        "Auto tareeka: poster ke caption mein ek line likho  Also: Jannat 2, Jannat 3  to un naamo ki files apne aap us card mein aayengi.")

async def on_setq(update, context):
    # /setq POST_LINK 480p [POST_LINK 720p ...] -> file ka quality label haath se set karo (label "Watch" ho to)
    if not await admin_ok(update):
        return
    a = context.args
    pairs = []
    for i in range(0, len(a) - 1, 2):
        mid = re.search(r"(\d+)\s*$", a[i])
        qm = re.fullmatch(r"(2160|1080|720|480|360)p?", a[i + 1].strip(), re.I)
        if mid and qm:
            pairs.append((mid.group(1), qm.group(1) + "p"))
    if not pairs or len(a) % 2:
        await update.message.reply_text("Aise likho (file wali post ka link, phir quality):\n"
                                        "/setq https://t.me/tumhara_channel/2720 480p\n"
                                        "Ek saath kai: /setq LINK1 480p LINK2 720p LINK3 1080p")
        return
    data = db.reference("movies").get() or {}
    items = {str(k): v for k, v in (data.items() if isinstance(data, dict) else enumerate(data)) if v}
    done, miss = [], []
    for pid, q in pairs:
        hit = next(((k, v) for k, v in items.items() if pid in (v.get("files") or {})), None)
        if not hit:
            miss.append(pid)
            continue
        k, v = hit
        old = str((v["files"][pid] or {}).get("label", ""))
        tag = re.sub(r"(2160|1080|720|480|360)p", "", old, flags=re.I).strip()
        tag = "" if tag.lower() == "watch" else tag
        label = (tag + " " + q).strip()
        db.reference(f"movies/{k}/files/{pid}/label").set(label)
        done.append(f"{pid} -> {label}")
    if done:
        touch_meta()
    msg = ("Label badal diye:\n" + "\n".join(done)) if done else "Koi file nahi mili."
    if miss:
        msg += "\nYe post app mein file ki tarah nahi mili: " + ", ".join(miss)
    await update.message.reply_text(msg + ("\nApp mein 3 minute ke andar badal jayega." if done else ""))

async def on_setname(update, context):
    # /setname LINK Naya.Naam.mkv   (ek file)   ya   kai file: har line mein  LINK  naam
    if not await admin_ok(update):
        return
    lines = (update.message.text or "").splitlines()
    first = re.sub(r"^/setname(@\w+)?[ \t]*", "", lines[0] if lines else "")
    entries = [e.strip() for e in [first] + lines[1:] if e.strip()]
    jobs, bad = [], []
    for e in entries:
        parts = e.split(None, 1)
        mid = re.search(r"(\d+)\s*$", parts[0]) if parts else None
        name = parts[1].strip() if len(parts) == 2 else ""
        if not mid or not name or re.search(r"https?://|t\.me/", name) or len(name) > 200:
            bad.append(e[:60])
        else:
            jobs.append((mid.group(1), name))
    if not jobs:
        await update.message.reply_text("Aise likho (har line mein file wali post ka link, phir naya naam):\n"
                                        "/setname\n"
                                        "https://t.me/tumhara_channel/2720 Dhoondte.Reh.Jaaoge.2009.480p.mkv\n"
                                        "https://t.me/tumhara_channel/2721 Dhoondte.Reh.Jaaoge.2009.720p.mkv"
                                        + ("\n\nYe line samajh nahi aayi: " + "; ".join(bad) if bad else ""))
        return
    data = db.reference("movies").get() or {}
    items = {str(k): v for k, v in (data.items() if isinstance(data, dict) else enumerate(data)) if v}
    out = []
    for pid, new in jobs:
        hit = next((k for k, v in items.items() if pid in (v.get("files") or {})), None)
        if not hit:
            out.append(f"{pid}: app mein file ki tarah nahi mili")
            continue
        db.reference(f"movies/{hit}/files/{pid}/file").set(new)
        bmap = db.reference(f"backup/{pid}").get() or {}
        bok = bfail = 0
        for cid, copy_id in (bmap.items() if isinstance(bmap, dict) else []):
            try:
                await context.bot.edit_message_caption(chat_id=int(cid), message_id=int(copy_id), caption=new[:1024])
                bok += 1
            except Exception as e:
                if "not modified" in str(e).lower():
                    bok += 1
                else:
                    bfail += 1
                    print("Backup caption nahi badla:", cid, copy_id, type(e).__name__, str(e)[:100])
            await asyncio.sleep(0.5)
        bk = f" | backup {bok} jagah" + (f", {bfail} nahi" if bfail else "") if bmap else " | backup copy nahi mili"
        out.append(f"{pid}: {new}{bk}")
    touch_meta()
    msg = "File ka naam badal diya:\n" + "\n".join(out)
    if bad:
        msg += "\n\nYe line samajh nahi aayi: " + "; ".join(bad)
    await update.message.reply_text(msg)

async def on_rebuildindex(update, context):
    # /rebuildindex -> purani saari movies ka halka index ek baar mein banao (naya app chalane se pehle ek baar)
    if not await admin_ok(update):
        return
    data = db.reference("movies").get() or {}
    items = data.items() if isinstance(data, dict) else enumerate(data)
    idx = {str(k): {"title": v.get("title", ""), "quality": v.get("quality", ""), "date": v.get("date", "")}
           for k, v in items if v and v.get("title")}
    db.reference("index").set(idx)
    touch_meta()
    await update.message.reply_text(f"Index ban gaya: {len(idx)} movies. Ab nayi site chalu kar sakte ho.")

async def on_merge(update, context):
    # /merge -> bina-poster wale faltu cards ki files poster wale card mein jod do
    if not await admin_ok(update):
        return
    n = merge_autos()
    await update.message.reply_text(f"{n} faltu card poster wale card mein mila diye.\nApp mein 3 minute ke andar badal jayega." if n
                                    else "Koi faltu card nahi mila (ya uska poster wala card abhi nahi bana).")

def parse_ids(arg):
    """'https://t.me/ch/57' ya 'https://t.me/ch/40-50' ya '40-50' -> (ids, error)"""
    rg = re.search(r"(\d+)\s*[-\u2013]\s*(\d+)\s*$", arg)
    one = re.search(r"/(\d+)\s*$", arg)
    if rg:
        lo, hi = sorted((int(rg.group(1)), int(rg.group(2))))
        if hi - lo > 200:
            return None, "Ek baar mein zyada se zyada 200 posts hata sakte ho. Chhota range likho."
        return [str(n) for n in range(lo, hi + 1)], None
    if one:
        return [one.group(1)], None
    return None, "format"

def app_remove(ids):
    """Ye post(s) app (Firebase) se hatao. (card, file, card ke naam) lautata hai."""
    global NAMES
    data = db.reference("movies").get() or {}
    items = {str(k): v for k, v in (data.items() if isinstance(data, dict) else enumerate(data)) if v}   # list aaye to khali (None) jagah chhod do
    cards, files_n, titles = 0, 0, []
    for pid in ids:
        if pid in items:                                   # poster post: poori movie hatao
            titles.append(items[pid].get("title", ""))
            db.reference(f"movies/{pid}").delete()
            db.reference(f"posters/{pid}").delete()
            db.reference(f"index/{pid}").delete()
            items.pop(pid, None)
            cards += 1
            continue
        for k, v in list(items.items()):
            files = (v or {}).get("files") or {}
            if pid in files:
                db.reference(f"movies/{k}/files/{pid}").delete()
                files_n += 1
                files.pop(pid, None)
                if not files:                              # aakhri file thi: khali card ka kya kaam
                    titles.append(v.get("title", ""))
                    db.reference(f"movies/{k}").delete()
                    db.reference(f"posters/{k}").delete()
                    db.reference(f"index/{k}").delete()
                    items.pop(k, None)
                    cards += 1
                break
    if cards or files_n:
        NAMES = None            # naam ki yaad dobara banegi
        touch_meta()
    return cards, files_n, titles

async def tg_delete(bot, chat_id, message_id):
    """Telegram se ek message hatao -> 'ok' / 'gone' (pehle se nahi thi) / 'fail'"""
    for _ in range(3):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            return "ok"
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            txt = str(e).lower()
            if "not found" in txt:
                return "gone"
            print("Delete fail:", chat_id, message_id, type(e).__name__, str(e)[:100])
            return "fail"
    return "fail"

REMOVE_USAGE = ("Aise likho:\n{c} https://t.me/tumhara_channel/57\n{c} https://t.me/tumhara_channel/40-50\n"
                "(post ka link; range mein 40 se 50 tak sab)")

async def on_remove(update, context):
    # /remove LINK  ya  LINK/40-50  -> sirf APP se hatao (channel/backup mein post rehti hai)
    if not await admin_ok(update):
        return
    ids, err = parse_ids(" ".join(context.args).strip())
    if err:
        await update.message.reply_text(err if err != "format" else REMOVE_USAGE.format(c="/remove"))
        return
    cards, files_n, titles = app_remove(ids)
    if not cards and not files_n:
        await update.message.reply_text("Ye post(s) app mein nahi mili.")
        return
    msg = f"{files_n} file aur {cards} card app se hata diye."
    if titles:
        msg += "\nCard: " + ", ".join(t for t in titles[:8] if t)
    await update.message.reply_text(msg + "\nApp mein 3 minute ke andar hat jayegi.")

async def on_delete(update, context):
    # /delete LINK  ya  LINK/40-50  -> APP se bhi, CHANNEL se bhi, BACKUP se bhi (wapas nahi aayega)
    if not await admin_ok(update):
        return
    arg = " ".join(context.args).strip()
    ids, err = parse_ids(arg)
    if err:
        await update.message.reply_text(err if err != "format" else REMOVE_USAGE.format(c="/delete"))
        return
    mu = re.search(r"t\.me/([A-Za-z0-9_]+)/", arg)
    chat = ("@" + mu.group(1)) if mu else main_chat()
    if not chat:
        await update.message.reply_text("Channel pata nahi chala. Link ke saath likho: /delete https://t.me/tumhara_channel/57")
        return
    cards, files_n, _ = app_remove(ids)
    ch = {"ok": 0, "gone": 0, "fail": 0}
    bk = {"ok": 0, "gone": 0, "fail": 0}
    bk_unknown = 0
    for pid in ids:
        db.reference(f"heads/{pid}").delete()
        bmap = db.reference(f"backup/{pid}").get()
        if isinstance(bmap, dict) and bmap:
            for cid, bm in bmap.items():
                bk[await tg_delete(context.bot, int(cid), int(bm))] += 1
            db.reference(f"backup/{pid}").delete()
        else:
            bk_unknown += 1
        ch[await tg_delete(context.bot, chat, int(pid))] += 1
        await asyncio.sleep(0.05)
    lines = [f"App: {files_n} file, {cards} card hata diye.",
             f"Channel: {ch['ok']} post hata di" + (f", {ch['fail']} nahi hui" if ch["fail"] else "") + (f", {ch['gone']} pehle se nahi thi" if ch["gone"] else "") + "."]
    if BACKUP_IDS:
        lines.append(f"Backup: {bk['ok']} copy hata di" + (f", {bk['fail']} nahi hui" if bk["fail"] else "")
                     + (f". {bk_unknown} post ki copy ka ID yaad nahi (purani post), wo haath se hatani hogi" if bk_unknown else "") + ".")
    if ch["fail"] or bk["fail"]:
        lines.append("Jo nahi hui uski wajah: bot ko wahan Admin banao aur 'Delete messages' permission do. Telegram bahut purani (48 ghante se zyada) posts bhi kabhi nahi hatata.")
    await update.message.reply_text("\n".join(lines))

async def on_error(update, context):
    # Network ki chhoti dikkat (Telegram tak nahi pahunch paya) par lambi traceback ki jagah ek line likho
    err = context.error
    print("Error:", type(err).__name__, str(err)[:150])
    try:      # admin ko chat mein bhi dikhao, taaki command chup na lage
        if update and getattr(update, "message", None) and update.effective_user and update.effective_user.id == ADMIN_ID:
            await update.message.reply_text("Error: " + type(err).__name__ + " - " + str(err)[:200])
    except Exception:
        pass

async def post_init(application):
    me = await application.bot.get_me()
    db.reference("config").update({"bot": me.username})   # app is username se link banata hai
    if db.reference("meta/updated").get() is None:
        touch_meta()                                       # app ko pehli value mil jaye
    print("Bot chalu:", me.username)
    # Telegram ke "/" menu mein commands dikhao: sab ko sirf /start, admin ko poori list
    try:
        await application.bot.set_my_commands([BotCommand("start", "Bot shuru karo")])
        if ADMIN_ID:
            await application.bot.set_my_commands([
                BotCommand("help", "Saari admin commands ki list"),
                BotCommand("myid", "Apna Telegram ID dekho"),
                BotCommand("settitle", "App ka naam badlo"),
                BotCommand("setlogo", "App ka logo badlo (photo ke saath)"),
                BotCommand("setlink", "Request/Help/Channel button ka link"),
                BotCommand("remove", "Sirf app se hatao (post link)"),
                BotCommand("delete", "App, channel aur backup teeno se hatao"),
                BotCommand("merge", "Bina-poster faltu cards milao"),
                BotCommand("alias", "Doosre naam ki files card mein lao"),
                BotCommand("setq", "File ki quality (480p/720p/1080p) set karo"),
                BotCommand("setname", "File ka naam badlo (caption + backup)"),
                BotCommand("rebuildindex", "App ka index dobara banao"),
                BotCommand("start", "Bot shuru karo"),
            ], scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    except Exception as e:
        print("Command menu set nahi hua:", type(e).__name__, str(e)[:100])
    try:
        n = merge_autos()
        if n:
            print("Start par", n, "faltu card poster wale card mein mila diye")
    except Exception as e:
        print("Auto merge error:", type(e).__name__, str(e)[:100])

class Ping(BaseHTTPRequestHandler):
    # Render ko "inbound traffic" chahiye; UptimeRobot isse har 5 minute mein ping karta hai
    def _ok(self):
        self.send_response(200)
        self.end_headers()
    def do_GET(self):
        self._ok()
        self.wfile.write(b"ok")
    def do_HEAD(self):
        self._ok()
    def log_message(self, *args):
        pass

def serve():
    try:
        HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Ping).serve_forever()
    except OSError as e:
        print("Ping server nahi chala:", e)

threading.Thread(target=serve, daemon=True).start()

# ---- Ek hi bot active rahe: CMD, Termux aur Render teeno saath chalein to bhi Conflict nahi ----
# Firebase mein ek "lock" rakhte hain. Jis ke paas lock hai wahi Telegram se updates leta hai.
# Wo band ho jaye (CMD band, phone off) to ~1 minute mein agla taiyaar bot lock le leta hai.
ME = uuid.uuid4().hex[:8]
WHERE = os.environ.get("BOT_WHERE") or ("render" if os.environ.get("RENDER") else socket.gethostname())
LEASE_MS = 60000

def try_lock():
    now = int(time.time() * 1000)
    def fn(cur):
        if cur is None or cur.get("id") == ME or now - cur.get("ts", 0) > LEASE_MS:
            return {"id": ME, "ts": now, "where": WHERE}
        return cur
    ref = db.reference("lock/leader")
    ref.transaction(fn)
    cur = ref.get() or {}
    return cur.get("id") == ME, cur

def release_lock():
    try:
        db.reference("lock/leader").transaction(lambda cur: None if cur and cur.get("id") == ME else cur)
    except Exception:
        pass

def heartbeat():
    while True:
        time.sleep(20)
        try:
            ok, cur = try_lock()
            if not ok:
                print("Lock doosre bot ke paas chala gaya:", cur.get("where"), "- main band ho raha hu")
                os._exit(0)
        except Exception as e:
            print("Heartbeat error:", type(e).__name__)

while True:
    ok, cur = try_lock()
    if ok:
        break
    print(f"Doosra bot ('{cur.get('where')}') abhi active hai. Main intezaar kar raha hu...")
    time.sleep(15)
print("Main active bot hu:", WHERE)
threading.Thread(target=heartbeat, daemon=True).start()

app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
app.add_handler(CommandHandler("start", on_start, block=False))
app.add_handler(CommandHandler("myid", on_myid))
app.add_handler(CommandHandler("settitle", on_settitle))
app.add_handler(CommandHandler("setlink", on_setlink))
app.add_handler(CommandHandler("setlogo", on_setlogo))
app.add_handler(CommandHandler("remove", on_remove))
app.add_handler(CommandHandler("delete", on_delete))
app.add_handler(CommandHandler("merge", on_merge))
app.add_handler(CommandHandler("rebuildindex", on_rebuildindex))
app.add_handler(CommandHandler("alias", on_alias))
app.add_handler(CommandHandler("setq", on_setq))
app.add_handler(CommandHandler("setname", on_setname))
app.add_handler(CommandHandler("help", on_help))
app.add_error_handler(on_error)
app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r"^/setlogo") & filters.ChatType.PRIVATE, on_setlogo))
app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST | filters.UpdateType.EDITED_CHANNEL_POST, on_channel_post))
try:
    app.run_polling(allowed_updates=["channel_post", "edited_channel_post", "message"])
finally:
    release_lock()   # band hote hi lock chhod do, taaki doosra bot turant le le
