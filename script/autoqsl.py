#!/usr/bin/env python3
"""
AutoQSL — automatic e-mail QSL cards for WSJT-X (FT8/FT4 etc.)

Listens to WSJT-X UDP messages; every time you press "Log QSO",
the contact is queued, the recipient's e-mail is looked up on QRZ.com and/or HamQTH,
a QSL card image is drawn and sent from your Gmail after a delay.

Usage:
    python3 autoqsl.py                 # run (listen to WSJT-X)
    python3 autoqsl.py --preview UR5XXX  # draw a sample card -> preview.png
    python3 autoqsl.py --test-email    # send a sample card to yourself
    python3 autoqsl.py --status        # show the queue / sent log
    python3 autoqsl.py --lookup DL1ABC # check which e-mail would be used
"""

import argparse
import configparser
import datetime as dt
import os
import re
import smtplib
import socket
import sqlite3
import struct
import sys
import time
import xml.etree.ElementTree as ET
from email.message import EmailMessage

import requests
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.ini")
DB_PATH = os.path.join(BASE, "autoqsl.db")
CARDS_DIR = os.path.join(BASE, "cards")
WSJTX_MAGIC = 0xADBCCBDA


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- config
def load_config():
    if not os.path.exists(CFG_PATH):
        sys.exit(f"config.ini not found next to the script: {CFG_PATH}")
    cfg = configparser.ConfigParser()
    cfg.read(CFG_PATH, encoding="utf-8")
    return cfg


# ---------------------------------------------------------------- database
def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS qso (
        id INTEGER PRIMARY KEY,
        call TEXT, band TEXT, mode TEXT, qso_date TEXT, time_on TEXT,
        freq TEXT, rst_sent TEXT, rst_rcvd TEXT, grid TEXT, name TEXT,
        logged_at REAL, status TEXT, email TEXT, note TEXT)""")
    return con


# ---------------------------------------------------------------- WSJT-X UDP
def _read_utf8(buf, pos):
    (n,) = struct.unpack_from(">I", buf, pos)
    pos += 4
    if n == 0xFFFFFFFF:
        return None, pos
    return buf[pos:pos + n].decode("utf-8", "replace"), pos + n


def parse_wsjtx(packet):
    """Return ADIF text from a WSJT-X 'Logged ADIF' (type 12) message,
    or from a plain-text ADIF packet (WSJT-X 'Secondary UDP Server')."""
    if len(packet) < 12:
        return None
    if packet[:4] != b"\xad\xbc\xcb\xda":
        text = packet.decode("utf-8", "replace")
        return text if re.search(r"<call:\d+", text, re.I) else None
    magic, _schema, mtype = struct.unpack_from(">III", packet, 0)
    if magic != WSJTX_MAGIC or mtype != 12:
        return None
    _id, pos = _read_utf8(packet, 12)
    adif, _ = _read_utf8(packet, pos)
    return adif


def parse_adif(text):
    """Parse the first ADIF record into a dict with upper-case keys."""
    if "<EOH>" in text.upper():
        text = text[text.upper().index("<EOH>") + 5:]
    rec, pos = {}, 0
    for m in re.finditer(r"<(\w+):(\d+)(?::\w)?>", text):
        if m.start() < pos:
            continue
        name, length = m.group(1).upper(), int(m.group(2))
        rec[name] = text[m.end():m.end() + length].strip()
        pos = m.end() + length
    return rec


# ---------------------------------------------------------------- HamQTH
class HamQTH:
    URL = "https://www.hamqth.com/xml.php"

    def __init__(self, user, password):
        self.user, self.password = user, password
        self.session, self.session_time = None, 0

    @staticmethod
    def _find(root, tag):
        for el in root.iter():
            if el.tag.split("}")[-1] == tag:
                return (el.text or "").strip()
        return ""

    def _login(self):
        r = requests.get(self.URL, params={"u": self.user, "p": self.password}, timeout=15)
        root = ET.fromstring(r.content)
        sid = self._find(root, "session_id")
        if not sid:
            raise RuntimeError("HamQTH login failed: " + (self._find(root, "error") or "unknown"))
        self.session, self.session_time = sid, time.time()

    def email(self, call):
        if not self.session or time.time() - self.session_time > 50 * 60:
            self._login()
        r = requests.get(self.URL, params={"id": self.session, "callsign": call,
                                           "prg": "AutoQSL"}, timeout=15)
        root = ET.fromstring(r.content)
        err = self._find(root, "error")
        if err and "session" in err.lower():
            self.session = None
            return self.email(call)
        return self._find(root, "email") or None


class QRZ:
    URL = "https://xmldata.qrz.com/xml/current/"

    def __init__(self, user, password):
        self.user, self.password = user, password
        self.key = None

    def _get(self, params):
        r = requests.get(self.URL, params=params, timeout=15)
        return ET.fromstring(r.content)

    def _login(self):
        root = self._get({"username": self.user, "password": self.password,
                          "agent": "AutoQSL1.1"})
        key = HamQTH._find(root, "Key")
        if not key:
            raise RuntimeError("QRZ login failed: " + (HamQTH._find(root, "Error") or "unknown"))
        if HamQTH._find(root, "SubExp").lower() == "non-subscriber":
            log("QRZ: no XML subscription - e-mails will usually be hidden, "
                "HamQTH will be used as fallback")
        self.key = key

    def email(self, call, _retry=True):
        if not self.key:
            self._login()
        root = self._get({"s": self.key, "callsign": call})
        err = HamQTH._find(root, "Error")
        if err and ("session" in err.lower() or "timeout" in err.lower()) and _retry:
            self.key = None
            return self.email(call, _retry=False)
        return HamQTH._find(root, "email") or None


class Lookup:
    """Try QRZ first, then HamQTH. Each source is optional."""

    def __init__(self, cfg):
        self.sources = []
        for sec, cls in (("qrz", QRZ), ("hamqth", HamQTH)):
            if cfg.has_section(sec) and cfg[sec].get("user", "").strip() \
                    and "xxx" not in cfg[sec].get("user", "").lower():
                self.sources.append((sec, cls(cfg[sec]["user"].strip(),
                                              cfg[sec]["password"].strip())))
        if not self.sources:
            sys.exit("Fill in [qrz] and/or [hamqth] in config.ini")

    def email(self, call):
        errors = []
        for name, src in self.sources:
            try:
                e = src.email(call)
                if e:
                    return e, name
            except Exception as ex:
                errors.append(f"{name}: {ex}")
        if errors and len(errors) == len(self.sources):
            raise RuntimeError("; ".join(errors))
        return None, None


# ---------------------------------------------------------------- card
FONT_CANDIDATES = {
    True: ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
           "/Library/Fonts/Arial Bold.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    False: ["/System/Library/Fonts/Supplemental/Arial.ttf",
            "/Library/Fonts/Arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
}


def font(size, bold=False):
    for path in FONT_CANDIDATES[bold]:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def shadow_text(draw, xy, text, fnt, fill="white", anchor="la"):
    x, y = xy
    for dx, dy in ((2, 2), (3, 3)):
        draw.text((x + dx, y + dy), text, font=fnt, fill=(0, 0, 0, 180), anchor=anchor)
    draw.text((x, y), text, font=fnt, fill=fill, anchor=anchor)


def _fmt(q):
    date = q["qso_date"]
    date_fmt = f"{date[0:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date
    t = q["time_on"]
    time_fmt = f"{t[0:2]}:{t[2:4]}" if len(t) >= 4 else t
    try:
        freq_fmt = f"{float(q['freq']):.3f} MHz"
    except (TypeError, ValueError):
        freq_fmt = q["freq"] or "-"
    return [("DATE", date_fmt), ("UTC", time_fmt), ("BAND", q["band"] or "-"),
            ("MODE", q["mode"] or "-"), ("FREQ", freq_fmt), ("RST", q["rst_sent"] or "-")]


def _load_background(cfg):
    bg_path = cfg["card"].get("background", "").strip()
    if bg_path and not os.path.isabs(bg_path):
        bg_path = os.path.join(BASE, bg_path)
    if bg_path and os.path.exists(bg_path):
        img = Image.open(bg_path).convert("RGB")
        W = 1800  # keep the picture's own proportions
        return img.resize((W, round(img.height * W / img.width)), Image.LANCZOS)
    if bg_path:
        log(f"WARNING: background picture not found: {bg_path} - using plain card")
    return None


def make_card(cfg, q, out_path):
    bg = _load_background(cfg)
    style = cfg["card"].get("style", "overlay" if bg else "full").strip().lower()
    if bg is not None and style == "overlay":
        img = _card_overlay(cfg, q, bg)
    else:
        img = _card_full(cfg, q, bg)
    img.convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path


def _card_overlay(cfg, q, img):
    """Your own finished card design: only a QSO info panel is added."""
    W, H = img.size
    c = cfg["card"]
    fx, fy, fw, fh = [float(v) for v in c.get("panel", "0.035, 0.37, 0.45, 0.255").split(",")]
    x0, y0, pw, ph = int(fx * W), int(fy * H), int(fw * W), int(fh * H)
    opacity = int(255 * c.getfloat("panel_opacity", 0.62))
    accent = c.get("accent_color", "#FFD500").strip()

    img = img.convert("RGBA")
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle((x0, y0, x0 + pw, y0 + ph), radius=int(ph * 0.08),
                         fill=(8, 12, 24, opacity), outline=(255, 255, 255, 60), width=2)
    img = Image.alpha_composite(img, ov)
    d = ImageDraw.Draw(img)

    pad = int(pw * 0.05)
    u = ph / 100  # vertical unit
    d.text((x0 + pad, y0 + 9 * u), "CONFIRMING QSO WITH", font=font(int(7.5 * u), True),
           fill=(200, 210, 230))
    d.text((x0 + pad, y0 + 19 * u), q["call"], font=font(int(21 * u), True), fill=accent)

    cols = _fmt(q)
    colw = (pw - 2 * pad) / 3
    for i, (h, v) in enumerate(cols):
        r, k = divmod(i, 3)
        cx = x0 + pad + colw * k
        cy = y0 + (50 if r == 0 else 74) * u
        d.text((cx, cy), h, font=font(int(6.5 * u), True), fill=(170, 185, 210))
        d.text((cx, cy + 8 * u), v, font=font(int(10 * u), True), fill="white")
    return img


def _card_full(cfg, q, bg):
    """Generated card: callsign, name and QSO details drawn by the script."""
    W, H = 1400, 900
    if bg is not None:
        scale = max(W / bg.width, H / bg.height)
        img = bg.resize((int(bg.width * scale) + 1, int(bg.height * scale) + 1))
        l, t = (img.width - W) // 2, (img.height - H) // 2
        img = img.crop((l, t, l + W, t + H))
    else:
        img = Image.new("RGB", (W, H))
        d = ImageDraw.Draw(img)
        for y in range(H):  # simple dark-blue gradient
            k = y / H
            d.line([(0, y), (W, y)], fill=(int(15 + 20 * k), int(30 + 40 * k), int(70 + 60 * k)))

    img = img.convert("RGBA")
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle((60, 520, W - 60, H - 50), radius=24, fill=(0, 0, 0, 150))
    img = Image.alpha_composite(img, overlay)
    d = ImageDraw.Draw(img)

    st = cfg["station"]
    shadow_text(d, (70, 50), st["callsign"].upper(), font(150, True))
    sub = " · ".join(x for x in (st.get("name", ""), st.get("qth", ""),
                                 st.get("grid", "")) if x)
    shadow_text(d, (78, 225), sub, font(44))
    shadow_text(d, (90, 545), f"To radio: {q['call']}", font(56, True), fill=(255, 200, 80))

    cols = _fmt(q)
    x0, colw = 90, (W - 180) / len(cols)
    for i, (h, v) in enumerate(cols):
        cx = x0 + colw * i + colw / 2
        d.text((cx, 650), h, font=font(30, True), fill=(180, 200, 230), anchor="ma")
        d.text((cx, 695), v, font=font(34, True), fill="white", anchor="ma")
    d.text((W / 2, 790), st.get("message", "Thanks for the QSO! 73"),
           font=font(36), fill=(230, 230, 230), anchor="ma")
    return img


# ---------------------------------------------------------------- e-mail
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def nice_date(d):
    """20261018 -> 18th of October 2026"""
    try:
        y, m, day = int(d[0:4]), int(d[4:6]), int(d[6:8])
    except (ValueError, IndexError):
        return d
    suf = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suf} of {MONTHS[m - 1]} {y}"


def email_body(cfg, q):
    my = cfg["station"]["callsign"].upper()
    t = q["time_on"]
    utc = f"{t[0:2]}:{t[2:4]}" if len(t) >= 4 else t
    lines = [f"Dear {q['name'] or q['call']},", "",
             f"Thank you very much for the QSO on {nice_date(q['qso_date'])} "
             f"at {utc} UTC on {q['band']} {q['mode']}.",
             "Please find my QSL card attached.", ""]
    qsl = cfg["qsl"] if cfg.has_section("qsl") else {}
    if qsl and cfg["qsl"].getboolean("ask_paper_qsl", True):
        lines += ["I would be truly grateful to receive your paper QSL card in return. "
                  "It means a great deal to me - a paper card is something very special "
                  "and symbolic for me.", ""]
        addr = [a.strip() for a in qsl.get("postal_address", "").strip().splitlines() if a.strip()]
        if addr:
            lines += ["My address for direct QSL:", ""] + ["    " + a for a in addr] + [""]
    lines += [f"73 de {my}"]
    return "\n".join(lines) + "\n"


def send_mail(cfg, to_addr, q, card_path):
    g, st = cfg["gmail"], cfg["station"]
    my = st["callsign"].upper()
    msg = EmailMessage()
    msg["From"] = f"{my} <{g['address']}>"
    msg["To"] = to_addr
    msg["Subject"] = f"QSL {my} - {q['call']} {q['band']} {q['mode']}"
    body = email_body(cfg, q)
    msg.set_content(body)
    with open(card_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="image", subtype="jpeg",
                           filename=f"QSL_{my}_{q['call']}.jpg".replace("/", "-"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(g["address"], g["app_password"].replace(" ", ""))
        s.send_message(msg)


# ---------------------------------------------------------------- queue
COLS = ["call", "band", "mode", "qso_date", "time_on", "freq",
        "rst_sent", "rst_rcvd", "grid", "name"]


# suffixes like /P, /M, /QRP, /R (rover) say nothing about the country
NEUTRAL_PARTS = {"P", "M", "MM", "AM", "QRP", "R", "A", "B", "LH", "LGT"}
DEFAULT_BLOCKED = "R, UA, UB, UC, UD, UE, UF, UG, UH, UI"


def is_blocked(cfg, call):
    """True if any meaningful part of the callsign starts with a blocked prefix.
    UA/DL1ABC, DL/UA3ABC and R7XX/P are all caught; K1ABC/R is not."""
    raw = cfg["filter"].get("block_prefixes", DEFAULT_BLOCKED) \
        if cfg.has_section("filter") else DEFAULT_BLOCKED
    prefixes = [p.strip().upper() for p in raw.split(",") if p.strip()]
    for part in call.upper().split("/"):
        if not part or part in NEUTRAL_PARTS or part.isdigit():
            continue
        if any(part.startswith(p) for p in prefixes):
            return True
    return False


def enqueue(con, rec, cfg=None):
    q = {
        "call": rec.get("CALL", "").upper(),
        "band": rec.get("BAND", "").lower(),
        "mode": rec.get("SUBMODE") or rec.get("MODE", ""),
        "qso_date": rec.get("QSO_DATE", ""),
        "time_on": rec.get("TIME_ON", ""),
        "freq": rec.get("FREQ", ""),
        "rst_sent": rec.get("RST_SENT", ""),
        "rst_rcvd": rec.get("RST_RCVD", ""),
        "grid": rec.get("GRIDSQUARE", ""),
        "name": rec.get("NAME", ""),
    }
    if not q["call"]:
        return
    blocked = cfg is not None and is_blocked(cfg, q["call"])
    con.execute(f"INSERT INTO qso ({','.join(COLS)}, logged_at, status, note) "
                f"VALUES ({','.join('?' * len(COLS))}, ?, ?, ?)",
                [q[c] for c in COLS] + [time.time(),
                 "blocked" if blocked else "pending",
                 "blocked prefix" if blocked else None])
    con.commit()
    if blocked:
        log(f"QSO logged: {q['call']} {q['band']} {q['mode']} -> BLOCKED, no QSL will be sent")
    else:
        log(f"QSO logged: {q['call']} {q['band']} {q['mode']} -> queued")


def process_queue(con, cfg, lookup):
    opts = cfg["options"]
    delay = opts.getfloat("delay_minutes", 30) * 60
    dedup = opts.get("dedup", "band_mode").strip()
    max_day = opts.getint("max_per_day", 300)
    rows = con.execute("SELECT id," + ",".join(COLS) + " FROM qso "
                       "WHERE status='pending' AND logged_at <= ?",
                       (time.time() - delay,)).fetchall()
    for row in rows:
        qid, q = row[0], dict(zip(COLS, row[1:]))

        def done(status, email=None, note=""):
            con.execute("UPDATE qso SET status=?, email=?, note=? WHERE id=?",
                        (status, email, note, qid))
            con.commit()

        if is_blocked(cfg, q["call"]):
            done("blocked", note="blocked prefix")
            log(f"{q['call']}: blocked prefix, skipped")
            continue

        # duplicate check
        if dedup == "call":
            where, args = "call=?", (q["call"],)
        elif dedup == "band_mode":
            where, args = "call=? AND band=? AND mode=?", (q["call"], q["band"], q["mode"])
        else:
            where, args = None, ()
        if where and con.execute(f"SELECT 1 FROM qso WHERE status='sent' AND {where}",
                                 args).fetchone():
            done("duplicate", note="card already sent earlier")
            log(f"{q['call']}: duplicate, skipped")
            continue

        sent_today = con.execute("SELECT COUNT(*) FROM qso WHERE status='sent' "
                                 "AND logged_at > ?", (time.time() - 86400,)).fetchone()[0]
        if sent_today >= max_day:
            log("Daily limit reached, will continue later")
            return

        base_call = max(q["call"].split("/"), key=len)  # UR5XXX/P -> UR5XXX
        try:
            email, source = lookup.email(base_call)
        except Exception as e:
            log(f"{q['call']}: lookup error ({e}), will retry later")
            return
        if not email:
            done("no_email", note="no public e-mail on QRZ/HamQTH")
            log(f"{q['call']}: no e-mail found, skipped")
            continue

        try:
            os.makedirs(CARDS_DIR, exist_ok=True)
            path = os.path.join(CARDS_DIR, f"{q['qso_date']}_{q['time_on'][:4]}_"
                                           f"{q['call'].replace('/', '-')}.jpg")
            make_card(cfg, q, path)
            send_mail(cfg, email, q, path)
            done("sent", email, note=f"e-mail from {source}")
            log(f"{q['call']}: QSL sent to {email} (from {source})")
        except smtplib.SMTPAuthenticationError:
            sys.exit("Gmail rejected the login. Check address and app password in config.ini")
        except Exception as e:
            log(f"{q['call']}: sending failed ({e}), will retry later")
            return


# ---------------------------------------------------------------- listener
def open_socket(cfg):
    host = cfg["wsjtx"].get("address", "127.0.0.1").strip()
    port = cfg["wsjtx"].getint("port", 2237)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    first = int(host.split(".")[0])
    if 224 <= first <= 239:  # multicast: can share with GridTracker/JTAlert
        s.bind(("", port))
        mreq = struct.pack("4s4s", socket.inet_aton(host), socket.inet_aton("0.0.0.0"))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    else:
        s.bind((host, port))
    s.settimeout(5)
    log(f"Listening for WSJT-X on {host}:{port}")
    return s


def run(cfg):
    con = db()
    hq = Lookup(cfg)
    sock = open_socket(cfg)
    last_check = 0
    while True:
        try:
            data, _ = sock.recvfrom(65535)
            adif = parse_wsjtx(data)
            if adif:
                enqueue(con, parse_adif(adif), cfg)
        except socket.timeout:
            pass
        if time.time() - last_check > 60:
            last_check = time.time()
            process_queue(con, cfg, hq)


# ---------------------------------------------------------------- main
SAMPLE = {"call": "UR5XXX", "band": "20m", "mode": "FT8", "qso_date":
          dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d"), "time_on":
          dt.datetime.now(dt.timezone.utc).strftime("%H%M%S"), "freq": "14.074",
          "rst_sent": "-08", "rst_rcvd": "-12", "grid": "KN89", "name": ""}


def main():
    ap = argparse.ArgumentParser(description="Automatic e-mail QSL for WSJT-X")
    ap.add_argument("--preview", metavar="CALL", help="draw a sample card to preview.jpg")
    ap.add_argument("--test-email", action="store_true", help="send a sample card to yourself")
    ap.add_argument("--status", action="store_true", help="show the last 30 QSOs")
    ap.add_argument("--lookup", metavar="CALL", help="test e-mail lookup for a callsign")
    ap.add_argument("--email-text", action="store_true", help="show the e-mail text")
    a = ap.parse_args()
    cfg = load_config()

    if a.preview:
        q = dict(SAMPLE, call=a.preview.upper())
        log("Card saved: " + make_card(cfg, q, os.path.join(BASE, "preview.jpg")))
    elif a.test_email:
        q = dict(SAMPLE, call=cfg["station"]["callsign"].upper())
        path = make_card(cfg, q, os.path.join(BASE, "preview.jpg"))
        send_mail(cfg, cfg["gmail"]["address"], q, path)
        log("Test card sent to " + cfg["gmail"]["address"])
    elif a.email_text:
        print(email_body(cfg, dict(SAMPLE, call="US1MM", qso_date="20261018", time_on="150630")))
    elif a.lookup:
        email, src = Lookup(cfg).email(a.lookup.upper())
        print(f"{a.lookup.upper()}: {email or 'no e-mail found'}" + (f" (from {src})" if src else ""))
    elif a.status:
        for r in db().execute("SELECT qso_date,time_on,call,band,mode,status,"
                              "COALESCE(email,''),COALESCE(note,'') FROM qso "
                              "ORDER BY id DESC LIMIT 30"):
            print(" | ".join(str(x) for x in r))
    else:
        try:
            run(cfg)
        except KeyboardInterrupt:
            log("Stopped")


if __name__ == "__main__":
    main()