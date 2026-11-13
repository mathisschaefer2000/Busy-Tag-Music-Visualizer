"""Cover art from the Windows media session (e.g. TIDAL) -> Busy Tag display.

get_now_playing() -> (track_key, png_bytes or None)
render(png_bytes)  -> PNG bytes filling the 240x280 display (sides cropped)
upload_and_show(ser, data) -> uploads via AT+UF (to the file not on screen) and shows via AT+SP
"""
import asyncio
import io
import time

from PIL import Image
from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
from winrt.windows.storage.streams import DataReader

W, H = 240, 280


async def _now_playing():
    mgr = await _Mgr.request_async()
    s = mgr.get_current_session()
    if s is None:
        return None, None
    p = await s.try_get_media_properties_async()
    key = f"{p.artist} - {p.title}"
    if not p.thumbnail:
        return key, None
    st = await p.thumbnail.open_read_async()
    r = DataReader(st.get_input_stream_at(0))
    await r.load_async(st.size)
    buf = bytearray(st.size)
    r.read_bytes(buf)
    return key, bytes(buf)


def get_now_playing():
    return asyncio.run(_now_playing())


def render(img_bytes):
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    im = im.resize((H, H), Image.LANCZOS)  # scale to full height (280x280) ...
    left = (H - W) // 2
    im = im.crop((left, 0, left + W, H))   # ... and crop the sides to 240 wide
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def _read_until(ser, tokens, timeout):
    buf = b""
    t = time.time()
    while time.time() - t < timeout:
        n = ser.in_waiting
        if n:
            buf += ser.read(n)
            if any(tok in buf for tok in tokens):
                return buf
        else:
            time.sleep(0.002)
    return buf


NAMES = ("cover_a.png", "cover_b.png")


def upload_and_show(ser, data):
    """Upload file and show it. Returns (ok, seconds). Caller must own the port exclusively.
    Writes to the cover file that is NOT on screen: the device refuses to overwrite
    the shown file (ERROR:3) and would then read the image bytes as commands."""
    t0 = time.time()
    ser.reset_input_buffer()
    ser.write(b"AT+SP?\r\n")
    shown = _read_until(ser, [b"OK", b"ERROR"], 2)
    name = NAMES[1] if NAMES[0].encode() in shown else NAMES[0]
    ser.reset_input_buffer()
    ser.write(f"AT+UF={name},{len(data)}\r\n".encode())
    r = _read_until(ser, [b">", b"ERROR"], 3)
    if b">" not in r:
        return False, time.time() - t0
    # USB sends 64-byte packets: if the payload ends exactly on a packet boundary,
    # the device keeps waiting. Send the last byte separately in that case.
    body, tail = (data[:-1], data[-1:]) if len(data) % 64 == 0 else (data, b"")
    for i in range(0, len(body), 32 * 1024):
        ser.write(body[i:i + 32 * 1024])
    if tail:
        time.sleep(0.02)
        ser.write(tail)
    r = _read_until(ser, [b"OK", b"ERROR"], 10)
    if b"OK" not in r:
        return False, time.time() - t0
    ser.write(f"AT+SP={name}\r\n".encode())
    r = _read_until(ser, [b"OK", b"ERROR"], 3)
    return b"OK" in r, time.time() - t0
