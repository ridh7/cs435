import argparse
import logging
import os

import httpx
from flask import Flask, jsonify, request
from waitress import serve

app = Flask(__name__)

# Initializing the backend servers for simplicity
# Your code must dynamically read the capability_file and parse it
backend_servers = [
    "http://127.0.0.1:8001",
    "http://127.0.0.1:8002",
    "http://127.0.0.1:8003",
]

# Setting up logging
def setup_log(level: str):
    lv = {
            "critical": logging.CRITICAL,
            "error": logging.ERROR,
            "info": logging.INFO,
            "debug": logging.DEBUG
    }.get(level, logging.INFO)

    logging.basicConfig(level = lv, format=f"%(asctime)s [Proxy PID:{os.getpid()}] %(levelname)s: %(message)s")

@app.route('/proxy/<path:subpath>', methods=['GET'])
def proxy_handler(subpath):
    """
    If the client hits /proxy/api1, we forward to the chosen_server/api1.
    If the client hits /proxy/api2, we forward to the chosen_server/api2.
    """
    server = backend_servers[0]

    url = f"{server}/{subpath}"  # e.g. subpath = 'api1' => server + '/api1'

    # Forward query params as-is
    query_params = request.args.to_dict()
    timeout = httpx.Timeout(2.0, connect=1.0, read=2.0)

    try:
        with httpx.Client(http2=True, timeout=timeout) as client:
            response = client.get(url, params=query_params)
            return jsonify(response.json()), response.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--capability_file', type=str, required=True, help='File specifying backend server addresses and supported APIs.')
    parser.add_argument("--log_level", type=str, choices=["critical", "error", "info", "debug"], default="info", help="Set log level.")
    args = parser.parse_args()
    setup_log(args.log_level)

    logging.info(f"Proxy listening on port {args.port}")
    serve(app, host='127.0.0.1', port=args.port, threads=16)
