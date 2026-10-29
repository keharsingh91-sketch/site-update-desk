"""Site update desk: WhatsApp site chats (+ photos, + Outlook emails) -> daily site email,
management report and client pending summary."""
import base64
import io
import json
import re
import zipfile
from datetime import date, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

import pandas as pd
import streamlit as st
from anthropic import Anthropic
from PIL import Image

st.set_page_config(page_title="Site update desk", page_icon="📡", layout="wide")

# ---------------------------------------------------------------- defaults
EX = """13674 ENNISKILLEN MF(COL.MOD.UK.0009238) BUILD Day 02 : Team attended the site and continued work. Correct square bolts were delivered. 3 x antennas were swapped out. 2 new poles were installed along with bob ers’s and earth bars. Team confirmed that they had intermittent weather, which had slowed them down.

LANCASTER AMTE COL.MOD.UK.0004880 BUILD Final Day : Electrical and rigging teams attended the site. All electrical works had been done. AC works was complete, decom was completed, AIRI DCDU works had been completed, Vertiv PSU had been installed, BOB Boxes connections had been done. All rigging works have also been done. Site build was completed and was ready to be handed over to DICI.

Electricians confirmed that the Legacy DCDU within FURI is not in the correct location but will need to remain due to cable entry points in the rack. Some redlines in DD in the electrical schematic as well: C10 60898 lighting circuit on 8L2, C20 61009 socket circuit on 8L3. This is due to cable lengths and positioning of the earth bar on the new DB and where they were on the existing DB.

23902- Leeds Highfield Lane -COL.MOD.UK.0009254 Build Day 02 : Team attended the site along with MEWP. Yesterday as well, we hadn’t received confirmation from Cellnex/Northern Gas regarding the MEWP setup. Team carried out GL work as much as they could. They had got everything prepped and ready for height work. H3G 18/21 had been cut over to the new diplexors. Flexi stack is decommed. 18/21 fibre/DC/RF tails were run around and into position. That’s about all the team could do at ground level."""

EX2 = """Site 1 :- LANCASTER AMTE WOW Day 2 : Team attended the site. Site is on AIR and no issue was reported by DICI. All VSWR/losses were also within range. Scaffolding team also attended the site, and scaffolding has also been struck. Site cleared. HOP data has also been collected. Everything has been completed from Teligent’s site.

Site 2 :- 3674 ENNISKILLEN MF Build Final Day : Team attended the site and started the work. All works have been done except GPS as the correct connectors are being delivered with SC van today. Team confirmed that build completed and ready to hand over to the DICI.
• Some snagging works were also left to be completed today."""

GL = """"Morning all, on site now" or "arrived" = The team (rigger, engineer or FE) attended the site.
"Outages on CTIL" followed later by a fix or "resolved" = Outages on CTIL were reported and then resolved.
"OHS audit done" = The OHS audit was completed.
"Decom done" with a decom items list or file shared in the chat = Decom was completed and the decom items list was shared.
"AI completed, PI complete on site" = AI and PI were completed on site.
"All alarms cleared" = All alarms were cleared.
"CTs completed. PC done" = All CTs were completed and PC was also done.
Any short "done", "complete" or "cleared" line means that task was finished: write it as a completed task in the past tense.
A file name in the chat (list, form, certificate, report) means that document was shared."""

INC = """Build installation details (what was installed or built)
GPS, AIRI and ERS installation
Equipment, band, sector and cell details such as High Delta L1800 S1 C2
Site lock and unlock information (access, keys, who opened or locked the site)
Team returning to site (when, why, and what is left to do)"""

BUL = {
    "Auto: bullets only for lists": 'Write a paragraph by default. Use bullet points (each on its own line starting with "• ") only where there is a list of three or more separate items, such as pending work, snag items, issues or materials: put the paragraph first, then the bullets on separate lines.',
    "Always paragraphs": "Write paragraphs only. Do not use bullet points.",
    "Always bullets": 'Write the update as bullet points, each on its own line starting with "• ".',
}

# ---------------------------------------------------------------- helpers
HEAD = re.compile(r"^\[?(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4}),?\s+(\d{1,2})[:.]\d{2}(?::\d{2})?(?:\s?([AaPp])\.?[Mm]\.?)?\]?\s*(?:-\s*)?")
SENDER = re.compile(r"^([^:]{1,60}?):\s(.*)$")
ATT = re.compile(r"[^\s<>:\"/\\]+\.(?:jpe?g|png|webp|opus|m4a|mp3|aac|ogg|amr|wav)", re.I)
IMG = re.compile(r"\.(jpe?g|png|webp)$", re.I)
NOISE = re.compile(r"<attached:\s*>|\(file attached\)|<Media omitted>|<This message was edited>|image omitted|audio omitted", re.I)


def secret(key, default=""):
    try:
        return st.secrets[key]
    except Exception:
        return default


def fmt(d):
    return d.strftime("%d %b %Y")


def parse_chat(text, dayfirst=True, cut=0):
    """WhatsApp export text -> list of {'d': date, 'text': str}. Messages before `cut` o'clock count as the previous day."""
    msgs, cur = [], None
    for raw in re.sub(r"[\u200e\u202f\u00a0]", " ", text).splitlines():
        h = HEAD.match(raw)
        if h:
            a, b, y = int(h[1]), int(h[2]), int(h[3])
            y += 2000 if y < 100 else 0
            dd, mm = (a, b) if dayfirst else (b, a)
            hr = int(h[4])
            if h[5]:
                pm = h[5].lower() == "p"
                hr = hr + 12 if pm and hr < 12 else (0 if (not pm and hr == 12) else hr)
            m = SENDER.match(raw[h.end():])
            if m:
                try:
                    d = date(y, mm, dd)
                except ValueError:
                    cur = None
                    continue
                if hr < cut:
                    d -= timedelta(days=1)
                cur = {"d": d, "text": m.group(2)}
                msgs.append(cur)
            else:
                cur = None
        elif cur is not None:
            cur["text"] += "\n" + raw
    return msgs


def day_data(msgs, d):
    imgs, auds, lines = [], [], []
    for m in msgs:
        if m["d"] != d:
            continue
        for f in ATT.findall(m["text"]):
            (imgs if IMG.search(f) else auds).append(f)
        t = NOISE.sub("", ATT.sub("", m["text"]))
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            lines.append(t)
    return imgs, auds, lines


def clean(t, banned, rm_phone=True):
    for w in banned:
        t = re.sub(re.escape(w), "", t, flags=re.I)
    if rm_phone:
        t = re.sub(r"\+?\d[\d\s\-()]{8,}\d", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"[ \t]+([.,;:])", r"\1", t)
    return t.strip()


def site_name(filename):
    n = re.sub(r"\.(txt|zip)$", "", filename, flags=re.I)
    n = re.sub(r"^WhatsApp Chat (with|-)\s*", "", n, flags=re.I)
    return re.sub(r"\s*\(\d+\)$", "", n).strip()


def detect_stage(txt):
    found = re.findall(r"\b(build|survey|decom|install)\s+(day\s*\d+|final day)\b", txt, re.I)
    if not found:
        return ""
    w, dpart = found[-1]
    if "final" in dpart.lower():
        return f"{w.upper()} Final Day"
    num = int(re.sub(r"\D", "", dpart))
    return f"{w.upper()} Day {num:02d}"


@st.cache_data(show_spinner=False)
def read_chat(data: bytes, filename: str) -> str:
    if filename.lower().endswith(".zip"):
        z = zipfile.ZipFile(io.BytesIO(data))
        names = [n for n in z.namelist() if n.lower().endswith(".txt") and not n.endswith("/")]
        return z.read(names[0]).decode("utf-8", "ignore") if names else ""
    return data.decode("utf-8", "ignore")


def load_photos(data: bytes, names, limit=4):
    """Pick up to `limit` photos spread across the day from the zip, shrink them, return base64 JPEGs."""
    if not names:
        return []
    z = zipfile.ZipFile(io.BytesIO(data))
    index = {n.split("/")[-1].lower(): n for n in z.namelist()}
    pick = names if len(names) <= limit else [names[int(k * len(names) / limit)] for k in range(limit)]
    out = []
    for n in pick:
        path = index.get(n.lower())
        if not path:
            continue
        try:
            im = Image.open(io.BytesIO(z.read(path))).convert("RGB")
            im.thumbnail((1280, 1280))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            out.append(base64.b64encode(buf.getvalue()).decode())
        except Exception:
            pass
    return out


def parse_mail(raw: bytes, name: str):
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        if not (msg["subject"] or msg["from"] or msg["date"]):
            raise ValueError("not an email file")
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        if body is not None and body.get_content_type() == "text/html":
            text = re.sub(r"(?is)<(style|script).*?</\1>", "", text)
            text = re.sub(r"<[^>]+>", " ", text)
        try:
            d = parsedate_to_datetime(str(msg["date"])).date()
        except Exception:
            d = None
        return {"subject": str(msg["subject"] or name), "date": d, "text": re.sub(r"\n{3,}", "\n\n", text).strip()}
    except Exception:
        t = raw.decode("utf-8", "ignore").strip()
        return {"subject": (t.splitlines() or [name])[0][:100], "date": None, "text": t}


def tokens(name):
    t = re.findall(r"[A-Za-z0-9._/\-]{4,}", name)
    t = [x for x in t if re.search(r"\d", x) or len(x) >= 6] + re.findall(r"\d{4,}", name)
    return [x.lower() for x in t]


def mails_for(name, mails, d=None, same_day=True):
    tk = tokens(name)
    out = []
    for m in mails:
        if not any(t in (m["subject"] + " " + m["text"]).lower() for t in tk):
            continue
        if d and same_day and m["date"] and m["date"] != d:
            continue
        out.append(m)
    return out[:5]


def parse_json(t):
    t = re.sub(r"```(?:json)?", "", t).strip()
    return json.loads(t[t.find("{"): t.rfind("}") + 1])


def ask(client, model, prompt, images=None, max_tokens=2500):
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in (images or [])]
    content.append({"type": "text", "text": prompt})
    r = client.messages.create(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": content}])
    return parse_json("".join(b.text for b in r.content if b.type == "text"))


def mail_block(ms, banned, rm_phone, cap=2500):
    return "\n---\n".join(
        f"Subject: {clean(m['subject'], banned, rm_phone)}" + (f"\nDate: {m['date']}" if m["date"] else "") + "\n" + clean(m["text"], banned, rm_phone)[:cap]
        for m in ms
    ) or "(none)"


def day_prompt(site, stage, d, lines, mails, voice, n_img, o):
    ban = f" Never use these words or phrases: {'; '.join(o['banned'])}." if o["banned"] else ""
    return "\n".join([
        "You write the daily site update for a telecom site delivery team, for management. Copy the style of the example: the site line, then one paragraph in past tense, plain professional English, factual. Add a short second paragraph only for notes, redlines, issues or pending items.",
        "", "STYLE EXAMPLE:", o["example"],
        ("\nSECOND STYLE EXAMPLE (numbered sites, bullets for a list):\n" + EX2) if o["number"] else "",
        "\nBULLETS: " + BUL[o["bullets"]],
        "\nREADING GUIDE (how short chat lines should be written up):\n" + o["guide"],
        "\nMUST INCLUDE (if the chat mentions any of these, write the details in the email exactly as stated, including equipment names, bands, sectors, cells, counts and times; never leave them out, even when they are plans, access information or next steps):\n" + o["include"],
        f"\nSITE: {site} {stage}\nDATE: {fmt(d)}",
        f"\nRULES: Describe the work done on {fmt(d)} as shown in the chat for that day, together with access, lock and unlock information, plans to return to site and next steps mentioned that day. Never mention work from other days. Keep technical specifics (equipment, bands, sectors, cells, counts, values) exactly as written in the chat and never drop them. Use only facts found in the chat messages, the related emails, the attached photos and the voice-note notes below. The chat is the main source for what happened on site that day. Use the emails for approvals, access, deliveries, instructions, issues, handover and other details, and add them only where they matter to management. Never copy email signatures, greetings, names or phone numbers. Do not invent facts, but DO interpret short or shorthand chat lines like a site delivery coordinator would, using the READING GUIDE: turn each into a full completed-work sentence (for example \"on site now\" means the team attended the site). Read the messages in order: a problem followed by a fix means it was resolved. Mention photo content only when it clearly shows completed work or a problem. Ignore greetings and chit-chat. Never include phone numbers or people’s names.{ban}",
        "\nCHAT MESSAGES:\n" + ("\n".join(lines) or "(none)"),
        "\nRELATED OUTLOOK EMAILS (background from the UK team or client, use them together with the chat):\n" + mails,
        "\nVOICE-NOTE AND EXTRA NOTES (typed by the user):\n" + (voice or "(none)"),
        f"\nPHOTOS ATTACHED: {n_img}",
        '\nReturn JSON only: {"paragraph":"text after the site line, with a blank line then the second paragraph if needed","status":"On-Air | Completed | In progress | Pending | Issue | No update","pending":"short text or Nil","issues":"short text or Nil"}',
    ])


def pend_prompt(site, days_text, mails, d0, d1, o):
    ban = f" Never use these words or phrases: {'; '.join(o['banned'])}." if o["banned"] else ""
    return "\n".join([
        "You prepare a client-facing pending-items summary for a telecom site delivery team.",
        f"\nSITE: {site}\nDATES COVERED: {fmt(d0)} to {fmt(d1)}",
        "\nWHATSAPP CHAT PER DAY (oldest first):\n" + days_text,
        "\nRELATED OUTLOOK EMAILS:\n" + mails,
        "\nTASK: The site is worked on over several days. Go through the days in order from the first to the last. Find every task or item that was pending, blocked, awaited or left to complete on any day. Follow each pending item day by day until it is closed or the last day is reached: pending on day 1, check day 2, still pending on day 3, find out why it is still pending (the cause stated in the chat or emails). Mark each item Resolved (with the date it was closed) or Still pending. Then decide whether the site is closed: it is closed only when the chat or emails show the work is completed, handed over or closed out on the last day. Do not say that nothing is pending unless the chat confirms the closure; if the site is closed but an item has no closing information, list it as Still pending with the reason \"Closure not confirmed in the chat\".",
        f"\nRULES: Use only facts in the chat and emails and invent nothing. Give the reason in your own words (for example waiting for a delivery, access, approval, weather or another team); if no reason is stated write \"Reason not stated in the chat\". Never include people’s names, phone numbers or internal remarks. Use a neutral, professional tone suitable for the client.{ban}",
        "\nREADING GUIDE (how short chat lines should be read):\n" + o["guide"],
        '\nReturn JSON only: {"items":[{"item":"...","firstRaised":"date","status":"Resolved or Still pending","resolvedOn":"date or empty","reason":"...","nextAction":"..."}],"siteClosed":true or false,"closedOn":"date or empty","summary":"3 to 5 sentence client-ready progress summary for this site that shows how pending items carried from day to day and how they were closed, or why they are still pending"}',
    ])


# ---------------------------------------------------------------- app
def gate():
    code = secret("APP_PASSWORD")
    if not code:
        return
    if st.session_state.get("ok"):
        return
    st.title("📡 Site update desk")
    v = st.text_input("Access code", type="password")
    if v and v == code:
        st.session_state["ok"] = True
        st.rerun()
    if v:
        st.error("Wrong access code.")
    st.stop()


def main():
    gate()
    st.title("📡 Site update desk")
    st.caption("Upload WhatsApp site chats (with photos) and related emails. Get the daily site email, management report and client summary.")

    api_key = secret("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY is missing in the app secrets. See the setup steps.")
        st.stop()
    client = Anthropic(api_key=api_key)
    model = secret("MODEL", "claude-sonnet-5-5")

    # ---- sidebar settings
    with st.sidebar:
        st.header("Settings")
        dayfirst = st.selectbox("Date order in chat", ["DD/MM/YYYY (India, UK)", "MM/DD/YYYY (US)"]).startswith("DD")
        cut = st.selectbox("Day starts at (use 05:00 if late UK messages land on the next day)", [0, 3, 5, 6], format_func=lambda h: f"{h:02d}:00")
        banned = [x.strip() for x in st.text_area("Words or phrases to remove (one per line)", key="ban").splitlines() if x.strip()]
        rm_phone = st.checkbox("Remove phone numbers", True)
        same_day_mail = st.checkbox("Use emails from the selected day only", True)
        number = st.checkbox("Use “Site 1 :-” numbering (off = use the site name)", False)
        bullets = st.selectbox("Bullet points", list(BUL))
        show_date = st.checkbox("Show the date at the top of the email", True)
        greet = st.text_input("Greeting (empty for none)", "")
        sign = st.text_area("Sign-off", "Regards,", height=68)
        with st.expander("Email format example"):
            example = st.text_area("Claude copies this style", EX, height=300)
        with st.expander("How to read short chat messages"):
            guide = st.text_area("Add your own shorthand", GL, height=260)
        with st.expander("Always include these details"):
            include = st.text_area("Must appear if mentioned in the chat", INC, height=160)
    opts = dict(banned=banned, example=example, guide=guide, include=include, bullets=bullets, number=number)

    # ---- uploads
    st.subheader("1. Chats")
    chat_files = st.file_uploader("WhatsApp exports (.zip with media, or .txt). One file per site group.", type=["zip", "txt"], accept_multiple_files=True)
    st.caption("In WhatsApp: open the group → ⋮ → More → Export chat → Include media.")
    mail_files = st.file_uploader("Related Outlook emails (optional, .eml or .txt)", type=["eml", "txt"], accept_multiple_files=True, key="mails")
    pasted = st.text_area("Or paste email text (optional)", height=90)

    mails = [parse_mail(f.getvalue(), f.name) for f in mail_files]
    if pasted.strip():
        mails.append(parse_mail(pasted.encode(), "Pasted email"))

    sites = []
    for f in chat_files:
        data = f.getvalue()
        txt = read_chat(data, f.name)
        sites.append({"fn": f.name, "data": data, "is_zip": f.name.lower().endswith(".zip"), "txt": txt, "msgs": parse_chat(txt, dayfirst, cut)})
    if not sites:
        st.info("Upload one or more chat exports to begin.")
        return

    all_days = sorted({m["d"] for s in sites for m in s["msgs"]})
    if not all_days:
        st.warning("No messages found. Check the date order setting in the sidebar.")
        return

    st.subheader("2. Dates")
    c1, c2 = st.columns(2)
    d0 = c1.date_input("Report date", value=all_days[-1], key=f"d0_{len(sites)}")
    use_to = c2.checkbox("Also set a To date (one email per day)")
    d1 = c2.date_input("To date", value=d0, key=f"d1_{len(sites)}") if use_to else None

    st.subheader("3. Sites")
    sel = []
    for s in sites:
        imgs, auds, lines = day_data(s["msgs"], d0)
        with st.expander(f"{site_name(s['fn'])}  ·  {len(lines)} messages · {len(imgs)} photos · {len(auds)} voice notes on {fmt(d0)}", expanded=True):
            a, b, c = st.columns([1, 3, 2])
            use = a.checkbox("Include", True, key="use_" + s["fn"])
            s["name"] = b.text_input("Site line", site_name(s["fn"]), key="nm_" + s["fn"])
            s["stage"] = c.text_input("Stage", detect_stage(s["txt"]), key="st_" + s["fn"])
            s["voice"] = st.text_area("Voice notes can’t be read automatically. Type what they said (optional).", key="vo_" + s["fn"], height=68)
            if imgs and not s["is_zip"]:
                st.caption("Photos need a .zip export (Include media).")
            st.caption(f"Emails matched: {len(mails_for(s['name'], mails))}")
            with st.expander("See chat lines used"):
                st.text("\n".join(lines) or "(no messages on this date)")
        if use:
            sel.append(s)

    st.subheader("4. Create")
    b1, b2 = st.columns(2)
    go = b1.button("Create email", type="primary", disabled=not sel)
    gp = b2.button("Client summary (whole site)", disabled=not sel)

    if go:
        end = d1 if (use_to and d1 and d1 >= d0) else d0
        dts = [d0 + timedelta(days=i) for i in range((end - d0).days + 1)][:7]
        jobs = [(s, d) for d in dts for s in sel if len(dts) == 1 or day_data(s["msgs"], d)[0] or day_data(s["msgs"], d)[2]]
        if not jobs:
            st.warning("No messages found for these dates.")
        else:
            res, bar = {}, st.progress(0.0)
            for i, (s, d) in enumerate(jobs):
                bar.progress(i / len(jobs), text=f"Working on {s['name']} · {fmt(d)} ({i + 1} of {len(jobs)})")
                imgs, auds, lines = day_data(s["msgs"], d)
                photos = load_photos(s["data"], imgs) if s["is_zip"] else []
                prompt = day_prompt(s["name"], s["stage"] if len(dts) == 1 else "", d, [clean(x, banned, rm_phone) for x in lines],
                                    mail_block(mails_for(s["name"], mails, d, same_day_mail), banned, rm_phone), clean(s["voice"], banned, rm_phone), len(photos), opts)
                try:
                    r = ask(client, model, prompt, photos)
                except Exception as e:  # keep going with the other sites
                    r = {"paragraph": f"[Could not create this site: {e}]", "status": "Error", "pending": "", "issues": ""}
                res.setdefault(d, []).append((s, r))
            bar.empty()
            days = []
            for d, rows in res.items():
                parts = []
                for i, (s, r) in enumerate(rows):
                    pre = f"Site {i + 1} :- " if number else ""
                    stage = f" {s['stage']}" if s["stage"] and len(dts) == 1 else ""
                    parts.append(f"{pre}{s['name']}{stage} : {clean(r.get('paragraph', ''), banned, rm_phone)}")
                body = (greet + "\n\n" if greet else "") + (f"Date: {fmt(d)}\n\n" if show_date else "") + "\n\n".join(parts) + ("\n\n" + sign if sign else "")
                table = [{"Site": s["name"], "Status": r.get("status", ""), "Pending": clean(str(r.get("pending", "Nil")), banned, rm_phone), "Issues / Errors": clean(str(r.get("issues", "Nil")), banned, rm_phone)} for s, r in rows]
                days.append({"d": d, "body": body, "table": table})
            st.session_state["out"] = {"kind": "daily", "days": days, "multi": len(dts) > 1, "n": st.session_state.get("n", 0) + 1}
            st.session_state["n"] = st.session_state["out"]["n"]

    if gp:
        res, bar = [], st.progress(0.0)
        for i, s in enumerate(sel):
            bar.progress(i / len(sel), text=f"Tracking pending items: {s['name']} ({i + 1} of {len(sel)})")
            days = sorted({m["d"] for m in s["msgs"] if (d0 <= m["d"] <= d1 if (use_to and d1) else m["d"] <= d0)})
            if not days:
                res.append((s, {"items": [], "siteClosed": False, "summary": "No chat messages in this period."}, None, None))
                continue
            cap = 60000 // len(days)
            days_text = "\n\n".join(f"{fmt(d)}:\n" + ("\n".join(clean(x, banned, rm_phone) for x in day_data(s["msgs"], d)[2])[:cap] or "(no messages)") for d in days)
            prompt = pend_prompt(s["name"], days_text, mail_block(mails_for(s["name"], mails, None, False), banned, rm_phone, 2000), days[0], days[-1], opts)
            try:
                r = ask(client, model, prompt, None, 4000)
            except Exception as e:
                r = {"items": [], "siteClosed": False, "summary": f"[Could not create this site: {e}]"}
            res.append((s, r, days[0], days[-1]))
        bar.empty()
        spans = [x for _, _, a, b2_ in res for x in (a, b2_) if x]
        rng = f"{fmt(min(spans))} to {fmt(max(spans))}" if spans else ""
        blocks, table = [], []
        for s, r, _, _ in res:
            items = r.get("items") or []
            openi = [x for x in items if "still" in str(x.get("status", "")).lower()]
            done = [x for x in items if x not in openi]
            t = s["name"] + "\n" + clean(r.get("summary", ""), banned, rm_phone)
            if openi:
                t += "\nStill pending:\n" + "\n".join(f"• {clean(x.get('item', ''), banned, rm_phone)} – pending since {x.get('firstRaised', '')}. Reason: {clean(x.get('reason', ''), banned, rm_phone)}. Next action: {clean(x.get('nextAction', ''), banned, rm_phone)}." for x in openi)
            if r.get("siteClosed") and not openi:
                t += f"\nStatus: Closed{' on ' + str(r['closedOn']) if r.get('closedOn') else ''}. No items are pending."
            if done:
                t += "\nItems closed out:\n" + "\n".join(f"• {clean(x.get('item', ''), banned, rm_phone)} – raised {x.get('firstRaised', '')}, closed {x.get('resolvedOn', '')}" + (f". Cause of delay: {clean(x['reason'], banned, rm_phone)}" if x.get("reason") and "not stated" not in x["reason"].lower() else "") + "." for x in done)
            blocks.append(t)
            table += [{"Site": s["name"], "Item": clean(x.get("item", ""), banned, rm_phone), "First raised": x.get("firstRaised", ""), "Status": x.get("status", "") + (f" ({x['resolvedOn']})" if x.get("resolvedOn") else ""), "Reason": clean(x.get("reason", ""), banned, rm_phone), "Next action": clean(x.get("nextAction", ""), banned, rm_phone)} for x in items]
        body = (greet + "\n\n" if greet else "") + f"Please find the pending items summary for {rng}.\n\n" + "\n\n".join(blocks) + ("\n\n" + sign if sign else "")
        st.session_state["out"] = {"kind": "pend", "body": body, "table": table, "rng": rng, "n": st.session_state.get("n", 0) + 1}
        st.session_state["n"] = st.session_state["out"]["n"]

    show(st.session_state.get("out"))


def show(out):
    if not out:
        return
    n = out["n"]
    st.divider()
    if out["kind"] == "daily":
        for blk in out["days"]:
            d = blk["d"]
            st.subheader(f"Email for management – {fmt(d)}" if out["multi"] else "Email for management")
            st.text_input("Subject", f"Daily Site Update – {fmt(d)}", key=f"sub_{n}_{d}")
            body = st.text_area("Email body (you can edit it)", blk["body"], height=min(720, 120 + 24 * blk["body"].count("\n")), key=f"body_{n}_{d}")
            st.download_button("Download email (.txt)", body, file_name=f"site-update-{d}.txt", key=f"dl_{n}_{d}")
            with st.expander("Copy view (use the copy icon)"):
                st.code(body, language=None)
            st.markdown(f"**Management report – {fmt(d)}**")
            df = pd.DataFrame(blk["table"])
            st.dataframe(df, width="stretch", hide_index=True)
            st.download_button("Download table (.csv)", df.to_csv(index=False), file_name=f"report-{d}.csv", key=f"csv_{n}_{d}")
    else:
        st.subheader(f"Client summary – {out['rng']}")
        st.text_input("Subject", f"Pending Items Summary – {out['rng']}", key=f"psub_{n}")
        body = st.text_area("Client email (you can edit it)", out["body"], height=min(720, 120 + 24 * out["body"].count("\n")), key=f"pbody_{n}")
        st.download_button("Download email (.txt)", body, file_name="client-summary.txt", key=f"pdl_{n}")
        with st.expander("Copy view (use the copy icon)"):
            st.code(body, language=None)
        title = st.text_input("Table title", "Pending tracker", key="ptitle")
        st.markdown(f"**{title}**")
        df = pd.DataFrame(out["table"])
        st.dataframe(df, width="stretch", hide_index=True)
        st.download_button("Download table (.csv)", df.to_csv(index=False), file_name="pending-tracker.csv", key=f"pcsv_{n}")


if __name__ == "__main__":
    main()
