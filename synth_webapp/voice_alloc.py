"""
Voice allocator for the Takuan web synth.
Adapted from Scripts/pynq/patch.py — simplified for web-controlled params.
"""


class VoiceAllocator:
    """
    Manages hardware voice allocation for one oscillator bank.
    Round-robin allocation with configurable voice count and start index.
    """
    def __init__(self, start_index: int, voice_count: int):
        self.voice_start = start_index
        self.voice_count = voice_count
        self.cursor = 0
        self.active_notes = {}  # midi_note -> [hw_indices]

    def resize(self, start_index: int, voice_count: int):
        """Resize the allocator (e.g. switching single/dual mode)."""
        self.voice_start = start_index
        self.voice_count = voice_count
        self.cursor = 0
        self.active_notes.clear()

    def allocate(self, midi_note, voices_needed=1) -> list:
        """
        Allocate hardware voice(s) for a MIDI note.
        Returns list of hw indices, or None if already playing.
        """
        if midi_note in self.active_notes:
            return self.active_notes[midi_note]

        assigned = []
        for i in range(voices_needed):
            slot = (self.cursor + i) % self.voice_count
            hw_index = self.voice_start + slot
            self._evict(hw_index)
            assigned.append(hw_index)

        self.cursor = (self.cursor + voices_needed) % self.voice_count
        self.active_notes[midi_note] = assigned
        return assigned

    def release(self, midi_note) -> list:
        """Release voices for a MIDI note. Returns list of hw indices or None."""
        if midi_note in self.active_notes:
            return self.active_notes.pop(midi_note)
        return None

    def release_all(self) -> list:
        """Release all voices. Returns list of all hw indices that were active."""
        all_indices = []
        for indices in self.active_notes.values():
            all_indices.extend(indices)
        self.active_notes.clear()
        self.cursor = 0
        return all_indices

    def get_all_active_indices(self) -> list:
        """Return all currently active hw indices."""
        indices = []
        for idx_list in self.active_notes.values():
            indices.extend(idx_list)
        return indices

    def _evict(self, target_hw_index):
        """Evict any note currently using this hw voice slot."""
        for note, indices in list(self.active_notes.items()):
            if target_hw_index in indices:
                indices.remove(target_hw_index)
                if not indices:
                    del self.active_notes[note]
                break
