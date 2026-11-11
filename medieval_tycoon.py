"""
Medieval Idle - a city / army / crafts idle game.
Run:      python medieval_idle.py
Balance:  python medieval_idle.py sim            (idle economy simulation)
          python medieval_idle.py sim active     (bot that clicks + uses the decree)
"""
import json
import os
import time

# ----------------------------------------------------------------- config
SAVE = "save.json"
COST_GROWTH = 1.15
OFFLINE_CAP = 8 * 3600        # max offline progress (seconds)
OFFLINE_STEP = 10             # offline simulation step (seconds)
WAVE_INTERVAL = 300           # seconds between enemy waves (live play only)
BASE_GOLD_INCOME = 0.2        # passive tax, prevents a dead-end start
SMITH_MAX = 10
GATHER_CD = 1.0               # seconds between manual gathers
GATHER_FLAT = 0.5             # flat part of a gather
GATHER_SECONDS = 0.3          # + this many seconds of production per gather
GATHERABLE = ["gold", "food", "wood", "iron"]
DECREE_MULT = 2.0             # Royal Decree: production speed multiplier
DECREE_DURATION = 30          # seconds the boost lasts
DECREE_COOLDOWN = 180         # seconds, counted from activation (also offline)
RESOURCES = ["gold", "food", "wood", "iron", "weapons"]

# ------------------------------------------------------------------- data
BUILDINGS = {
    "farm":        {"cost": {"gold": 10},              "in": {},             "out": {"food": 1.0}},
    "lumber_camp": {"cost": {"gold": 15},              "in": {},             "out": {"wood": 0.8}},
    "market":      {"cost": {"wood": 30},              "in": {"food": 0.5},  "out": {"gold": 1.5}},
    "iron_mine":   {"cost": {"gold": 40, "wood": 20},  "in": {"food": 0.2},  "out": {"iron": 0.5}},
    "blacksmith":  {"cost": {"gold": 100, "wood": 50}, "in": {"iron": 0.4},  "out": {"weapons": 0.2}},
}

UNITS = {
    "militia": {"cost": {"food": 20},                "power": 1},
    "archer":  {"cost": {"food": 30, "wood": 20},    "power": 3},
    "knight":  {"cost": {"food": 60, "weapons": 10}, "power": 10},
}


# ----------------------------------------------------------------- engine
def new_state():
    return {
        "res": {"gold": 30.0, "food": 0.0, "wood": 0.0, "iron": 0.0, "weapons": 0.0},
        "buildings": {b: 0 for b in BUILDINGS},
        "units": {u: 0 for u in UNITS},
        "smith_level": 0,
        "clock": 0.0,
        "next_wave": float(WAVE_INTERVAL),
        "wave": 0,
        "last": time.time(),
        "last_gather": 0.0,
        "decree_left": 0.0,
        "decree_cd": 0.0,
        "log": ["Your kingdom begins. Build a farm first."],
    }


def scaled_cost(base, owned):
    return {r: v * COST_GROWTH ** owned for r, v in base.items()}


def smith_cost(level):
    return {"gold": 200 * 2 ** level, "iron": 50 * 2 ** level}


def can_afford(res, cost):
    return all(res[r] >= v for r, v in cost.items())


def pay(res, cost):
    for r, v in cost.items():
        res[r] -= v


def smith_mult(s):
    return 1 + 0.25 * s["smith_level"]


def army_power(s):
    base = sum(UNITS[u]["power"] * n for u, n in s["units"].items())
    return base * (1 + 0.1 * s["smith_level"])


def enemy_power(wave):
    return 10 * 1.3 ** wave


def net_rates(s):
    """Theoretical net production per second (assumes all inputs available)."""
    rates = {r: 0.0 for r in RESOURCES}
    rates["gold"] += BASE_GOLD_INCOME
    for name, count in s["buildings"].items():
        b = BUILDINGS[name]
        mult = smith_mult(s) if name == "blacksmith" else 1
        for r, v in b["out"].items():
            rates[r] += v * count * mult
        for r, v in b["in"].items():
            rates[r] -= v * count
    if s["decree_left"] > 0:
        rates = {r: v * DECREE_MULT for r, v in rates.items()}
    return rates


def gross_rate(s, r):
    """Production per second of resource r, ignoring consumption."""
    total = BASE_GOLD_INCOME if r == "gold" else 0.0
    for name, count in s["buildings"].items():
        mult = smith_mult(s) if name == "blacksmith" else 1
        total += BUILDINGS[name]["out"].get(r, 0.0) * count * mult
    return total


def gather_gain(s, r):
    gain = GATHER_FLAT + GATHER_SECONDS * gross_rate(s, r)
    return gain * (DECREE_MULT if s["decree_left"] > 0 else 1)


def gather(s, r):
    now = time.time()
    if s["last_gather"] > now:           # clock set backwards
        s["last_gather"] = 0.0
    if now - s["last_gather"] < GATHER_CD:
        return False
    update(s)
    s["res"][r] += gather_gain(s, r)
    s["last_gather"] = now
    return True


def use_decree(s):
    update(s)
    if s["decree_cd"] > 0:
        return False
    s["decree_left"] = float(DECREE_DURATION)
    s["decree_cd"] = float(DECREE_COOLDOWN)
    s["log"].append(f"Royal Decree issued! Production x{DECREE_MULT:.0f} for {DECREE_DURATION}s.")
    return True


def check_wave(s):
    while s["clock"] >= s["next_wave"]:
        w = s["wave"]
        enemy, mine = enemy_power(w), army_power(s)
        if mine >= enemy:
            reward = 50 * (w + 1)
            s["res"]["gold"] += reward
            s["log"].append(f"Wave {w + 1} repelled! ({mine:.0f} vs {enemy:.0f}) +{reward} gold")
        else:
            for u in s["units"]:
                s["units"][u] = int(s["units"][u] * 0.7)
            s["log"].append(f"Wave {w + 1} broke through! ({mine:.0f} vs {enemy:.0f}) You lost 30% of your army.")
        s["wave"] += 1
        s["next_wave"] += WAVE_INTERVAL


def tick(s, dt, waves=True):
    if dt <= 0:
        return
    boosted = min(dt, s["decree_left"])
    eff = dt + (DECREE_MULT - 1) * boosted      # effective production time
    s["decree_left"] = max(0.0, s["decree_left"] - dt)
    s["decree_cd"] = max(0.0, s["decree_cd"] - dt)
    s["res"]["gold"] += BASE_GOLD_INCOME * eff
    for name, count in s["buildings"].items():
        if count == 0:
            continue
        b = BUILDINGS[name]
        ratio = 1.0
        for r, v in b["in"].items():
            need = v * count * eff
            if need > 0:
                ratio = min(ratio, s["res"][r] / need)
        ratio = max(0.0, min(1.0, ratio))
        for r, v in b["in"].items():
            s["res"][r] -= v * count * eff * ratio
        mult = smith_mult(s) if name == "blacksmith" else 1
        for r, v in b["out"].items():
            s["res"][r] += v * count * eff * ratio * mult
    if waves:
        s["clock"] += dt
        check_wave(s)


def update(s):
    now = time.time()
    dt = now - s["last"]
    s["last"] = now
    if dt < 0:               # clock set backwards: no gain, no loss
        return
    if dt > 5:               # treated as offline (or a long freeze)
        dt = min(dt, OFFLINE_CAP)
        for _ in range(int(dt // OFFLINE_STEP)):
            tick(s, OFFLINE_STEP, waves=False)
        tick(s, dt % OFFLINE_STEP, waves=False)
        s["log"].append(f"Welcome back! Simulated {fmt_time(dt)} offline (waves paused).")
    else:
        tick(s, dt)


def build(s, name):
    update(s)
    c = scaled_cost(BUILDINGS[name]["cost"], s["buildings"][name])
    if can_afford(s["res"], c):
        pay(s["res"], c)
        s["buildings"][name] += 1
        return True
    return False


def recruit(s, name):
    update(s)
    c = UNITS[name]["cost"]
    if can_afford(s["res"], c):
        pay(s["res"], c)
        s["units"][name] += 1
        return True
    return False


def upgrade_smith(s):
    update(s)
    if s["smith_level"] >= SMITH_MAX or s["buildings"]["blacksmith"] == 0:
        return False
    c = smith_cost(s["smith_level"])
    if can_afford(s["res"], c):
        pay(s["res"], c)
        s["smith_level"] += 1
        return True
    return False


# --------------------------------------------------------- save / load
def save(s):
    s["log"] = s["log"][-50:]
    tmp = SAVE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, SAVE)    # atomic: a crash mid-write can't corrupt the save


def load():
    base = new_state()
    if not os.path.exists(SAVE):
        return base
    try:
        with open(SAVE) as f:
            s = json.load(f)
        for k in ("res", "buildings", "units"):
            for key, val in s[k].items():
                if key in base[k]:
                    base[k][key] = val
        for k in ("smith_level", "clock", "next_wave", "wave", "last", "log", "decree_left", "decree_cd"):
            if k in s:
                base[k] = s[k]
        base["last"] = min(float(base["last"]), time.time())
        update(base)
        return base
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        base = new_state()
        base["log"].append("Save file was corrupted - started a new game.")
        return base


# ---------------------------------------------------------------- helpers
def fmt(n):
    for limit, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= limit:
            return f"{n / limit:.2f}{suf}"
    return f"{n:.1f}"


def fmt_time(sec):
    sec = int(sec)
    h, m, s_ = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}h {m}m" if h else f"{m}m {s_}s"


def cost_text(cost):
    return ", ".join(f"{fmt(v)} {r}" for r, v in cost.items())


def pretty(name):
    return name.replace("_", " ").title()


# -------------------------------------------------------------------- GUI
# Palette (dark wood, parchment, gold leaf)
BG = "#1c120a"
WOOD = "#2f2014"
PANEL = "#3d2a1a"
PARCH = "#e8d5a3"
PARCH_DARK = "#d9c28a"
INK = "#2b1d12"
BROWN = "#6b4a2a"
GOLD = "#c9a227"
GOLD_HI = "#f0c94a"
DIS_BG = "#8a7a5c"
DIS_FG = "#4a3d2a"
GOOD = "#7fae4a"
BAD = "#e0775f"
RED = "#8b1e1e"
RED_HI = "#b22b2b"

# Emoji icons. If they render as boxes on your system, set them to "".
ICONS = {"gold": "💰", "food": "🌾", "wood": "🌲", "iron": "⛏", "weapons": "🗡"}
ROW_ICONS = {
    "farm": "🌾", "lumber_camp": "🪓", "market": "⚖", "iron_mine": "⛏",
    "blacksmith": "🔨", "militia": "🛡", "archer": "🏹", "knight": "🐎", "smith": "🔥",
}


def run_gui():
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import messagebox

    state = load()
    root = tk.Tk()
    root.title("Medieval Idle")
    root.geometry("880x850")
    root.minsize(780, 760)
    root.configure(bg=BG)

    avail = set(tkfont.families())
    serif = next((f for f in ("Palatino Linotype", "Book Antiqua", "Garamond",
                              "Georgia", "Times New Roman") if f in avail), "Times")

    def F(size, style=""):
        return (serif, size, style) if style else (serif, size)

    class MedButton(tk.Label):
        """Label-based button: colours work on every OS (tk.Button ignores them on macOS)."""

        def __init__(self, parent, command, base=GOLD, hover=GOLD_HI, fg=INK, **kw):
            super().__init__(parent, bd=2, relief="ridge", padx=10, pady=6, **kw)
            self.command, self.base, self.hover_col, self.fg_col = command, base, hover, fg
            self.enabled, self.hovering = True, False
            self.bind("<Enter>", lambda e: self._set_hover(True))
            self.bind("<Leave>", lambda e: self._set_hover(False))
            self.bind("<Button-1>", lambda e: self.command() if self.enabled else None)
            self._paint()

        def _set_hover(self, h):
            self.hovering = h
            self._paint()

        def set_enabled(self, on):
            self.enabled = on
            self._paint()

        def _paint(self):
            if not self.enabled:
                self.config(bg=DIS_BG, fg=DIS_FG, cursor="arrow")
            else:
                self.config(bg=self.hover_col if self.hovering else self.base,
                            fg=self.fg_col, cursor="hand2")

    # ---- header
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", pady=(10, 2))
    tk.Label(header, text="⚔  MEDIEVAL IDLE  ⚔", bg=BG, fg=GOLD_HI,
             font=F(24, "bold")).pack()
    tk.Label(header, text="⚜ ━━━━━━━━━━  Build · Forge · Conquer  ━━━━━━━━━━ ⚜",
             bg=BG, fg=GOLD, font=F(10, "italic")).pack()

    # ---- resource cards
    resbar = tk.Frame(root, bg=BG)
    resbar.pack(fill="x", padx=12, pady=8)
    res_cards = {}
    for i, r in enumerate(RESOURCES):
        resbar.columnconfigure(i, weight=1, uniform="res")
        card = tk.Frame(resbar, bg=PANEL, highlightbackground=GOLD, highlightthickness=1)
        card.grid(row=0, column=i, padx=4, sticky="ew")
        tk.Label(card, text=f"{ICONS.get(r, '')} {r.title()}".strip(), bg=PANEL, fg=GOLD,
                 font=F(10, "bold")).pack(pady=(6, 0))
        amt = tk.Label(card, text="0", bg=PANEL, fg=PARCH, font=F(17, "bold"))
        amt.pack()
        rate = tk.Label(card, text="+0.00/s", bg=PANEL, fg=GOOD, font=F(9))
        rate.pack(pady=(0, 6))
        res_cards[r] = (amt, rate)

    # ---- wave bar
    wave_canvas = tk.Canvas(root, height=30, bg=BG, highlightthickness=0)
    wave_canvas.pack(fill="x", padx=16, pady=(0, 8))

    # ---- active play: gather buttons + Royal Decree
    actionbar = tk.Frame(root, bg=BG)
    actionbar.pack(fill="x", padx=16, pady=(0, 8))
    gather_btns = {}

    def do_gather(r):
        gather(state, r)
        refresh()

    def do_decree():
        use_decree(state)
        refresh()

    for i, r in enumerate(GATHERABLE):
        actionbar.columnconfigure(i, weight=1, uniform="g")
        btn = MedButton(actionbar, command=lambda r=r: do_gather(r),
                        font=F(10, "bold"), justify="center")
        btn.grid(row=0, column=i, padx=3, sticky="ew")
        gather_btns[r] = btn
    actionbar.columnconfigure(len(GATHERABLE), weight=2, uniform="d")
    decree_btn = MedButton(actionbar, command=do_decree, font=F(10, "bold"), justify="center")
    decree_btn.grid(row=0, column=len(GATHERABLE), padx=(12, 3), sticky="ew")

    # ---- bottom widgets are packed first so the content area takes the rest
    footer = tk.Frame(root, bg=BG)
    footer.pack(side="bottom", fill="x", padx=16, pady=(2, 10))
    log_frame = tk.Frame(root, bg=PANEL, highlightbackground=GOLD, highlightthickness=1)
    log_frame.pack(side="bottom", fill="x", padx=16, pady=6)
    tk.Label(log_frame, text="📜 Chronicle", bg=PANEL, fg=GOLD,
             font=F(11, "bold")).pack(anchor="w", padx=8, pady=(4, 0))
    log_box = tk.Text(log_frame, height=6, bg=BG, fg=PARCH, font=F(10, "italic"),
                      bd=0, padx=8, pady=4, wrap="word", state="disabled")
    log_box.pack(fill="x", padx=4, pady=(0, 4))
    log_cache = [""]

    # ---- tabs
    tabbar = tk.Frame(root, bg=BG)
    tabbar.pack(fill="x", padx=16)
    content = tk.Frame(root, bg=PARCH, highlightbackground=GOLD, highlightthickness=2)
    content.pack(fill="both", expand=True, padx=16, pady=(0, 4))

    pages, tab_btns = {}, {}

    def show(name):
        for n, p in pages.items():
            p.pack_forget()
            tab_btns[n].config(bg=PANEL, fg=GOLD)
        pages[name].pack(fill="both", expand=True, pady=6)
        tab_btns[name].config(bg=PARCH, fg=INK)

    for key, title in (("city", "🏰 City"), ("army", "⚔ Army"), ("crafts", "🔨 Crafts")):
        b = tk.Label(tabbar, text=title, font=F(12, "bold"), padx=22, pady=6,
                     bg=PANEL, fg=GOLD, cursor="hand2", bd=1, relief="raised")
        b.pack(side="left", padx=(0, 3))
        b.bind("<Button-1>", lambda e, n=key: show(n))
        tab_btns[key] = b
        pages[key] = tk.Frame(content, bg=PARCH)

    # ---- action rows
    rows = []  # (kind, key, title_label, sub_label, button)

    def act(kind, key):
        if kind == "b":
            ok = build(state, key)
        elif kind == "u":
            ok = recruit(state, key)
        else:
            ok = upgrade_smith(state)
        if not ok:
            state["log"].append("Not enough resources (or the blacksmith is missing).")
        refresh()

    def make_row(parent, kind, key):
        card = tk.Frame(parent, bg=PARCH_DARK, highlightbackground=BROWN, highlightthickness=1)
        card.pack(fill="x", padx=10, pady=4)
        left = tk.Frame(card, bg=PARCH_DARK)
        left.pack(side="left", fill="x", expand=True, padx=10, pady=6)
        title = tk.Label(left, bg=PARCH_DARK, fg=INK, font=F(13, "bold"), anchor="w")
        title.pack(anchor="w")
        sub = tk.Label(left, bg=PARCH_DARK, fg="#5a4630", font=F(10, "italic"),
                       anchor="w", justify="left")
        sub.pack(anchor="w")
        btn = MedButton(card, command=lambda: act(kind, key), font=F(10, "bold"),
                        width=24, justify="center")
        btn.pack(side="right", padx=10, pady=6)
        rows.append((kind, key, title, sub, btn))

    for b in BUILDINGS:
        make_row(pages["city"], "b", b)
    for u in UNITS:
        make_row(pages["army"], "u", u)
    army_info = tk.Label(pages["army"], bg=PARCH, fg=INK, font=F(13, "bold"))
    army_info.pack(anchor="w", padx=14, pady=10)
    make_row(pages["crafts"], "s", "smith")
    tk.Label(pages["crafts"], bg=PARCH, fg="#5a4630", font=F(11, "italic"),
             wraplength=760, justify="left",
             text="Each blacksmith level: +25% weapon output and +10% army power.\n"
                  "The chain: Iron Mine → iron → Blacksmith → weapons → Knights → army power."
             ).pack(anchor="w", padx=14, pady=12)

    # ---- drawing / refresh
    def draw_wave_bar():
        c = wave_canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 200), 30
        eta = max(0.0, state["next_wave"] - state["clock"])
        frac = min(1.0, max(0.0, 1 - eta / WAVE_INTERVAL))
        enemy, mine = enemy_power(state["wave"]), army_power(state)
        c.create_rectangle(1, 1, w - 1, h - 1, fill=BG, outline=GOLD)
        c.create_rectangle(3, 3, 3 + (w - 6) * frac, h - 3, outline="",
                           fill="#5a7d2a" if mine >= enemy else "#a8321e")
        c.create_text(w / 2, h / 2, fill=PARCH, font=F(10, "bold"),
                      text=f"⚔ Wave {state['wave'] + 1} arrives in {fmt_time(eta)}   |   "
                           f"Enemy {enemy:.0f}  vs  Your army {mine:.0f}")

    def refresh():
        rates = net_rates(state)
        for r in RESOURCES:
            amt, rate = res_cards[r]
            amt.config(text=fmt(state["res"][r]))
            rate.config(text=f"{rates[r]:+.2f}/s", fg=GOOD if rates[r] >= 0 else BAD)
        draw_wave_bar()

        for r, gb in gather_btns.items():
            gb.config(text=f"{ICONS.get(r, '')} Gather {r.title()}\n+{fmt(gather_gain(state, r))}")
        if state["decree_left"] > 0:
            decree_btn.base = decree_btn.hover_col = GOOD
            decree_btn.config(text=f"👑 Decree ACTIVE x{DECREE_MULT:.0f}\n{state['decree_left']:.0f}s left")
            decree_btn.set_enabled(True)
        elif state["decree_cd"] > 0:
            decree_btn.base, decree_btn.hover_col = GOLD, GOLD_HI
            decree_btn.config(text=f"👑 Royal Decree\ncooldown {state['decree_cd']:.0f}s")
            decree_btn.set_enabled(False)
        else:
            decree_btn.base, decree_btn.hover_col = GOLD_HI, "#ffe27a"
            decree_btn.config(text=f"👑 ROYAL DECREE READY\nx{DECREE_MULT:.0f} for {DECREE_DURATION}s  [Space]")
            decree_btn.set_enabled(True)

        for kind, key, title, sub, btn in rows:
            icon = ROW_ICONS.get(key, "")
            if kind == "b":
                n = state["buildings"][key]
                b = BUILDINGS[key]
                c = scaled_cost(b["cost"], n)
                inp = ", ".join(f"{v} {r}" for r, v in b["in"].items()) or "nothing"
                out = ", ".join(f"{v} {r}" for r, v in b["out"].items())
                title.config(text=f"{icon} {pretty(key)}   ×{n}")
                sub.config(text=f"Consumes {inp}/s  →  produces {out}/s")
                btn.config(text=f"Build\n{cost_text(c)}")
                afford = can_afford(state["res"], c)
            elif kind == "u":
                u = UNITS[key]
                title.config(text=f"{icon} {pretty(key)}   ×{state['units'][key]}")
                sub.config(text=f"Power {u['power']} each")
                btn.config(text=f"Recruit\n{cost_text(u['cost'])}")
                afford = can_afford(state["res"], u["cost"])
            else:
                lvl = state["smith_level"]
                title.config(text=f"{icon} Forge mastery   level {lvl}/{SMITH_MAX}")
                sub.config(text="Requires at least one Blacksmith")
                if lvl >= SMITH_MAX:
                    btn.config(text="Mastered")
                    afford = False
                else:
                    c = smith_cost(lvl)
                    btn.config(text=f"Train\n{cost_text(c)}")
                    afford = can_afford(state["res"], c) and state["buildings"]["blacksmith"] > 0
            btn.set_enabled(afford)

        army_info.config(text=f"🛡 Total army power: {army_power(state):.1f}")

        text = "\n".join(state["log"][-6:])
        if text != log_cache[0]:
            log_cache[0] = text
            log_box.config(state="normal")
            log_box.delete("1.0", "end")
            log_box.insert("end", text)
            log_box.see("end")
            log_box.config(state="disabled")

    def loop():
        update(state)
        refresh()
        root.after(250, loop)

    def autosave():
        save(state)
        root.after(10000, autosave)

    def reset():
        nonlocal state
        if messagebox.askyesno("Reset kingdom", "Burn it all down and start over?"):
            state = new_state()
            save(state)

    def on_close():
        update(state)
        save(state)
        root.destroy()

    tk.Label(footer, text="Autosaves every 10 seconds", bg=BG, fg=BROWN,
             font=F(9, "italic")).pack(side="left")
    MedButton(footer, command=reset, base=RED, hover=RED_HI, fg=PARCH,
              text="Reset kingdom", font=F(9, "bold")).pack(side="right")

    for i, r in enumerate(GATHERABLE):
        root.bind(str(i + 1), lambda e, r=r: do_gather(r))
    root.bind("<space>", lambda e: do_decree())
    show("city")
    root.protocol("WM_DELETE_WINDOW", on_close)
    loop()
    root.after(10000, autosave)
    root.mainloop()


# ------------------------------------------------------ balance simulator
def simulate(minutes=30, active=False):
    """Dumb bot: round-robin builds, spends spare food on the best unit it can afford."""
    s = new_state()
    s["last"] = time.time()
    order = list(BUILDINGS)
    for sec in range(1, minutes * 60 + 1):
        tick(s, 1)
        s["last"] = time.time()
        if active:                       # max-spam player: decree on cooldown + 2 clicks/s
            use_decree(s)
            for _ in range(max(1, round(1 / GATHER_CD))):
                s["last_gather"] = 0.0
                gather(s, "food")
        target = min(order, key=lambda b: s["buildings"][b])
        build(s, target)
        if s["buildings"]["blacksmith"]:
            upgrade_smith(s)
        for u in ("knight", "archer", "militia"):
            if recruit(s, u):
                break
        if sec % 300 == 0:
            r = s["res"]
            print(f"{sec // 60:>3} min | wave {s['wave']} | power {army_power(s):7.1f} "
                  f"vs next {enemy_power(s['wave']):7.1f} | "
                  + " ".join(f"{k}={fmt(v)}" for k, v in r.items()))
    print("\n".join(s["log"][-8:]))
    return s


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "sim":
        simulate(active=len(sys.argv) > 2 and sys.argv[2] == "active")
    else:
        run_gui()