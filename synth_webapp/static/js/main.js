document.addEventListener("DOMContentLoaded", () => {
    // 1. Setup Event Listeners
    const controls = document.querySelectorAll('webaudio-knob, webaudio-slider, webaudio-switch');
    const ledIndicator = document.getElementById('status-led');

    // Wavetable visualizer variables
    const wtCanvas = document.getElementById('wavetable-viewer');
    const wtCtx = wtCanvas ? wtCanvas.getContext('2d') : null;
    let wtBuffers = {
        'wavetable-viewer': null,
        'wavetable2-viewer': null
    };
    const wtFrameCount = 128; // Assuming 128 frames for wt_pos 0-127

    let debounceTimer;

    controls.forEach(control => {
        // Exclude switch for value display
        let valDisplay = null;
        const isSwitch = control.tagName.toLowerCase() === 'webaudio-switch';
        if (!isSwitch) {
            valDisplay = document.createElement('input');
            valDisplay.type = 'text';
            valDisplay.className = 'value-display';
            
            valDisplay.style.fontSize = '10px';
            valDisplay.style.color = '#FF7D34';
            valDisplay.style.marginTop = '2px';
            valDisplay.style.fontFamily = "'Noto Sans JP', sans-serif";
            valDisplay.style.fontWeight = 'bold';
            valDisplay.style.background = 'transparent';
            valDisplay.style.border = 'none';
            valDisplay.style.outline = 'none';
            valDisplay.style.textAlign = 'center';
            valDisplay.style.width = '36px';

            const formatVal = (v) => {
                if (control.id.match(/^env_\d+_[adr]$/)) return v + 'ms';
                if (control.id.match(/^env_\d+_s$/)) return parseFloat(v).toFixed(2);
                return v;
            };

            valDisplay.value = formatVal(control.value);

            // Editable Input Handling
            valDisplay.addEventListener('focus', () => {
                valDisplay.value = control.value;
                valDisplay.select();
            });

            valDisplay.addEventListener('blur', () => {
                let parsed = parseFloat(valDisplay.value);
                if (!isNaN(parsed)) {
                    let min = parseFloat(control.getAttribute('min')) || 0;
                    let max = parseFloat(control.getAttribute('max')) || 127;
                    if (parsed < min) parsed = min;
                    if (parsed > max) parsed = max;
                    
                    control.value = parsed;
                    // Force the component to update and trigger our network post payload via simulated input event
                    control.dispatchEvent(new Event('input', { bubbles: true }));
                }
                valDisplay.value = formatVal(control.value);
            });

            valDisplay.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    valDisplay.blur();
                }
            });

            // Place it directly after the control element
            control.insertAdjacentElement('afterend', valDisplay);
        } else {
            // Provide a tiny LED status indicator for toggle switches
            const ledDiv = document.createElement('div');
            ledDiv.className = 'toggle-led';
            if (parseInt(control.value) === 1) {
                ledDiv.classList.add('active');
            }
            
            // Insert before the switch element so it sits snug under its Top Label
            control.insertAdjacentElement('beforebegin', ledDiv);
            
            control.addEventListener('change', (e) => {
                if (parseInt(e.target.value) === 1) {
                    ledDiv.classList.add('active');
                } else {
                    ledDiv.classList.remove('active');
                }
            });
        }

        control.addEventListener(isSwitch ? 'change' : 'input', (e) => {
            const param = e.target.id;
            const value = e.target.value;

            if (valDisplay && document.activeElement !== valDisplay) {
                let displayVal = value;
                if (param.match(/^env_\d+_[adr]$/)) {
                    displayVal = value + 'ms';
                } else if (param.match(/^env_\d+_s$/)) {
                    displayVal = parseFloat(value).toFixed(2);
                }
                valDisplay.value = displayVal;
            }

            // Blink LED to show activity
            if (ledIndicator) {
                ledIndicator.classList.add('active');
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {
                    ledIndicator.classList.remove('active');
                }, 100);
            }

            if (param === 'wt_pos') {
                renderWavetableCanvas('wavetable-viewer', parseInt(value));
            }

            // Send to Backend
            fetch('/api/control', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    param: param,
                    value: value
                })
            }).catch(err => console.error('Error sending control:', err));
        });
    });

    // 2. Setup LFO Visualization
    const lfos = [
        { id: 'lfo_1_canvas', speedKnobId: 'lfo_1_speed', phase: 0 },
        { id: 'lfo_2_canvas', speedKnobId: 'lfo_2_speed', phase: 0 },
        { id: 'lfo_3_canvas', speedKnobId: 'lfo_3_speed', phase: 0 },
        { id: 'lfo_4_canvas', speedKnobId: 'lfo_4_speed', phase: 0 }
    ];

    function drawLFO() {
        requestAnimationFrame(drawLFO);

        lfos.forEach(lfo => {
            const canvas = document.getElementById(lfo.id);
            if (!canvas) return;
            const ctx = canvas.getContext('2d');
            const width = canvas.width;
            const height = canvas.height;

            const speedKnob = document.getElementById(lfo.speedKnobId);
            // Convert speed (0-127) to a usable animation frequency increment
            const speedVal = speedKnob ? parseFloat(speedKnob.value) : 64;
            const freq = (speedVal / 127) * 0.3 + 0.01;

            lfo.phase += freq;

            // Clear background
            ctx.fillStyle = '#0a0a0c';
            ctx.fillRect(0, 0, width, height);

            // Draw grid or center line (optional cosmetic detail)
            ctx.beginPath();
            ctx.strokeStyle = '#222';
            ctx.lineWidth = 1;
            ctx.moveTo(0, height / 2);
            ctx.lineTo(width, height / 2);
            ctx.stroke();

            // Draw wave
            ctx.beginPath();
            ctx.strokeStyle = '#4fa7ff';
            ctx.lineWidth = 2;
            ctx.shadowBlur = 10;
            ctx.shadowColor = 'rgba(79, 167, 255, 0.8)';

            for (let x = 0; x < width; x++) {
                // scale x to a phase offset to show the wave shape
                // showing about 1.5 cycles on the canvas purely for visual aesthetic
                const phaseOffset = (x / width) * Math.PI * 3;
                const waveScale = speedVal > 0 ? (height / 2.5) : 0;
                const y = Math.sin(lfo.phase + phaseOffset) * waveScale + (height / 2);
                if (x === 0) {
                    ctx.moveTo(x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            }
            ctx.stroke();
            ctx.shadowBlur = 0; // reset for next drawing operations
        });
    }

    drawLFO();

    // 3. Setup Wavetable Visualizer
    async function initWavetables() {
        try {
            const listResponse = await fetch('/api/wavetables');
            const files = await listResponse.json();
            
            if (!files || files.length === 0) return;
            
            const dropdown1 = document.getElementById('wt_wave_select');
            const dropdown2 = document.getElementById('wt2_wave_select');
            
            files.forEach(file => {
                const opt1 = document.createElement('option');
                opt1.value = file;
                opt1.textContent = file.replace('.wav', '');
                if (dropdown1) dropdown1.appendChild(opt1);
                
                const opt2 = document.createElement('option');
                opt2.value = file;
                opt2.textContent = file.replace('.wav', '');
                if (dropdown2) dropdown2.appendChild(opt2);
            });
            
            if (dropdown1) dropdown1.value = files[0];
            if (dropdown2) dropdown2.value = files[0];
            
            if (dropdown1) dropdown1.addEventListener('change', (e) => {
                loadWavetableFile(e.target.value, 'wavetable-viewer', 'wt_pos');
                fetch('/api/control', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({param: 'wt_wave_select', value: e.target.value}) });
            });
            
            if (dropdown2) dropdown2.addEventListener('change', (e) => {
                loadWavetableFile(e.target.value, 'wavetable2-viewer', null);
                fetch('/api/control', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({param: 'wt2_wave_select', value: e.target.value}) });
            });
            
            if (dropdown1) await loadWavetableFile(dropdown1.value, 'wavetable-viewer', 'wt_pos');
            if (dropdown2) await loadWavetableFile(dropdown2.value, 'wavetable2-viewer', null);
        } catch(e) {
            console.error("Failed to init wavetables:", e);
        }
    }

    async function loadWavetableFile(filename, canvasId, posControlId) {
        const wtCanvasNode = document.getElementById(canvasId);
        if (!wtCanvasNode || !wtCtx) return;
        try {
            const response = await fetch('/wav/' + encodeURIComponent(filename));
            const arrayBuffer = await response.arrayBuffer();

            // Minimal WAV parser to extract exact raw samples without AudioContext resampling
            const view = new DataView(arrayBuffer);
            let offset = 12; // skip RIFF header
            let formatCode = 1;
            let bitDepth = 16;
            let newBuffer = null;

            while (offset < view.byteLength) {
                const chunkIdStr = String.fromCharCode(view.getUint8(offset), view.getUint8(offset + 1), view.getUint8(offset + 2), view.getUint8(offset + 3));
                const chunkSize = view.getUint32(offset + 4, true);

                if (chunkIdStr === 'fmt ') {
                    formatCode = view.getUint16(offset + 8, true);
                    bitDepth = view.getUint16(offset + 22, true);
                } else if (chunkIdStr === 'data') {
                    const dataOffset = offset + 8;
                    const numSamples = chunkSize / (bitDepth / 8);
                    newBuffer = new Float32Array(numSamples);

                    if (formatCode === 3 && bitDepth === 32) {
                        for (let i = 0; i < numSamples; i++) {
                            newBuffer[i] = view.getFloat32(dataOffset + i * 4, true);
                        }
                    } else if (formatCode === 1 && bitDepth === 16) {
                        for (let i = 0; i < numSamples; i++) {
                            newBuffer[i] = view.getInt16(dataOffset + i * 2, true) / 32768.0;
                        }
                    } else if (formatCode === 1 && bitDepth === 24) {
                        for (let i = 0; i < numSamples; i++) {
                            const byteOffset = dataOffset + i * 3;
                            let int32 = view.getUint8(byteOffset) | (view.getUint8(byteOffset + 1) << 8) | (view.getInt8(byteOffset + 2) << 16);
                            newBuffer[i] = int32 / 8388608.0;
                        }
                    }
                    break;
                }
                offset += 8 + chunkSize;
            }

            wtBuffers[canvasId] = newBuffer;
            
            let pos = 0;
            if (posControlId) {
                const ctrl = document.getElementById(posControlId);
                if (ctrl) pos = parseInt(ctrl.value) || 0;
            }
            renderWavetableCanvas(canvasId, pos);
        } catch (e) {
            console.error("Failed to load generic wavetable " + filename + ":", e);
        }
    }

    function renderWavetableCanvas(canvasId, position) {
        const wtCanvasNode = document.getElementById(canvasId);
        const activeBuffer = wtBuffers[canvasId];
        if (!wtCanvasNode || !activeBuffer) return;
        const ctx = wtCanvasNode.getContext('2d');
        if (!ctx) return;

        const frameSize = 2048; // Exactly 2048 samples per wavetable position
        const totalFrames = Math.max(1, Math.floor(activeBuffer.length / frameSize));

        // Map knob position to 0-(totalFrames-1) frame index
        // Hardware usually downsamples 256-frame files to 128 by skipping every other
        let frameIdx = 0;
        if (totalFrames === 256) {
            frameIdx = position * 2;
        } else if (totalFrames === 128) {
            frameIdx = position;
        } else {
            // Generic proportional fallback
            const mappedPos = position / 127.0;
            frameIdx = Math.floor(mappedPos * (totalFrames - 1));
        }

        frameIdx = Math.max(0, Math.min(totalFrames - 1, frameIdx));

        const startIdx = frameIdx * frameSize;
        const endIdx = startIdx + frameSize;

        // Exact 2048 sample slice
        const slice = activeBuffer.subarray(startIdx, endIdx);

        const width = wtCanvasNode.width;
        const height = wtCanvasNode.height;

        // Clear background
        ctx.fillStyle = '#0a0a0c';
        ctx.fillRect(0, 0, width, height);

        // Draw center line
        ctx.beginPath();
        ctx.strokeStyle = '#222';
        ctx.lineWidth = 1;
        ctx.moveTo(0, height / 2);
        ctx.lineTo(width, height / 2);
        ctx.stroke();

        // Draw waveform
        ctx.beginPath();
        ctx.strokeStyle = '#FF7D34'; // Theme Accent
        ctx.lineWidth = 2;
        ctx.shadowBlur = 10;
        ctx.shadowColor = 'rgba(255, 125, 52, 0.8)';

        for (let x = 0; x < width; x++) {
            const index = Math.floor((x / width) * slice.length);
            const sample = slice[index] || 0;

            const y = (0.5 - (sample * 0.45)) * height;

            if (x === 0) {
                ctx.moveTo(x, y);
            } else {
                ctx.lineTo(x, y);
            }
        }

        ctx.stroke();
        ctx.stroke();
    }

    initWavetables();
});
