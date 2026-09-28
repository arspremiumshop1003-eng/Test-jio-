from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def home():
    return "Premium Hub Web API Running"

@app.route("/api/products")
def products():
    return jsonify({"products": ["Gemini Jio 18 Months"]})

@app.route("/api/status")
def status():
    return jsonify({"status": "ready"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
