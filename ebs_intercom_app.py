import threading
import tkinter as tk
from tkinter import messagebox
import numpy as np
import pyaudio
import ttkbootstrap as tb
from ttkbootstrap.constants import *

RATE = 48000
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 1


def rms_level(int16_audio: np.ndarray):
    if int16_audio.size == 0:
        return 0.0
    rms = np.sqrt(np.mean(int16_audio.astype(np.float32) ** 2))
    level = (rms / 32768.0) * 100.0
    return float(np.clip(level, 0, 100))


def is_real_input(dev):
    name = dev["name"].lower()
    if dev["maxInput"] < 1:
        return False
    bad_words = ["mapper", "mix", "virtual", "wave", "stereo", "default"]
    if any(bad in name for bad in bad_words):
        return False
    return True


def is_real_output(dev):
    name = dev["name"].lower()
    if dev["maxOutput"] < 1:
        return False
    bad_words = ["mapper", "mix", "virtual", "wave", "stereo", "default"]
    if any(bad in name for bad in bad_words):
        return False
    return True


class AudioRouter(threading.Thread):
    """
    Bir kişinin mikrofonunu okur, Gain/Mute/PTT uygular,
    routing matrisine göre diğer çıkışlara yollar.
    """
    def __init__(self, p, mic_id, out_ids_by_person,
                 routing_getter, routing_lock,
                 gain_var, mute_var, ptt_enabled_var, ptt_pressed_var,
                 vu_callback, stop_event, self_index):
        super().__init__(daemon=True)
        self.p = p
        self.mic_id = mic_id
        self.out_ids_by_person = out_ids_by_person  # list length N
        self.routing_getter = routing_getter        # callable(i)->list[bool]
        self.routing_lock = routing_lock
        self.gain_var = gain_var
        self.mute_var = mute_var
        self.ptt_enabled_var = ptt_enabled_var
        self.ptt_pressed_var = ptt_pressed_var
        self.vu_callback = vu_callback
        self.stop_event = stop_event
        self.self_index = self_index

        self.mic_stream = None
        self.out_streams = {}  # person_index -> stream

    def open_streams(self):
        self.mic_stream = self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
            input_device_index=self.mic_id
        )

        # Kendi hariç tüm kişilerin output stream'ini aç
        for j, oid in enumerate(self.out_ids_by_person):
            if j == self.self_index:
                continue
            s = self.p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                output=True,
                frames_per_buffer=CHUNK,
                output_device_index=oid
            )
            self.out_streams[j] = s

    def close_streams(self):
        try:
            if self.mic_stream:
                self.mic_stream.stop_stream()
                self.mic_stream.close()
        except:
            pass

        for s in self.out_streams.values():
            try:
                s.stop_stream()
                s.close()
            except:
                pass

        self.out_streams = {}

    def run(self):
        try:
            self.open_streams()
        except Exception as e:
            messagebox.showerror("Audio Stream Hatası", str(e))
            self.stop_event.set()
            return

        while not self.stop_event.is_set():
            try:
                data = self.mic_stream.read(CHUNK, exception_on_overflow=False)
                audio_np = np.frombuffer(data, dtype=np.int16)

                # VU meter güncelle
                lvl = rms_level(audio_np)
                if self.vu_callback:
                    self.vu_callback(lvl)

                if self.mute_var.get():
                    continue

                if self.ptt_enabled_var.get() and not self.ptt_pressed_var.get():
                    continue

                gain = float(self.gain_var.get())
                if gain != 1.0:
                    f = audio_np.astype(np.float32) * gain
                    f = np.clip(f, -32768, 32767).astype(np.int16)
                    out_data = f.tobytes()
                else:
                    out_data = data

                # Routing row'u güvenli şekilde al
                with self.routing_lock:
                    row = self.routing_getter(self.self_index)

                # Hangi kişilere gidecekse yaz
                for j, s in self.out_streams.items():
                    if row[j]:
                        s.write(out_data)

            except Exception:
                continue

        self.close_streams()


class IntercomApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Çok Kişilik Interkom - Mixer Routing")
        self.root.geometry("1200x760")
        self.root.resizable(True, True)

        self.p = pyaudio.PyAudio()
        self.devices = self.get_devices()

        self.stop_event = threading.Event()
        self.routers = []
        self.running = False

        self.person_count_var = tk.IntVar(value=3)
        self.person_panels = []

        # routing matrix (python bool), lock ile korunur
        self.routing_lock = threading.Lock()
        self.routing = []  # NxN bool

        self.build_ui()
        self.build_person_panels()
        self.init_routing_matrix()
    # ---------- Mixer UI Helpers (LED / Fade / Hover) ----------
    def _hex_to_rgb(self, hx):
        hx = hx.lstrip("#")
        return tuple(int(hx[i:i+2], 16) for i in (0, 2, 4))

    def _rgb_to_hex(self, rgb):
        return "#%02x%02x%02x" % rgb

    def _blend(self, c1, c2, t):
        r1, g1, b1 = self._hex_to_rgb(c1)
        r2, g2, b2 = self._hex_to_rgb(c2)
        r = int(r1 + (r2 - r1) * t)
        g = int(g1 + (g2 - g1) * t)
        b = int(b1 + (b2 - b1) * t)
        return self._rgb_to_hex((r, g, b))

    def _fade_circle(self, canvas, circle_id, start_color, end_color, steps=8, delay=18):
        # Basit fade (renk geçişi)
        def step(k=0):
            if k > steps:
                return
            color = self._blend(start_color, end_color, k / steps)
            canvas.itemconfig(circle_id, fill=color, outline=color)
            canvas.after(delay, lambda: step(k+1))
        step()

    def _click_pulse(self, canvas, circle_id, base_color):
        # Kısa tıklama animasyonu: parlayıp normale dönsün
        bright = self._blend(base_color, "#ffffff", 0.35)
        self._fade_circle(canvas, circle_id, base_color, bright, steps=4, delay=12)
        canvas.after(80, lambda: self._fade_circle(canvas, circle_id, bright, base_color, steps=5, delay=14))

    def _set_led_state(self, cell, state):
        """
        state: "on" | "off" | "lock"
        cell: {"cv":canvas,"circle":id,"text":id,"label":id}
        """
        if state == "lock":
            color = "#555555"
            emoji = "⚪"
        elif state == "on":
            color = "#1fa64b"   # LED yeşil
            emoji = "🟢"
        else:
            color = "#c0392b"   # LED kırmızı
            emoji = "🔴"

        cv = cell["cv"]
        circle_id = cell["circle"]
        text_id = cell["text"]

        current = cv.itemcget(circle_id, "fill") or color
        self._fade_circle(cv, circle_id, current, color, steps=7, delay=16)
        cv.itemconfig(text_id, text=emoji)
        cell["color"] = color
        cell["state"] = state

    # ---------------- Devices ----------------
    def get_devices(self):
        devs = []
        for i in range(self.p.get_device_count()):
            info = self.p.get_device_info_by_index(i)
            devs.append({
                "id": i,
                "name": info["name"],
                "maxInput": info.get("maxInputChannels", 0),
                "maxOutput": info.get("maxOutputChannels", 0)
            })
        return devs

    def list_inputs(self):
        return [d for d in self.devices if is_real_input(d)]

    def list_outputs(self):
        return [d for d in self.devices if is_real_output(d)]

    def parse_id(self, s):
        return int(s.split(" - ")[0].strip())

    # ---------------- UI ----------------
    def build_ui(self):
        tb.Style("darkly")
        main = tb.Frame(self.root, padding=12)
        main.pack(fill=BOTH, expand=True)

        title = tb.Label(main, text="🎧 Çok Kişilik Interkom (VU + PTT + Mixer Routing)",
                         font=("Segoe UI", 18, "bold"))
        title.pack(pady=(0, 10))

        topbar = tb.Frame(main)
        topbar.pack(fill=X, pady=(0, 10))

        tb.Label(topbar, text="Kişi sayısı:", font=("Segoe UI", 11, "bold")).pack(side=LEFT, padx=(0, 6))

        count_cb = tb.Combobox(
            topbar, width=6, state="readonly",
            values=[3, 4, 5, 6],
            textvariable=self.person_count_var
        )
        count_cb.pack(side=LEFT)
        count_cb.bind("<<ComboboxSelected>>", lambda e: self.on_change_person_count())

        tb.Button(
            topbar, text="🎚 Mikser / Routing Aç",
            bootstyle="info", command=self.open_mixer
        ).pack(side=LEFT, padx=8)

        tb.Button(
            topbar, text="🔄 Cihazları Yenile",
            bootstyle="secondary", command=self.refresh_devices
        ).pack(side=RIGHT)

        self.grid_holder = tb.Frame(main)
        self.grid_holder.pack(fill=BOTH, expand=True)

        controls = tb.Frame(main)
        controls.pack(fill=X, pady=8)

        self.start_btn = tb.Button(
            controls, text="▶ Start Intercom",
            bootstyle="success", command=self.start_intercom, width=18
        )
        self.start_btn.pack(side=LEFT, padx=5)

        self.stop_btn = tb.Button(
            controls, text="⏹ Stop",
            bootstyle="danger", command=self.stop_intercom,
            width=10, state=DISABLED
        )
        self.stop_btn.pack(side=LEFT, padx=5)

        hint = (
            "• Her kişi için farklı mikrofon ve farklı kulaklık/çıkış seç.\n"
            "• Varsayılan routing: herkes herkesi duyar, kimse kendini duymaz.\n"
            "• Mikserde tıklayarak anlık routing değiştirebilirsin."
        )
        tb.Label(main, text=hint, justify="left", foreground="#bbbbbb").pack(anchor="w", pady=4)

    def clear_person_panels(self):
        for w in self.grid_holder.winfo_children():
            w.destroy()
        self.person_panels = []

    def build_person_panels(self):
        self.clear_person_panels()

        n = int(self.person_count_var.get())
        inputs = self.list_inputs()
        outputs = self.list_outputs()
        in_names = [f'{d["id"]} - {d["name"]}' for d in inputs]
        out_names = [f'{d["id"]} - {d["name"]}' for d in outputs]

        default_names_base = ["Spiker", "Soru Sorana", "Konuk", "Yönetmen", "Editör", "Misafir"]
        default_names = default_names_base[:n]

        # grid sütunlarını eşitle
        for c in range(n):
            self.grid_holder.columnconfigure(c, weight=1)

        for idx in range(n):
            card = tb.Labelframe(self.grid_holder, text=f"Kişi {idx+1}", padding=10, bootstyle="primary")
            card.grid(row=0, column=idx, padx=6, pady=6, sticky="nsew")

            name_var = tk.StringVar(value=default_names[idx])
            mic_var = tk.StringVar(value=in_names[0] if in_names else "")
            out_var = tk.StringVar(value=out_names[0] if out_names else "")
            gain_var = tk.DoubleVar(value=1.0)
            mute_var = tk.BooleanVar(value=False)

            # PTT default: son kişi (guest) kapalı olsun, diğerleri açık
            ptt_enabled_var = tk.BooleanVar(value=False if idx == n-1 else True)
            ptt_pressed_var = tk.BooleanVar(value=False)

            tb.Label(card, text="👤 İsim/Rol:").pack(anchor="w")
            tb.Entry(card, textvariable=name_var, width=24).pack(pady=(0, 6))

            tb.Label(card, text="🎙 Mikrofon Seç:").pack(anchor="w")
            tb.Combobox(card, values=in_names, textvariable=mic_var,
                        width=24, state="readonly").pack(pady=(0, 6))

            tb.Label(card, text="🔊 Kulaklık / Çıkış Seç:").pack(anchor="w")
            tb.Combobox(card, values=out_names, textvariable=out_var,
                        width=24, state="readonly").pack(pady=(0, 6))

            tb.Label(card, text="VU Meter:").pack(anchor="w")
            vu = tb.Progressbar(card, length=180, maximum=100, bootstyle="info-striped")
            vu.pack(pady=(0, 6))

            tb.Label(card, text="Gain:").pack(anchor="w")
            tb.Scale(card, from_=0.2, to=2.5, variable=gain_var,
                     length=180, bootstyle="info").pack(pady=(0, 2))
            tb.Label(card, textvariable=gain_var).pack(anchor="e")

            tb.Checkbutton(card, text="Mute", variable=mute_var,
                           bootstyle="danger").pack(anchor="w", pady=(4, 2))

            tb.Checkbutton(card, text="PTT Modu (Bas-Konuş)",
                           variable=ptt_enabled_var,
                           bootstyle="warning").pack(anchor="w", pady=(0, 4))

            ptt_btn = tb.Button(card, text="🎤 BAS & KONUŞ",
                                bootstyle="success-outline", width=18)
            ptt_btn.pack(pady=(0, 4))

            def on_press(ev, v=ptt_pressed_var):
                v.set(True)

            def on_release(ev, v=ptt_pressed_var):
                v.set(False)

            ptt_btn.bind("<ButtonPress-1>", on_press)
            ptt_btn.bind("<ButtonRelease-1>", on_release)
            ptt_btn.bind("<Leave>", on_release)

            self.person_panels.append({
                "name_var": name_var,
                "mic_var": mic_var,
                "out_var": out_var,
                "gain_var": gain_var,
                "mute_var": mute_var,
                "ptt_enabled_var": ptt_enabled_var,
                "ptt_pressed_var": ptt_pressed_var,
                "vu_bar": vu,
            })

    # ---------------- Routing / Mixer ----------------
    def init_routing_matrix(self):
        n = int(self.person_count_var.get())
        with self.routing_lock:
            self.routing = []
            for i in range(n):
                row = []
                for j in range(n):
                    row.append(False if i == j else True)
                self.routing.append(row)

    def routing_getter(self, i):
        return self.routing[i]

    def open_mixer(self):
        n = int(self.person_count_var.get())

        win = tb.Toplevel(self.root)
        win.title("🎚 OBS Tarzı Mikser / Routing Matrix")
        win.geometry("980x700")
        win.resizable(True, True)

        # OBS vibe: koyu arkaplan
        win.configure(bg="#101214")

        header = tb.Label(
            win,
            text="🎧 Ses Yönlendirme Matrisi (OBS Grid)\n"
                 "Satırdaki 🎙️ KONUŞAN kişinin sesi → Sütundaki 🎧 DİNLEYEN kişiye gider.\n"
                 "🟢 Açık | 🔴 Kapalı | ⚪ Kilitli (Kişi kendini duyamaz)",
            font=("Segoe UI", 12, "bold"),
            justify="center",
            bootstyle="inverse"
        )
        header.pack(pady=10)

        outer = tb.Frame(win, bootstyle="dark")
        outer.pack(fill=BOTH, expand=True, padx=14, pady=14)

        table = tb.Frame(outer, bootstyle="dark")
        table.pack(padx=10, pady=10)

        # Grid çizgileri gibi görünmesi için frame border
        for c in range(n+1):
            table.columnconfigure(c, weight=1)

        # --- Başlıklar (sütun) ---
        tb.Label(table, text="", bootstyle="inverse").grid(row=0, column=0, padx=8, pady=8)

        for j in range(n):
            name = self.person_panels[j]["name_var"].get()
            lbl = tb.Label(
                table,
                text=f"🎧 {name}",
                font=("Segoe UI", 11, "bold"),
                bootstyle="inverse"
            )
            lbl.grid(row=0, column=j+1, padx=12, pady=12)

        # hücre referansları
        cells = {}  # (i,j) -> cell dict

        # --- Satırlar + LED yuvarlak node'lar ---
        for i in range(n):
            name_i = self.person_panels[i]["name_var"].get()
            tb.Label(
                table,
                text=f"🎙️ {name_i}",
                font=("Segoe UI", 11, "bold"),
                bootstyle="inverse"
            ).grid(row=i+1, column=0, padx=10, pady=10, sticky="e")

            for j in range(n):
                # OBS-stil hücre arka paneli
                cell_frame = tb.Frame(
                    table,
                    bootstyle="dark",
                    padding=4
                )
                cell_frame.grid(row=i+1, column=j+1, padx=10, pady=10)

                # Circular LED node = Canvas
                cv = tk.Canvas(
                    cell_frame,
                    width=52, height=52,
                    bg="#181b1f",  # obs cell bg
                    highlightthickness=1,
                    highlightbackground="#2b2f36"
                )
                cv.pack()

                # circle
                circle = cv.create_oval(
                    8, 8, 44, 44,
                    fill="#333333",
                    outline="#333333",
                    width=2
                )
                # emoji text
                text_id = cv.create_text(
                    26, 26,
                    text="",
                    fill="white",
                    font=("Segoe UI Emoji", 14, "bold")
                )

                # başlangıç state
                if i == j:
                    state = "lock"
                else:
                    state = "on" if self.routing[i][j] else "off"

                cell = {
                    "cv": cv,
                    "circle": circle,
                    "text": text_id,
                    "state": state,
                    "color": None
                }
                cells[(i, j)] = cell
                self._set_led_state(cell, state)

                # --- HOVER EFEKTİ (LED parlaması) ---
                def make_hover_handlers(ii=i, jj=j):
                    cell_local = cells[(ii, jj)]

                    def on_enter(_):
                        if cell_local["state"] == "lock":
                            return
                        # hover'da hafif aydınlat
                        base = cell_local["color"]
                        hover = self._blend(base, "#ffffff", 0.25)
                        cv.itemconfig(circle, fill=hover, outline=hover)

                    def on_leave(_):
                        if cell_local["state"] == "lock":
                            return
                        base = cell_local["color"]
                        cv.itemconfig(circle, fill=base, outline=base)

                    return on_enter, on_leave

                on_enter, on_leave = make_hover_handlers()
                cv.bind("<Enter>", on_enter)
                cv.bind("<Leave>", on_leave)

                # --- CLICK / TOGGLE + PULSE + FADE ---
                if i != j:
                    def make_toggle(ii=i, jj=j):
                        cell_local = cells[(ii, jj)]
                        def toggle(_):
                            # routing değiştir
                            with self.routing_lock:
                                self.routing[ii][jj] = not self.routing[ii][jj]
                                new_state = "on" if self.routing[ii][jj] else "off"

                            # pulse animasyonu
                            base_color = "#1fa64b" if new_state == "on" else "#c0392b"
                            self._click_pulse(cell_local["cv"], cell_local["circle"], base_color)

                            # state set (fade ile)
                            self._set_led_state(cell_local, new_state)
                        return toggle

                    cv.bind("<Button-1>", make_toggle())

        # Alt mini legend (obs grid gibi)
        legend = tb.Frame(win, bootstyle="dark")
        legend.pack(pady=6)

        tb.Label(legend, text="🟢 Açık", bootstyle="success-inverse", padding=6).pack(side=LEFT, padx=6)
        tb.Label(legend, text="🔴 Kapalı", bootstyle="danger-inverse", padding=6).pack(side=LEFT, padx=6)
        tb.Label(legend, text="⚪ Kilitli", bootstyle="secondary-inverse", padding=6).pack(side=LEFT, padx=6)

        tb.Button(win, text="Kapat", bootstyle="secondary", command=win.destroy).pack(pady=10)



    # ---------------- Actions ----------------
    def on_change_person_count(self):
        if self.running:
            messagebox.showinfo("Çalışıyor", "Önce interkomu durdurmalısın.")
            # eski değere geri dön
            return

        self.build_person_panels()
        self.init_routing_matrix()

    def start_intercom(self):
        if self.running:
            return

        try:
            n = int(self.person_count_var.get())
            mics = [self.parse_id(p["mic_var"].get()) for p in self.person_panels]
            outs = [self.parse_id(p["out_var"].get()) for p in self.person_panels]
            if len(mics) != n or len(outs) != n:
                raise ValueError()
        except Exception:
            messagebox.showwarning("Eksik Seçim", "Lütfen tüm mikrofon ve çıkışları seç.")
            return

        self.stop_event.clear()
        self.routers = []

        def make_vu_cb(vu_bar):
            def cb(level):
                self.root.after(0, lambda: vu_bar.configure(value=level))
            return cb

        for i in range(n):
            r = AudioRouter(
                self.p,
                mics[i],
                outs,
                self.routing_getter,
                self.routing_lock,
                self.person_panels[i]["gain_var"],
                self.person_panels[i]["mute_var"],
                self.person_panels[i]["ptt_enabled_var"],
                self.person_panels[i]["ptt_pressed_var"],
                make_vu_cb(self.person_panels[i]["vu_bar"]),
                self.stop_event,
                self_index=i
            )
            self.routers.append(r)

        for r in self.routers:
            r.start()

        self.running = True
        self.start_btn.config(state=DISABLED)
        self.stop_btn.config(state=NORMAL)

    def stop_intercom(self):
        if not self.running:
            return

        self.stop_event.set()
        self.running = False
        self.start_btn.config(state=NORMAL)
        self.stop_btn.config(state=DISABLED)

        for p in self.person_panels:
            p["vu_bar"].configure(value=0)

    def refresh_devices(self):
        if self.running:
            messagebox.showinfo("Çalışıyor", "Önce interkomu durdurmalısın.")
            return
        self.devices = self.get_devices()
        self.build_person_panels()
        self.init_routing_matrix()
        messagebox.showinfo("Yenilendi", "Cihaz listesi yenilendi.")

    def on_close(self):
        self.stop_intercom()
        try:
            self.p.terminate()
        except:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tb.Window(themename="darkly")
    app = IntercomApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
