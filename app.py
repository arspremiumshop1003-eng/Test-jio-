from flask import Flask, jsonify
app = Flask(__name__)

@app.route("/")
def home():
    return "Premium Hub WebApp Final Backend Running"

@app.route("/api/status")
def status():
    return jsonify({"status":"ready","app":"Premium Hub"})
