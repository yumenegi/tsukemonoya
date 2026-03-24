"""
Takuan Synth Web Server — PYNQ-Z2
Flask server that bridges the browser UI to the FPGA synth engine via AXI MMIO.
MIDI input provides key-on/key-off; all other parameters come from the web UI.
"""
from flask import Flask, render_template, request, jsonify, send_from_directory
import os
import re
import sys
import time
import threading
import numpy as np

# ---------------------------------------------------------------------------
# Hardware imports (only available on the PYNQ board)
# ---------------------------------------------------------------------------
try:
    import pynq
    import pynq.lib
    HW_AVAILABLE = True
except ImportError:
    HW_AVAILABLE = False
    print("WARNING: pynq not available — running in stub mode (no hardware writes)")

from engine import Engine, midi_to_freq, freq_to_stride, note_to_name
from synth_state import SynthState, LFO_SHAPES, LFO_SHAPE_NAMES, _lfo_speed_to_stride
from voice_alloc import VoiceAllocator

# ---------------------------------------------------------------------------
# ADAU1761 codec config (same as play_midi.py)
# ---------------------------------------------------------------------------
ADAU1761_CONFIG = [
    (0x4000, 0x01), (0x4015, 0x00), (0x4016, 0x00), (0x4017, 0x00),
    (0x4018, 0x00), (0x4019, 0x13), (0x401A, 0x00), (0x401B, 0x00),
    (0x401C, 0x21), (0x401E, 0x41), (0x4023, 0xFF), (0x4024, 0xFF),
    (0x4029, 0x03), (0x402A, 0x03), (0x402B, 0x00), (0x402C, 0x00),
    (0x40F2, 0x01), (0x40F3, 0x01), (0x40F9, 0x7F), (0x40FA, 0x03),
]

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder='static', template_folder='templates')

engine = None       # type: Engine
state = SynthState()
wt1_alloc = VoiceAllocator(start_index=0, voice_count=256)
wt2_alloc = VoiceAllocator(start_index=128, voice_count=128)
bram0 = None        # BRAM for WT1 (128 frames)
bram1 = None        # BRAM for WT2 (1 frame)

# ---------------------------------------------------------------------------
# Hardware init
# ---------------------------------------------------------------------------
def init_codec(overlay):
    """Initialize the ADAU1761 codec via I2C."""
    try:
        i2c = pynq.lib.AxiIIC(overlay.ip_dict['axi_iic_0'])
        is_axi = True
    except KeyError:
        class PSI2C:
            def __init__(self, bus=0):
                self.bus = bus
            def write(self, addr, data):
                cmd = f"i2cset -y {self.bus} 0x{addr:02X} 0x{data[0]:02X} 0x{data[1]:02X} 0x{data[2]:02X} i"
                os.system(cmd)
        i2c = PSI2C(0)
        is_axi = False

    reg, val = ADAU1761_CONFIG[0]
    if is_axi:
        i2c.send(0x3B, [reg >> 8, reg & 0xFF, val], 3)
    else:
        i2c.write(0x3B, [reg >> 8, reg & 0xFF, val])
    time.sleep(0.5)

    for reg, val in ADAU1761_CONFIG[1:]:
        if is_axi:
            i2c.send(0x3B, [reg >> 8, reg & 0xFF, val], 3)
        else:
            i2c.write(0x3B, [reg >> 8, reg & 0xFF, val])
        time.sleep(0.01)


def load_wavetable_to_bram(bram, wav_path, num_slices=128, samples_per_slice=2048):
    """Load a Serum-format .wav into BRAM with frame downsampling."""
    from scipy.io import wavfile
    sr, data = wavfile.read(wav_path)

    if len(data.shape) > 1:
        data = data[:, 0]
    if data.dtype == np.float32 or data.dtype == np.float64:
        data = np.clip(data, -1.0, 1.0)
        data = (data * 32767.0).astype(np.int16)
    elif data.dtype == np.int32:
        data = (data >> 16).astype(np.int16)
    elif data.dtype != np.int16:
        data = data.astype(np.int16)

    target_total = samples_per_slice * num_slices
    actual_frames = len(data) // samples_per_slice

    if actual_frames > num_slices:
        # Downsample frames evenly
        indices = np.linspace(0, actual_frames - 1, num_slices).astype(int)
        new_data = np.zeros(target_total, dtype=data.dtype)
        for i, fi in enumerate(indices):
            start = fi * samples_per_slice
            new_data[i * samples_per_slice:(i + 1) * samples_per_slice] = data[start:start + samples_per_slice]
        data = new_data
    elif len(data) < target_total:
        data = np.pad(data, (0, target_total - len(data)), 'constant')
    else:
        data = data[:target_total]

    # Pack pairs: [31:16] = even, [15:0] = odd
    data_pairs = data.reshape(-1, 2)
    even = (data_pairs[:, 0].astype(np.int32) & 0xFFFF).astype(np.uint32)
    odd  = (data_pairs[:, 1].astype(np.int32) & 0xFFFF).astype(np.uint32)
    words = (even << 16) | odd

    for i, word in enumerate(words):
        bram.write(i * 4, int(word))

    print(f"  Loaded {num_slices} × {samples_per_slice} samples into BRAM")


def load_default_wavetable(bram, num_slices=128, samples_per_slice=2048):
    """Generate a sawtooth fallback wavetable."""
    target_total = samples_per_slice * num_slices
    one_frame = np.linspace(-32768, 32767, samples_per_slice, dtype=np.int16)
    data = np.tile(one_frame, num_slices)

    data_pairs = data.reshape(-1, 2)
    even = (data_pairs[:, 0].astype(np.int32) & 0xFFFF).astype(np.uint32)
    odd  = (data_pairs[:, 1].astype(np.int32) & 0xFFFF).astype(np.uint32)
    words = (even << 16) | odd

    for i, word in enumerate(words):
        bram.write(i * 4, int(word))

    print(f"  Loaded default sawtooth ({num_slices} frames)")


def init_hardware():
    """Initialize PYNQ overlay, codec, engine, envelopes, and LFOs."""
    global engine, bram0, bram1

    print("Loading overlay...")
    overlay = pynq.Overlay("/home/xilinx/hw/kai_san/takuan.bit")

    print("Initializing ADAU1761 codec...")
    init_codec(overlay)

    bram0 = overlay.axi_bram_ctrl_0
    bram1 = overlay.axi_bram_ctrl_1

    # Load default wavetables
    print("Loading default wavetables...")
    load_default_wavetable(bram0, num_slices=128)
    load_default_wavetable(bram1, num_slices=1)

    # Init engine
    synth = overlay.umeboshi_0
    engine = Engine(synth)
    engine.reset()

    # Write default envelopes
    for i in range(8):
        adsr1, adsr2 = state.compute_adsr(i)
        engine.write_adsr(i, adsr1, adsr2)

    # Write default LFO shapes and strides
    for i in range(4):
        engine.write_lfo_shape(i, state.get_scaled_lfo_shape(i))
        engine.write_lfo_stride(i, _lfo_speed_to_stride(state.lfo_speeds[i]))

    print("Hardware initialized!")


# ---------------------------------------------------------------------------
# MIDI thread
# ---------------------------------------------------------------------------
def _play_test_scale():
    """Fallback: loop a C major scale when no MIDI device is available."""
    # C4=60, D4=62, E4=64, F4=65, G4=67, A4=69, B4=71, C5=72
    c_major = [60, 62, 64, 65, 67, 69, 71, 72]
    print("TEST MODE: Playing C major scale in a loop")
    while True:
        for note in c_major:
            _handle_note_on(note)
            time.sleep(0.4)
            _handle_note_off(note)
            time.sleep(0.1)
        time.sleep(0.5)


def midi_thread_func():
    """Background thread listening for MIDI note on/off events."""
    try:
        import rtmidi
    except ImportError:
        print("WARNING: python-rtmidi not installed — falling back to test scale")
        _play_test_scale()
        return

    midi_in = rtmidi.MidiIn()
    ports = midi_in.get_ports()
    if not ports:
        print("WARNING: No MIDI input ports found — falling back to test scale")
        _play_test_scale()
        return

    midi_in.open_port(0)
    print(f"MIDI: Listening on '{ports[0]}'")

    while True:
        msg = midi_in.get_message()
        if msg:
            message, _delta = msg
            status = message[0] & 0xF0
            note = message[1]
            velocity = message[2] if len(message) > 2 else 0

            if status == 0x90 and velocity > 0:
                _handle_note_on(note)
            elif status == 0x80 or (status == 0x90 and velocity == 0):
                _handle_note_off(note)
        else:
            time.sleep(0.001)


def _handle_note_on(note):
    """Allocate voices and write registers for a note-on event."""
    if engine is None:
        return

    freq = midi_to_freq(note)
    stride = freq_to_stride(freq)

    # WT1 voices
    wt1_indices = wt1_alloc.allocate(note)
    if wt1_indices:
        wt_id = state.get_wt1_wt_id()
        env_id = state.wt1_env_select
        lfo_ctrl = state.get_lfo_ctrl_wt1()
        for hw_idx in wt1_indices:
            engine.write_voice(hw_idx, stride, wt_id, env_id, 1, lfo_ctrl)

    # WT2 voices (only if dual mode)
    if state.wt2_on:
        wt2_indices = wt2_alloc.allocate(note)
        if wt2_indices:
            wt_id = state.get_wt2_wt_id()
            env_id = state.wt2_env_select
            lfo_ctrl = state.get_lfo_ctrl_wt2()
            for hw_idx in wt2_indices:
                engine.write_voice(hw_idx, stride, wt_id, env_id, 1, lfo_ctrl)

    print(f"NOTE ON: {note_to_name(note)} ({note})")


def _handle_note_off(note):
    """Release voices for a note-off event."""
    if engine is None:
        return

    wt1_indices = wt1_alloc.release(note)
    if wt1_indices:
        for hw_idx in wt1_indices:
            engine.key_off(hw_idx)

    if state.wt2_on:
        wt2_indices = wt2_alloc.release(note)
        if wt2_indices:
            for hw_idx in wt2_indices:
                engine.key_off(hw_idx)

    print(f"NOTE OFF: {note_to_name(note)} ({note})")


# ---------------------------------------------------------------------------
# Param dispatch — maps UI param strings to hardware writes
# ---------------------------------------------------------------------------
# Regex patterns for param matching
RE_ENV = re.compile(r'^env_(\d+)_([adsr])$')
RE_LFO_SPEED = re.compile(r'^lfo_(\d+)_speed$')
RE_LFO_SHAPE = re.compile(r'^lfo_(\d+)_shape$')
RE_LFO_AMP = re.compile(r'^lfo_(\d+)_amp$')

def _invert_switch(value):
    """Invert switch value: 0 = ON, 1 = OFF per SERVER_API.md."""
    return int(value) == 0


def _rewrite_lfo(lfo_idx):
    """Rewrite LFO shape to hardware with current amplitude scaling."""
    if engine:
        scaled = state.get_scaled_lfo_shape(lfo_idx)
        engine.write_lfo_shape(lfo_idx, scaled)


def dispatch_control(param, value):
    """Route a UI parameter change to the appropriate engine write."""
    global engine

    # --- Envelope ADSR ---
    m = RE_ENV.match(param)
    if m:
        env_idx = int(m.group(1)) - 1  # UI is 1-indexed
        adsr_param = m.group(2)
        if 0 <= env_idx < 8:
            state.update_envelope(env_idx, adsr_param, value)
            if engine:
                adsr1, adsr2 = state.compute_adsr(env_idx)
                engine.write_adsr(env_idx, adsr1, adsr2)
        return

    # --- LFO Speed ---
    m = RE_LFO_SPEED.match(param)
    if m:
        lfo_idx = int(m.group(1)) - 1  # UI is 1-indexed
        if 0 <= lfo_idx < 4:
            state.update_lfo_speed(lfo_idx, value)
            if engine:
                engine.write_lfo_stride(lfo_idx, _lfo_speed_to_stride(int(float(value))))
        return

    # --- LFO Shape ---
    m = RE_LFO_SHAPE.match(param)
    if m:
        lfo_idx = int(m.group(1)) - 1
        if 0 <= lfo_idx < 4:
            state.update_lfo_shape(lfo_idx, str(value))
            _rewrite_lfo(lfo_idx)
        return

    # --- LFO Amplitude ---
    m = RE_LFO_AMP.match(param)
    if m:
        lfo_idx = int(m.group(1)) - 1
        if 0 <= lfo_idx < 4:
            state.update_lfo_amp(lfo_idx, value)
            _rewrite_lfo(lfo_idx)
        return

    # --- WT1 Controls ---
    if param == 'wt_pos':
        with state.lock:
            state.wt1_pos = int(float(value))
        # Update wt_id for all active WT1 voices
        if engine:
            wt_id = state.get_wt1_wt_id()
            for hw_idx in wt1_alloc.get_all_active_indices():
                engine.write_wt_id(hw_idx, wt_id)
        return

    if param == 'wt_env_select':
        with state.lock:
            state.wt1_env_select = int(float(value))
        if engine:
            for hw_idx in wt1_alloc.get_all_active_indices():
                engine.write_envelope_id(hw_idx, state.wt1_env_select)
        return

    if param == 'wt_lfo_on':
        with state.lock:
            state.wt1_pitch_lfo_en = _invert_switch(value)
        _update_wt1_lfo_ctrl()
        return

    if param == 'wt_lfo_pitch':
        with state.lock:
            state.wt1_pitch_lfo_idx = int(float(value))
        _update_wt1_lfo_ctrl()
        return

    if param == 'wt_pitch_mode':
        with state.lock:
            state.wt1_pitch_trig = _invert_switch(value)
        _update_wt1_lfo_ctrl()
        return

    if param == 'wt_lfo_pos_on':
        with state.lock:
            state.wt1_pos_lfo_en = _invert_switch(value)
        _update_wt1_lfo_ctrl()
        return

    if param == 'wt_lfo_pos':
        with state.lock:
            state.wt1_pos_lfo_idx = int(float(value))
        _update_wt1_lfo_ctrl()
        return

    if param == 'wt_pos_mode':
        with state.lock:
            state.wt1_pos_trig = _invert_switch(value)
        _update_wt1_lfo_ctrl()
        return

    if param == 'wt_wave_select':
        _load_wt_file(str(value), is_wt2=False)
        return

    # --- WT2 Controls ---
    if param == 'wt2_on':
        new_state = _invert_switch(value)
        _toggle_wt2(new_state)
        return

    if param == 'wt2_env_select':
        with state.lock:
            state.wt2_env_select = int(float(value))
        if engine and state.wt2_on:
            for hw_idx in wt2_alloc.get_all_active_indices():
                engine.write_envelope_id(hw_idx, state.wt2_env_select)
        return

    if param == 'wt2_lfo_on':
        with state.lock:
            state.wt2_pitch_lfo_en = _invert_switch(value)
        _update_wt2_lfo_ctrl()
        return

    if param == 'wt2_lfo_pitch':
        with state.lock:
            state.wt2_pitch_lfo_idx = int(float(value))
        _update_wt2_lfo_ctrl()
        return

    if param == 'wt2_pitch_mode':
        with state.lock:
            state.wt2_pitch_trig = _invert_switch(value)
        _update_wt2_lfo_ctrl()
        return

    if param == 'wt2_wave_select':
        _load_wt_file(str(value), is_wt2=True)
        return

    print(f"  Unhandled param: {param} = {value}")


def _update_wt1_lfo_ctrl():
    """Recompute and write LFO ctrl byte for all active WT1 voices."""
    if engine:
        ctrl = state.get_lfo_ctrl_wt1()
        for hw_idx in wt1_alloc.get_all_active_indices():
            engine.write_lfo_ctrl(hw_idx, ctrl)


def _update_wt2_lfo_ctrl():
    """Recompute and write LFO ctrl byte for all active WT2 voices."""
    if engine and state.wt2_on:
        ctrl = state.get_lfo_ctrl_wt2()
        for hw_idx in wt2_alloc.get_all_active_indices():
            engine.write_lfo_ctrl(hw_idx, ctrl)


def _toggle_wt2(enabled):
    """Enable or disable WT2 dual-channel mode."""
    if engine is None:
        with state.lock:
            state.wt2_on = enabled
        return

    with state.lock:
        was_on = state.wt2_on
        state.wt2_on = enabled

    if enabled and not was_on:
        # Switch to dual mode: WT1 gets 128 voices (0-127), WT2 gets 128 voices (128-255)
        # Release all current voices first
        for hw_idx in wt1_alloc.release_all():
            engine.key_off(hw_idx)
        for hw_idx in wt2_alloc.release_all():
            engine.key_off(hw_idx)

        wt1_alloc.resize(start_index=0, voice_count=128)
        wt2_alloc.resize(start_index=128, voice_count=128)
        print("MODE: Dual channel (WT1: 128 voices, WT2: 128 voices)")

    elif not enabled and was_on:
        # Switch to single mode: WT1 gets all 256 voices
        for hw_idx in wt1_alloc.release_all():
            engine.key_off(hw_idx)
        for hw_idx in wt2_alloc.release_all():
            engine.key_off(hw_idx)

        wt1_alloc.resize(start_index=0, voice_count=256)
        print("MODE: Single channel (WT1: 256 voices)")


def _load_wt_file(filename, is_wt2=False):
    """Load a .wav wavetable file into the appropriate BRAM."""
    target_bram = bram1 if is_wt2 else bram0
    num_slices = 1 if is_wt2 else 128

    if target_bram is None:
        print(f"  BRAM not available for {'WT2' if is_wt2 else 'WT1'}")
        return

    wav_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wav')
    wav_path = os.path.join(wav_dir, filename)

    if not os.path.exists(wav_path):
        print(f"  File not found: {wav_path}")
        return

    print(f"  Loading {'WT2' if is_wt2 else 'WT1'}: {filename}")
    try:
        load_wavetable_to_bram(target_bram, wav_path, num_slices=num_slices)
        with state.lock:
            if is_wt2:
                state.wt2_wave_file = filename
            else:
                state.wt1_wave_file = filename
    except Exception as e:
        print(f"  Error loading wavetable: {e}")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/control', methods=['POST'])
def control():
    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "No JSON data provided"}), 400

    param = data.get('param')
    value = data.get('value')

    print(f"Control | {param} = {value}")
    dispatch_control(param, value)

    return jsonify({"status": "success", "param": param, "value": value})


@app.route('/api/lfo_shapes', methods=['GET'])
def get_lfo_shapes():
    """Return available LFO shape names for the dropdown."""
    return jsonify(LFO_SHAPE_NAMES)


@app.route('/webaudio-controls/<path:filename>')
def webaudio_controls(filename):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(base_dir, '..', 'webaudio-controls')
    return send_from_directory(target_dir, filename)


@app.route('/wav/<path:filename>')
def serve_wav(filename):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(base_dir, 'wav')
    return send_from_directory(target_dir, filename)


@app.route('/api/wavetables', methods=['GET'])
def list_wavetables():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    target_dir = os.path.join(base_dir, 'wav')
    if not os.path.exists(target_dir):
        return jsonify([])
    files = [f for f in os.listdir(target_dir) if f.lower().endswith('.wav')]
    return jsonify(files)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    if HW_AVAILABLE:
        init_hardware()

    # Start MIDI thread
    midi_t = threading.Thread(target=midi_thread_func, daemon=True)
    midi_t.start()

    app.run(host='0.0.0.0', port=5000, debug=False)
