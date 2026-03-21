from flask import Flask, render_template, request, jsonify, send_from_directory
import os

app = Flask(__name__, static_folder='static', template_folder='templates')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/control', methods=['POST'])
def control():
    data = request.json
    if not data:
        return jsonify({"status": "error", "message": "No JSON data provided"}), 400
    
    # Normally this would write to AXI registers on the Pynq board.
    # For now, we just print the received control messages.
    param = data.get('param')
    value = data.get('value')
    
    print(f"Synth Control Updated | Param: {param} | Value: {value}")
    
    return jsonify({"status": "success", "param": param, "value": value})

@app.route('/webaudio-controls/<path:filename>')
def webaudio_controls(filename):
    base_dir = os.path.dirname(os.path.abspath(__name__))
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
