"""
Engine for Takuan synth — AXI MMIO on PYNQ.
Copied from Scripts/pynq/engine.py into the web app for self-containment.
"""
import math

SAMPLING_RATE = 95274.390625  # actual fs from Zynq PLL (FCLK_CLK1 / 256)

def note_to_name(note_number):
    notes = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
    octave = note_number // 12 - 1
    return f"{notes[note_number % 12]}{octave}"

def midi_to_freq(note_number):
    return 440.0 * (2.0 ** ((note_number - 69) / 12.0))

def freq_to_stride(freq, sr=SAMPLING_RATE):
    return int((freq * 4294967296) / sr)

def calculate_unison_strides(center_freq_hz, detune_base_cents, voices) -> list[int]:
    if voices <= 1:
        return [freq_to_stride(center_freq_hz)]
    strides = []
    for i in range(voices):
        fraction = i / (voices - 1)
        factor = (fraction * 2.0) - 1.0
        cents_offset = factor * detune_base_cents
        multiplier = math.pow(2, cents_offset / 1200.0)
        strides.append(freq_to_stride(center_freq_hz * multiplier))
    return strides


class Engine:
    """
    AXI MMIO-based synth engine for the umeboshi IP on PYNQ.
    Register map (byte addresses, each voice/env index * 4):
        0x000 - 0x3FF: Stride        (256 voices)
        0x400 - 0x7FF: WT ID         (256 voices)
        0x800 - 0xBFF: Gain Env ID   (256 voices)
        0xC00 - 0xFFF: LFO ID        (256 voices)
        0x1000 - 0x13FF: Key On      (256 voices)
        0x1400 - 0x141F: LFO Stride  (4 LFOs)
        0x1420 - 0x143F: ADSR 1      (8 envelopes)
        0x1440 - 0x145F: ADSR 2      (8 envelopes)
        0x1800 - 0x1FFF: LFO Shape   (4 LFOs × 128 words)
    """
    def __init__(self, synth_mmio):
        self.synth = synth_mmio

    def write_voice(self, hw_index, stride, wt_id, envelope, key_on, lfo_ctrl=0):
        """Write all registers for a single hardware voice."""
        self.synth.write(0x000 + hw_index * 4, stride)
        self.synth.write(0x400 + hw_index * 4, wt_id)
        self.synth.write(0x800 + hw_index * 4, envelope)
        self.synth.write(0xC00 + hw_index * 4, lfo_ctrl)
        self.synth.write(0x1000 + hw_index * 4, int(key_on))

    def write_stride(self, hw_index, stride):
        self.synth.write(0x000 + hw_index * 4, stride)

    def write_wt_id(self, hw_index, wt_id):
        self.synth.write(0x400 + hw_index * 4, wt_id)

    def write_envelope_id(self, hw_index, envelope):
        self.synth.write(0x800 + hw_index * 4, envelope)

    def write_lfo_ctrl(self, hw_index, lfo_ctrl):
        self.synth.write(0xC00 + hw_index * 4, lfo_ctrl)

    def write_key_on(self, hw_index, key_on):
        self.synth.write(0x1000 + hw_index * 4, int(key_on))

    def write_adsr(self, env_idx, adsr1, adsr2):
        """Write packed ADSR registers for envelope env_idx (0-7)."""
        self.synth.write(0x1420 + env_idx * 4, adsr1)
        self.synth.write(0x1440 + env_idx * 4, adsr2)

    def write_lfo_stride(self, lfo_id, stride):
        self.synth.write(0x1400 + lfo_id * 4, stride)

    def write_lfo_shape(self, lfo_id, samples):
        """Write a 512-sample LFO shape (8-bit) into LUTRAM memory."""
        import numpy as np
        base = 0x1800 + lfo_id * 0x200
        samples_8 = samples.astype(np.uint8)
        words = samples_8.view(np.uint32)
        for i, word in enumerate(words):
            self.synth.write(base + i * 4, int(word))

    def key_off(self, hw_index):
        self.synth.write(0x1000 + hw_index * 4, 0)

    def reset(self):
        for i in range(256):
            self.write_voice(i, 0, 0, 0, 0)
