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

# VU meter ölçekleme
def rms_level(int16_audio: np.ndarray):
    if int16_audio.size == 0:
        return 0.0
    rms = np.sqrt(np.mean(int16_audio.astype(np.float32)**2))
    # int16 max ~32768 -> 0..100 arası normalize
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
    Bir mikrofonu okur, PTT/Mute/Gain uygular, diğer çıkışlara yazar.
    Ayrıca VU seviyesini callback ile GUI’ye yollar.
    """
    def __init__(self, p, mic_id, out_ids, gain_var, mute_var,
                 ptt_enabled_var, ptt_pressed_var,
                 vu_callback, stop_event):
        super().__init__(daemon=True)
        self.p = p
        self.mic_id = mic_id
        self.out_ids = out_ids
        self.gain_var = gain_var
        self.mute_var = mute_var
        self.ptt_enabled_var = ptt_enabled_var
        self.ptt_pressed_var = ptt_pressed_var
        self.vu_callback = vu_callback
        self.stop_event = stop_event

        self.mic_stream = None
        self.out_streams = []

    def open_streams(self):
        self.mic_stream = self.p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK,
            input_device_index=self.mic_id
        )

        self.out_streams = [
            self.p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                output=True,
                frames_per_buffer=CHUNK,
                output_device_index=oid
            )
            for oid in self.out_ids
        ]

    def close_streams(self):
        try:
            if self.mic_stream:
                self.mic_stream.stop_stream()
                self.mic_stream.close()
        except:
            pass

        for s in self.out_streams:
            try:
                s.stop_stream()
                s.close()
            except:
                pass

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

                # Mute kontrolü
                if self.mute_var.get():
                    continue

                # PTT kontrolü
                if self.ptt_enabled_var.get() and not self.ptt_pressed_var.get():
                    # PTT aktif ama basılmıyor -> gönderme
                    continue

                # Gain uygula
                gain = float(self.gain_var.get())
                if gain != 1.0:
                    f = audio_np.astype(np.float32) * gain
                    f = np.clip(f, -32768, 32767).astype(np.int16)
                    out_data = f.tobytes()
                else:
                    out_data = data

                for o in self.out_streams:
                    o.write(out_data)

            except Exception:
                continue

        self.close_streams()


class IntercomApp:
    def __init__(self, root):
        self.root = root
        self.root.title("3 Kişilik Interkom - Metro GUI (VU + PTT)")
        self.root.geometry("980x650")
        self.root.resizable(False, False)

        self.p = pyaudio.PyAudio()
        self.devices = self.get_devices()

        self.stop_event = threading.Event()
        self.routers = []
        self.running = False

        self.build_ui()

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

    def build_ui(self):
        tb.Style("darkly")
        main = tb.Frame(self.root, padding=15)
        main.pack(fill=BOTH, expand=True)

        title = tb.Label(main, text="🎧 3 Kişilik Interkom (Tek Laptop) - VU + PTT",
                         font=("Segoe UI", 18, "bold"))
        title.pack(pady=(0, 12))

        grid = tb.Frame(main)
        grid.pack(fill=X)

        inputs = self.list_inputs()
        outputs = self.list_outputs()
        in_names = [f'{d["id"]} - {d["name"]}' for d in inputs]
        out_names = [f'{d["id"]} - {d["name"]}' for d in outputs]

        default_names = ["Spiker", "Soru Sorana", "Konuk"]

        self.person_panels = []

        for idx in range(3):
            card = tb.Labelframe(grid, text=f"Kişi {idx+1}", padding=12, bootstyle="primary")
            card.grid(row=0, column=idx, padx=8, pady=8, sticky="n")

            name_var = tk.StringVar(value=default_names[idx])

            mic_var = tk.StringVar(value=in_names[0] if in_names else "")
            out_var = tk.StringVar(value=out_names[0] if out_names else "")
            gain_var = tk.DoubleVar(value=1.0)
            mute_var = tk.BooleanVar(value=False)

            ptt_enabled_var = tk.BooleanVar(value=True if idx < 2 else False)
            ptt_pressed_var = tk.BooleanVar(value=False)

            # --- İsim
            tb.Label(card, text="👤 İsim/Rol:").pack(anchor="w")
            name_entry = tb.Entry(card, textvariable=name_var, width=28)
            name_entry.pack(pady=(0, 8))

            # --- Mic seçimi
            tb.Label(card, text="🎙 Mikrofon Seç:").pack(anchor="w")
            mic_cb = tb.Combobox(card, values=in_names, textvariable=mic_var,
                                 width=28, state="readonly")
            mic_cb.pack(pady=(0, 8))

            # --- Out seçimi
            tb.Label(card, text="🔊 Kulaklık / Çıkış Seç:").pack(anchor="w")
            out_cb = tb.Combobox(card, values=out_names, textvariable=out_var,
                                 width=28, state="readonly")
            out_cb.pack(pady=(0, 8))

            # --- VU Meter
            tb.Label(card, text="VU Meter (Konuşma Seviyesi):").pack(anchor="w")
            vu = tb.Progressbar(card, length=210, maximum=100, bootstyle="info-striped")
            vu.pack(pady=(0, 8))

            # --- Gain
            tb.Label(card, text="Gain (Ses Seviyesi):").pack(anchor="w")
            gain_scale = tb.Scale(card, from_=0.2, to=2.5, variable=gain_var,
                                  length=210, bootstyle="info")
            gain_scale.pack(pady=(0, 4))
            tb.Label(card, textvariable=gain_var).pack(anchor="e")

            # --- Mute
            mute_chk = tb.Checkbutton(card, text="Mute", variable=mute_var,
                                      bootstyle="danger")
            mute_chk.pack(anchor="w", pady=(5, 2))

            # --- PTT enable/disable
            ptt_enable_chk = tb.Checkbutton(card, text="PTT Modu (Bas-Konuş)",
                                            variable=ptt_enabled_var, bootstyle="warning")
            ptt_enable_chk.pack(anchor="w", pady=(0, 6))

            # --- PTT Buton (hold to talk)
            ptt_btn = tb.Button(card, text="🎤 BAS & KONUŞ",
                                bootstyle="success-outline", width=20)
            ptt_btn.pack(pady=(0, 6))

            def on_press(ev, v=ptt_pressed_var):
                v.set(True)

            def on_release(ev, v=ptt_pressed_var):
                v.set(False)

            ptt_btn.bind("<ButtonPress-1>", on_press)
            ptt_btn.bind("<ButtonRelease-1>", on_release)
            ptt_btn.bind("<Leave>", on_release)  # mouse dışarı çıkarsa kapat

            self.person_panels.append({
                "name_var": name_var,
                "mic_var": mic_var,
                "out_var": out_var,
                "gain_var": gain_var,
                "mute_var": mute_var,
                "ptt_enabled_var": ptt_enabled_var,
                "ptt_pressed_var": ptt_pressed_var,
                "vu_bar": vu
            })

        hint = (
            "Kişi 1 ve 2: USB mikrofonlu kulaklık seç.\n"
            "Kişi 3: Laptop dahili mic+speaker veya ayrı ses kartı seçebilirsin.\n"
            "Echo olmaması için herkesin kulaklık kullanması önerilir."
        )
        tb.Label(main, text=hint, justify="left",
                 foreground="#bbbbbb").pack(anchor="w", pady=10)

        controls = tb.Frame(main)
        controls.pack(fill=X, pady=6)

        self.start_btn = tb.Button(controls, text="▶ Start Intercom",
                                   bootstyle="success", command=self.start_intercom, width=18)
        self.start_btn.pack(side=LEFT, padx=5)

        self.stop_btn = tb.Button(controls, text="⏹ Stop",
                                  bootstyle="danger", command=self.stop_intercom,
                                  width=10, state=DISABLED)
        self.stop_btn.pack(side=LEFT, padx=5)

        tb.Button(controls, text="🔄 Cihazları Yenile",
                  bootstyle="secondary", command=self.refresh_devices).pack(side=RIGHT, padx=5)

    def start_intercom(self):
        if self.running:
            return

        try:
            mics = [self.parse_id(p["mic_var"].get()) for p in self.person_panels]
            outs = [self.parse_id(p["out_var"].get()) for p in self.person_panels]
        except Exception:
            messagebox.showwarning("Eksik Seçim", "Lütfen tüm mikrofon ve çıkışları seç.")
            return

        self.stop_event.clear()
        self.routers = []

        # --- VU callback üret (thread-safe)
        def make_vu_cb(vu_bar):
            def cb(level):
                # GUI güncellemesini main thread'e taşı
                self.root.after(0, lambda: vu_bar.configure(value=level))
            return cb

        # Routing:
        # Mic1 -> Out2, Out3
        self.routers.append(AudioRouter(
            self.p, mics[0], [outs[1], outs[2]],
            self.person_panels[0]["gain_var"],
            self.person_panels[0]["mute_var"],
            self.person_panels[0]["ptt_enabled_var"],
            self.person_panels[0]["ptt_pressed_var"],
            make_vu_cb(self.person_panels[0]["vu_bar"]),
            self.stop_event
        ))

        # Mic2 -> Out1, Out3
        self.routers.append(AudioRouter(
            self.p, mics[1], [outs[0], outs[2]],
            self.person_panels[1]["gain_var"],
            self.person_panels[1]["mute_var"],
            self.person_panels[1]["ptt_enabled_var"],
            self.person_panels[1]["ptt_pressed_var"],
            make_vu_cb(self.person_panels[1]["vu_bar"]),
            self.stop_event
        ))

        # Mic3 -> Out1, Out2
        self.routers.append(AudioRouter(
            self.p, mics[2], [outs[0], outs[1]],
            self.person_panels[2]["gain_var"],
            self.person_panels[2]["mute_var"],
            self.person_panels[2]["ptt_enabled_var"],
            self.person_panels[2]["ptt_pressed_var"],
            make_vu_cb(self.person_panels[2]["vu_bar"]),
            self.stop_event
        ))

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

        # VU meterları sıfırla
        for p in self.person_panels:
            p["vu_bar"].configure(value=0)

    def refresh_devices(self):
        if self.running:
            messagebox.showinfo("Çalışıyor", "Önce interkomu durdurmalısın.")
            return
        self.devices = self.get_devices()
        messagebox.showinfo("Yenilendi",
                            "Cihaz listesi yenilendi. Uygulamayı kapatıp açarsan listeler güncel görünür.")

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
