"""Busy Tag LED visualizer: reacts to whatever Windows is playing (WASAPI loopback).

Bottom LEDs (1+7): red flash on every kick.
Upper LEDs (2-6): offbeat pulse while kicks play, otherwise colour follows the played frequencies.
Display: cover art of the current track.

Usage:  python led_visualizer.py        (options: see --help)
Close the Busy Tag app first (it holds the COM port). Stop with Ctrl+C.
"""
import argparse
import colorsys
import threading
import time
from collections import deque

import numpy as np
import pyaudiowpatch as pa
import serial

import cover  # cover art of the current track -> display

ap = argparse.ArgumentParser()
ap.add_argument("--port", default="auto", help="COM port, default: find the Busy Tag automatically by its USB ID")
ap.add_argument("--seconds", type=float, default=0, help="0 = run until Ctrl+C")
ap.add_argument("--brightness", type=int, default=100, help="max LED brightness 1-100")
ap.add_argument("--no-cover", action="store_true", help="do not show the cover of the current track (TIDAL etc.) on the display")
ap.add_argument("--mids-color", default="0028FF", help="upper LEDs: 1st colour of the bar cycle as RRGGBB, default royal blue")
ap.add_argument("--highs-color", default="9000FF", help="upper LEDs: 3rd colour of the bar cycle as RRGGBB, default violet. 2nd colour = mix of both (indigo)")
ap.add_argument("--upper-budget", type=float, default=14, help="upper LEDs: max LED commands per second, the rest of the device bandwidth stays free for the kick")
ap.add_argument("--kick-hold", type=float, default=100, help="kick: min pause between kicks in ms, lower = more reactive")
ap.add_argument("--kick-decay", type=float, default=0.9, help="kick: fade per 21 ms step, closer to 1 = longer fade")
ap.add_argument("--bpm", type=float, default=128, help="upper LEDs: tempo used until it has been measured from the kicks")
ap.add_argument("--glow-min", type=float, default=0.2, help="upper LEDs without kick: minimum glow of the sides while music plays (0 = may go dark)")
ap.add_argument("--glow-gate", type=float, default=0.25, help="upper LEDs without kick: a row stays dark below this share of its recent peak")
ap.add_argument("--glow-rise", type=float, default=0.08, help="upper LEDs without kick: glow rise per 21 ms step")
ap.add_argument("--glow-fall", type=float, default=0.97, help="upper LEDs without kick: glow fall per 21 ms step")
ap.add_argument("--pulse-fall", type=float, default=0.85, help="upper LEDs: offbeat pulse fade per 21 ms step, closer to 1 = longer")
ap.add_argument("--upper-interval", type=float, default=0.03, help="upper LEDs: min seconds between updates, keeps bandwidth for the kick")
ap.add_argument("--upper-max", type=float, default=0.6, help="upper LEDs: max brightness 0-1, so the kick stands out")
ap.add_argument("--kick-share", type=float, default=0.12, help="kick: min share of 40-80 Hz in the 20-2000 Hz spectrum, higher = fewer triggers in quiet or vocal parts")
ap.add_argument("--kick-sens", type=float, default=4.0, help="kick: onset threshold, higher = fewer triggers")
ap.add_argument("--kick-peak", type=float, default=0.5, help="kick: min share of the recent strongest attack, higher = fewer triggers")
args = ap.parse_args()

# ---------- audio capture (loopback of default output device) ----------
p = pa.PyAudio()
dev = p.get_default_wasapi_loopback()
RATE = int(dev["defaultSampleRate"])
CH = dev["maxInputChannels"]
BLOCK = 1024  # hop: new analysis step every ~21 ms
blocks = deque(maxlen=2)  # only keep newest audio -> no lag


def on_audio(data, frames, t, status):
    x = np.frombuffer(data, dtype=np.float32).reshape(-1, CH).mean(axis=1)
    blocks.append(x)
    return (None, pa.paContinue)


stream = p.open(format=pa.paFloat32, channels=CH, rate=RATE, input=True,
                input_device_index=dev["index"], frames_per_buffer=BLOCK,
                stream_callback=on_audio)
print(f"Audio: {dev['name']} @ {RATE} Hz, {CH} ch")

# ---------- device ----------
def find_busytag():
    """Find the Busy Tag by its USB ID (VID 303A, PID 81DF), whatever COM number it got."""
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.vid == 0x303A and p.pid == 0x81DF:
            return p.device
    raise SystemExit("Busy Tag not found. Is it plugged in? (Boot mode does not count.)")


port = find_busytag() if args.port == "auto" else args.port
print(f"Busy Tag on {port}")
try:
    ser = serial.Serial(port, 460800, timeout=0, write_timeout=2)
except serial.SerialException:
    raise SystemExit(f"Cannot open {port}. Close the Busy Tag app or any other program using the port, then try again.")
ser.dtr = False
sent_count = 0
ORDER = [1, 7, 2, 6, 3, 4, 5]  # LED numbers, kick LEDs first
desired = {k: (0, 0, 0) for k in range(1, 8)}  # target colour per LED (set by analysis)
sent = {k: None for k in range(1, 8)}          # colour last sent per LED
stop_sender = threading.Event()


pending_cover = None  # PNG bytes waiting to be uploaded (set by the cover watcher)


def _sender():
    """Own thread: pushes only changed LEDs, never blocks the analysis loop.
    The device replies (OK) are drained, not waited for.
    Also uploads a new cover when one is pending (LEDs pause ~1.5 s, once per track)."""
    global sent_count, pending_cover
    while not stop_sender.is_set():
        if pending_cover is not None:
            data, pending_cover = pending_cover, None
            time.sleep(0.15)  # let the device finish queued LED commands first
            ok, dt = cover.upload_and_show(ser, data)
            print(f"Cover {'shown' if ok else 'FAILED'} ({len(data)//1024} KB, {dt:.1f}s)", flush=True)
            continue
        groups = {}
        for k in ORDER:
            if desired[k] != sent[k]:
                groups.setdefault(desired[k], []).append(k)
        for rgb, leds in groups.items():
            mask = sum(1 << (k - 1) for k in leds)
            ser.write(f"AT+SC={mask},{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}\r\n".encode())
            for k in leds:
                sent[k] = rgb
            sent_count += 1
        n = ser.in_waiting
        if n:
            ser.read(n)
        if not groups:
            time.sleep(0.001)


sender = threading.Thread(target=_sender, daemon=True)
sender.start()


def _cover_watcher():
    """Polls the Windows media session (TIDAL etc.) once per second.
    Queues an upload only when the cover image itself changes."""
    global pending_cover
    last = None
    while not stop_sender.is_set():
        try:
            _, img = cover.get_now_playing()
            if img and img != last:
                last = img
                pending_cover = cover.render(img)
        except Exception as e:  # media session unavailable etc.: just try again
            print(f"Cover: {e}", flush=True)
        stop_sender.wait(1.0)


if not args.no_cover:
    threading.Thread(target=_cover_watcher, daemon=True).start()


def set_led(mask, rgb):
    """Set target colour for LEDs in mask. Sending happens in the sender thread."""
    for k in range(1, 8):
        if mask & (1 << (k - 1)):
            desired[k] = rgb


send_led = set_led  # same thing now: last write wins per LED


def quant(v, step=12):
    return int(round(v / step) * step) if v < 245 else 255


# ---------- upper LEDs (kick mode) ----------
KICK_LEDS = 65  # mask of LED 1 + 7 (bottom)
ALL_UPPER = sum(1 << (k - 1) for k in (2, 3, 4, 5, 6))
UPPER_BAND = (300, 14000)  # energy above the kick: makes pulses stronger in dense parts
last_upper = 0.0
seen_kick = 0.0            # last kick time already processed here (read-only use of the kick)
kick_times = deque(maxlen=9)
next_off = None            # time of the next offbeat pulse
beat_count = 0
colour_idx = 0
pulse = pulse_target = 0.0
e_short = e_long = 0.0
e_idx = None
pulses = pulse_steps = pulse_lit = 0  # stats, printed at exit
glow_steps = glow_lit = 0    # stats for parts without kick
ROWS = [[2, 6], [3, 5], [4]]   # glow: bottom -> top
GLOW_BANDS = [(150, 1000), (1000, 4000), (4000, 14000)]  # sides / upper sides / top
GLOW_SHIFT = [0.0, 0.12, 0.24]  # rows further up lean more to violet
glow_idx = None
c_idx = c_logf = None
band_s = [0.0, 0.0, 0.0]
band_peak = [0.0, 0.0, 0.0]
glow_lv = [0.0, 0.0, 0.0]
cent = 0.3                 # 0 = low/dark sound (blue) ... 1 = bright sound (violet)
up_tokens = 0.0
up_tok_time = 0.0


def mask_of(leds):
    return sum(1 << (k - 1) for k in leds)


def _lvl(v):
    """5 brightness levels (30-100 % of --upper-max), off below.
    Few levels = few commands per pulse; no very dim levels (colours drift there)."""
    if v < 0.12:
        return 0.0
    steps = round((min(1.0, v) - 0.12) / 0.88 * 4) / 4
    return (0.3 + 0.7 * steps) * args.upper_max


def _rgb(hexcol, level):
    """Scale a fixed colour, keeping its channel ratios exact (no per-channel rounding)."""
    s = level * args.brightness / 100
    return tuple(int(round(int(hexcol[i:i + 2], 16) * s)) for i in (0, 2, 4))


def _mix(a, b):
    """Colour halfway between two RRGGBB colours."""
    return "".join(f"{(int(a[i:i + 2], 16) + int(b[i:i + 2], 16)) // 2:02X}" for i in (0, 2, 4))


def _blend(t):
    """Colour along blue -> indigo -> violet for t in 0..1 (6 steps, fewer commands)."""
    t = round(min(1.0, max(0.0, t)) * 6) / 6
    a, b = (args.mids_color, _mix(args.mids_color, args.highs_color)) if t < 0.5 else            (_mix(args.mids_color, args.highs_color), args.highs_color)
    u = t * 2 if t < 0.5 else t * 2 - 1
    return "".join(f"{round(int(a[i:i + 2], 16) * (1 - u) + int(b[i:i + 2], 16) * u):02X}" for i in (0, 2, 4))


def _period():
    """Beat period from recent kick intervals (100-170 BPM), else --bpm."""
    ts = list(kick_times)
    iv = sorted(b - a for a, b in zip(ts, ts[1:]) if 0.35 <= b - a <= 0.6)
    return iv[len(iv) // 2] if len(iv) >= 3 else 60.0 / args.bpm


def upper_update(spec, now):
    """Upper LEDs: one soft pulse on every offbeat (halfway between two kicks),
    colour changes every bar (4 kicks): blue -> indigo -> violet.
    Only reads the kick time; the kick code itself is not touched."""
    global last_upper, seen_kick, next_off, beat_count, colour_idx, pulse, pulse_target
    global e_short, e_long, e_idx, pulses, pulse_steps, pulse_lit, up_tokens, up_tok_time
    global glow_steps, glow_lit, glow_idx, c_idx, c_logf, cent

    # energy above the kick -> pulse strength (stronger in dense parts)
    if e_idx is None:
        e_idx = (freqs >= UPPER_BAND[0]) & (freqs < UPPER_BAND[1])
    e = spec[e_idx].mean()
    if e_long == 0.0:
        e_short = e_long = e
    e_short = e_short * 0.9 + e * 0.1
    e_long = e_long * 0.998 + e * 0.002
    ratio = e_short / (e_long + 1e-9)
    strength = 0.6 + 0.4 * min(1.0, max(0.0, (ratio - 0.8) / 0.8))

    # parts without kick: colour follows where the sound sits (spectral centroid),
    # each row follows its own band: sides = low mids, upper sides = mids, top = highs
    if glow_idx is None:
        glow_idx = [(freqs >= a) & (freqs < b) for a, b in GLOW_BANDS]
        c_idx = (freqs >= 150) & (freqs < 14000)
        c_logf = np.log2(freqs[c_idx])
    sc = spec[c_idx]
    if sc.sum() > 1e-9:
        c = 2 ** float((sc * c_logf).sum() / sc.sum())                 # centroid in Hz
        ct = min(1.0, max(0.0, np.log2(c / 500) / np.log2(4000 / 500)))  # 500 Hz..4 kHz -> 0..1
        cent = cent * 0.96 + ct * 0.04                                    # smooth, ~0.5 s
    for i in range(3):
        band_s[i] = band_s[i] * 0.8 + spec[glow_idx[i]].mean() * 0.2
        band_peak[i] = max(band_s[i], band_peak[i] * 0.9995, band_peak[0] * 0.01)
        t = min(1.0, max(0.0, (band_s[i] / (band_peak[i] + 1e-9) - args.glow_gate) / (0.85 - args.glow_gate)))
        if i == 0 and band_s[0] > 1e-4:
            t = max(t, args.glow_min)
        if t > glow_lv[i]:
            glow_lv[i] += (t - glow_lv[i]) * args.glow_rise
        else:
            glow_lv[i] = glow_lv[i] * args.glow_fall + t * (1 - args.glow_fall)

    # new kick seen -> schedule the offbeat
    period = _period()
    if last_kick != seen_kick and last_kick > 0:
        seen_kick = last_kick
        kick_times.append(last_kick)
        next_off = last_kick + period / 2
        beat_count += 1
        if beat_count % 4 == 0:
            colour_idx = (colour_idx + 1) % 3
    if next_off is not None and now >= next_off:
        next_off = None
        pulse_target = strength
        pulses += 1

    # soft rise to the pulse, then fade out before the next kick
    if pulse_target > 0:
        pulse += (pulse_target - pulse) * 0.6
        if pulse >= pulse_target * 0.9:
            pulse_target = 0.0
    else:
        pulse *= args.pulse_fall
    beat_on = seen_kick > 0 and now - seen_kick < 2.5 * period  # kicks running -> offbeat, else glow
    if beat_on:
        pulse_steps += 1
        pulse_lit += _lvl(pulse) > 0
    else:
        glow_steps += 1
        glow_lit += _lvl(glow_lv[0]) > 0 or _lvl(glow_lv[1]) > 0 or _lvl(glow_lv[2]) > 0

    # output: throttled and with a command budget, so the kick never waits
    if now - last_upper < args.upper_interval:
        return
    last_upper = now
    up_tokens = min(3.0, up_tokens + (now - up_tok_time) * args.upper_budget)
    up_tok_time = now
    colours = [args.mids_color, _mix(args.mids_color, args.highs_color), args.highs_color]
    if beat_on:  # with kick: all upper LEDs pulse on the offbeat, colour per bar
        rgb = _rgb(colours[colour_idx], _lvl(pulse))
        if desired[2] != rgb and up_tokens >= 1:
            up_tokens -= 1
            send_led(ALL_UPPER, rgb)
        return
    # without kick: colour from the played frequencies, brightness per row from its band
    for leds, lv, shift in zip(ROWS, glow_lv, GLOW_SHIFT):
        rgb = _rgb(_blend(cent + shift), _lvl(lv))
        if desired[leds[0]] == rgb:
            continue
        if up_tokens < 1:
            break
        up_tokens -= 1
        send_led(mask_of(leds), rgb)


# ---------- analysis ----------
N = 2048  # analysis window: ~43 ms, ~23 Hz per bin, advanced every BLOCK samples
win = np.ones(N)
_ramp = np.hanning(512)
win[:256] = _ramp[:256]  # taper only the old start; newest samples stay untapered
freqs = np.fft.rfftfreq(N, 1 / RATE)
ring = np.zeros(N, dtype=np.float32)  # rolling window of the newest N samples
kick_idx = (freqs >= 40) & (freqs < 80)  # 2 bins at 23 Hz resolution (47 and 70 Hz)
total_idx = (freqs >= 20) & (freqs < 2000)  # reference for the kick's share of the whole spectrum
prev_kick_bass = 0.0
max_flux = 0.0
mean_flux = 0.0
last_kick = 0.0

agc_bass = 1e-4
env = 0.0
frames = 0
start = time.time()
last_audio = 0.0
scale = args.brightness / 100


def hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v * scale)
    return (quant(r * 255), quant(g * 255), quant(b * 255))


try:
    while args.seconds == 0 or time.time() - start < args.seconds:
        if not blocks:
            time.sleep(0.005)
            real_silence = time.time() - last_audio >= 0.25
            # gap between audio blocks: kick fades as before, upper LEDs are left alone
            env *= 0.9
            set_led(KICK_LEDS, hsv(0.0, 1, env))
            if real_silence:
                send_led(ALL_UPPER, (0, 0, 0))
            continue
        x = blocks.pop()
        blocks.clear()
        last_audio = time.time()
        if len(x) < BLOCK:
            continue
        ring = np.concatenate([ring[len(x):], x])  # slide window by the newest block
        spec = np.abs(np.fft.rfft(ring * win))
        frames += 1

        # Kick = sudden rise of 40-80 Hz energy (onset). Sustained synth bass has no onset.
        bass = spec[kick_idx].mean()
        share = spec[kick_idx].sum() / (spec[total_idx].sum() + 1e-9)  # kick share of the spectrum
        flux = max(0.0, bass - prev_kick_bass)
        prev_kick_bass = bass
        mean_flux = mean_flux * 0.93 + flux * 0.07
        max_flux = max(flux, max_flux * 0.98)  # slowly decaying peak of recent onsets
        agc_bass = max(bass, agc_bass * 0.995)
        now = time.time()
        if (flux > args.kick_sens * mean_flux + 1e-3
                and flux >= args.kick_peak * max_flux  # only strong recent attacks
                and bass > 0.3 * agc_bass
                and share >= args.kick_share
                and now - last_kick > args.kick_hold / 1000):
            # strength: strongest attacks full brightness, weaker ones dimmed
            env = 0.3 + 0.7 * min(1.0, flux / max_flux) if max_flux > 0 else 1.0
            last_kick = now
        else:
            env *= args.kick_decay  # per 21 ms step: softer tail than a blink
        set_led(KICK_LEDS, hsv(0.0, 1, env))                      # kick: red
        upper_update(spec, now)
except KeyboardInterrupt:
    pass
finally:
    stream.stop_stream()
    stream.close()
    p.terminate()
    for k in desired:
        desired[k] = (0, 0, 0)
    time.sleep(0.3)  # let the sender push the "off" state
    stop_sender.set()
    sender.join(timeout=1)
    ser.write(b"AT+SC=127,000000\r\n")
    time.sleep(0.1)
    ser.close()
    dur = max(time.time() - start, 1e-6)
    print(f"{frames} audio blocks, {sent_count} LED commands in {dur:.1f}s "
          f"({sent_count/dur:.0f}/s). LEDs off.")
    if pulse_steps:
        print(f"  offbeat: {pulses} pulses ({pulses/dur:.1f}/s), tempo {60/_period():.0f} BPM, "
              f"lit {100*pulse_lit/pulse_steps:.0f}% of the time")
    if glow_steps:
        print(f"  glow (no kick): {glow_steps*BLOCK/RATE:.1f}s, lit {100*glow_lit/glow_steps:.0f}% of that time")
