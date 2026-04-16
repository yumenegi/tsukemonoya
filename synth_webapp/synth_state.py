"""
SynthState — Centralized mutable state for the Takuan web synth.
Holds all parameters controlled by the browser UI and provides
methods to compute hardware register values.
"""
import threading
import numpy as np

SAMPLING_RATE = 95274.390625  # actual fs from Zynq PLL
MAX_UINT_24 = 16777216
SCALING_RATE = SAMPLING_RATE * 16  # env runs at audio rate, 16 ops per tick

# Hardcoded LFO shape library (512 samples each, uint8)
def _make_lfo_shapes():
    t = np.linspace(0, 2 * np.pi, 512, endpoint=False)
    shapes = {
        'flat':     np.zeros(512, dtype=np.uint8),
        'sine':     ((np.sin(t) * 127) + 128).astype(np.uint8),      # unsigned sine 0-255
        'sine_bi':  (np.sin(t) * 127).astype(np.int8).view(np.uint8), # signed sine for pitch
        'triangle': np.concatenate([
            np.linspace(0, 255, 256, dtype=np.uint8),
            np.linspace(255, 0, 256, dtype=np.uint8)
        ]),
        'ramp_up':  np.linspace(0, 255, 512, dtype=np.uint8),
        'ramp_down': np.linspace(255, 0, 512, dtype=np.uint8),
        'square':   np.where(np.arange(512) < 256, np.uint8(255), np.uint8(0)).astype(np.uint8),
    }
    return shapes

LFO_SHAPES = _make_lfo_shapes()
LFO_SHAPE_NAMES = list(LFO_SHAPES.keys())


def pack_adsr1(ar, ar_rs, dr, dr_rs):
    return (dr_rs << 24) | (dr << 16) | (ar_rs << 8) | ar

def pack_adsr2(sl, rr, rr_rs):
    return (rr_rs << 24) | (rr << 16) | sl

def _time_to_rate(seconds):
    """Convert time in seconds to (rate_8bit, shift_4bit) for ADSR hardware."""
    if seconds <= 0:
        seconds = 0.001
    raw = int(MAX_UINT_24 / (SCALING_RATE * seconds))
    rs = max(0, raw.bit_length() - 8)
    rate = min(255, raw >> rs)
    rs = min(15, rs)
    return rate, rs

def _sustain_float_to_hw(s_float):
    """Convert sustain 0.0-1.0 to 16-bit hardware value."""
    return int(max(0.0, min(1.0, s_float)) * 0xFFFF)

def _lfo_speed_to_stride(freq_hz):
    """Convert LFO frequency in Hz to phase accumulator stride."""
    freq = max(0.0, float(freq_hz))
    if freq == 0.0:
        return 0
    return int((freq * 4294967296) / SAMPLING_RATE)


class SynthState:
    """Thread-safe mutable synth state driven by the web UI."""

    def __init__(self):
        self.lock = threading.Lock()

        # --- Envelopes (8 envelopes, ADSR in ms / float) ---
        # Defaults: fast attack, short decay, high sustain, medium release
        self.envelopes = []
        for _ in range(8):
            self.envelopes.append({
                'a': 10,    # ms
                'd': 100,   # ms
                's': 0.87,  # 0.0-1.0
                'r': 100,   # ms
            })

        # --- LFOs (4 LFOs) ---
        self.lfo_speeds = [5.0, 5.0, 5.0, 5.0]    # Hz
        self.lfo_amplitudes = [255, 255, 255, 255]  # 0-255
        self.lfo_shapes = ['flat', 'sine_bi', 'triangle', 'ramp_down']  # shape names

        # --- WT1 config ---
        self.wt1_pos = 0           # 0-127 frame position
        self.wt1_env_select = 0    # 0-7
        self.wt1_pitch_lfo_en = False
        self.wt1_pitch_lfo_idx = 0  # 0-3
        self.wt1_pitch_trig = False
        self.wt1_pos_lfo_en = False
        self.wt1_pos_lfo_idx = 0    # 0-3
        self.wt1_pos_trig = False
        self.wt1_wave_file = None
        
        self.wt1_unison_on = False
        self.wt1_unison_voices = 3
        self.wt1_unison_detune = 30

        # --- WT2 config ---
        self.wt2_on = False        # derived from wt2_on switch (inverted: 0=on)
        self.wt2_env_select = 0
        self.wt2_pitch_lfo_en = False
        self.wt2_pitch_lfo_idx = 0
        self.wt2_pitch_trig = False
        self.wt2_wave_file = None
        
        self.wt2_unison_on = False
        self.wt2_unison_voices = 3
        self.wt2_unison_detune = 30

    # -------------------------------------------------------------------------
    # Computed hardware values
    # -------------------------------------------------------------------------
    def compute_adsr(self, env_idx):
        """Return (adsr1_word, adsr2_word) for envelope env_idx."""
        with self.lock:
            e = self.envelopes[env_idx]
            ar, ar_rs = _time_to_rate(e['a'] / 1000.0)
            dr, dr_rs = _time_to_rate(e['d'] / 1000.0)
            rr, rr_rs = _time_to_rate(e['r'] / 1000.0)
            sl = _sustain_float_to_hw(e['s'])
            return pack_adsr1(ar, ar_rs, dr, dr_rs), pack_adsr2(sl, rr, rr_rs)

    def get_lfo_ctrl_wt1(self):
        """Return the 8-bit LFO control byte for WT1 voices."""
        with self.lock:
            ctrl = (self.wt1_pitch_lfo_idx & 0x03) | ((self.wt1_pos_lfo_idx & 0x03) << 2)
            if self.wt1_pitch_lfo_en: ctrl |= 0x10
            if self.wt1_pos_lfo_en:   ctrl |= 0x20
            if self.wt1_pitch_trig:   ctrl |= 0x40
            if self.wt1_pos_trig:     ctrl |= 0x80
            return ctrl

    def get_lfo_ctrl_wt2(self):
        """Return the 8-bit LFO control byte for WT2 voices."""
        with self.lock:
            ctrl = (self.wt2_pitch_lfo_idx & 0x03)
            # WT2 has no position LFO (single frame)
            if self.wt2_pitch_lfo_en: ctrl |= 0x10
            if self.wt2_pitch_trig:   ctrl |= 0x40
            return ctrl

    def get_wt1_wt_id(self):
        """WT1 wt_id = frame position (0-127), bank bit 7 = 0."""
        with self.lock:
            return self.wt1_pos & 0x7F

    def get_wt2_wt_id(self):
        """WT2 wt_id = frame 0, bank bit 7 = 1 → 0x80."""
        return 0x80

    def get_voice_count(self):
        """Return (wt1_voices, wt2_voices)."""
        with self.lock:
            if self.wt2_on:
                return 128, 128
            return 256, 0

    # -------------------------------------------------------------------------
    # Update methods (called from /api/control)
    # -------------------------------------------------------------------------
    def update_envelope(self, env_idx, param, value):
        with self.lock:
            if param in ('a', 'd', 'r'):
                self.envelopes[env_idx][param] = float(value)
            elif param == 's':
                self.envelopes[env_idx]['s'] = float(value)

    def update_lfo_speed(self, lfo_idx, value):
        with self.lock:
            self.lfo_speeds[lfo_idx] = float(value)

    def update_lfo_shape(self, lfo_idx, shape_name):
        with self.lock:
            if shape_name in LFO_SHAPES:
                self.lfo_shapes[lfo_idx] = shape_name

    def update_lfo_amp(self, lfo_idx, value):
        with self.lock:
            self.lfo_amplitudes[lfo_idx] = int(max(0, min(255, float(value))))

    def get_scaled_lfo_shape(self, lfo_idx):
        """Return the LFO shape samples scaled by amplitude."""
        with self.lock:
            shape_name = self.lfo_shapes[lfo_idx]
            amp = self.lfo_amplitudes[lfo_idx]
        base = LFO_SHAPES.get(shape_name, LFO_SHAPES['flat'])
        if amp == 255:
            return base
        # Scale: treat as signed int8 for signed shapes, unsigned for others
        if shape_name in ('sine_bi',):
            signed = base.view(np.int8).astype(np.int16)
            scaled = (signed * amp // 255).astype(np.int8)
            return scaled.view(np.uint8)
        else:
            return (base.astype(np.uint16) * amp // 255).astype(np.uint8)
