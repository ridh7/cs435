import argparse
import asyncio
import logging
import os
import random
import signal
import threading
import time
from collections import defaultdict
from multiprocessing import Process, Value

from cryptography.fernet import Fernet
from flask import Flask, jsonify, request
from hypercorn.asyncio import serve
from hypercorn.config import Config

QUEUE_LIMIT = 64
RANDOM_SEED = 42

req_counts   = defaultdict(lambda: defaultdict(int))
stats_lock = threading.Lock()
STATS_PERIOD = 10.0

logging.getLogger('httpx').setLevel(logging.WARNING)

def setup_log(level: str):
    lv = {
            "critical": logging.CRITICAL,
            "error": logging.ERROR,
            "info": logging.INFO,
            "debug": logging.DEBUG
    }.get(level, logging.INFO)

    logging.basicConfig(level=lv, format=f"%(asctime)s [Server PID:{os.getpid()}] %(levelname)s: %(message)s")

def parse_server_config(path):
    servers = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"): continue

            if ln.lower().startswith("server"):
                parts = ln.split()

                if len(parts) < 11:
                    logging.warning(f"Skipping malformed server line (not enough fields): {ln}")
                    continue
                try:
                    config = dict(
                        port = int(parts[1]),
                        factor = float(parts[2]),
                        api1_delays = (float(parts[3]), float(parts[4])),
                        api2_delays = (float(parts[5]), float(parts[6])),
                        limit1 = int(parts[7]),
                        limit2 = int(parts[8]),
                        api1_active = bool(int(parts[9])),
                        api2_active = bool(int(parts[10])),

                        incident_type = None,
                        incident_start = 0.0,
                        incident_duration = 0.0,
                        incident_extra_delay = 1.0
                    )

                    if len(parts) >= 12:
                         itype = parts[11]
                         if itype in ["slowdown", "shutdown"]:
                             try:
                                  config["incident_type"] = itype
                                  config["incident_start"] = float(parts[12])
                                  config["incident_duration"] = float(parts[13])
                                  if itype == "slowdown" and len(parts) >= 15:
                                      config["incident_extra_delay"] = float(parts[14])
                                  logging.info(f"Server {config['port']} configured for incident: {itype} starting at {config['incident_start']}s for {config['incident_duration']}s")
                             except (ValueError, IndexError):
                                  logging.warning(f"Ignoring invalid incident parameters for server {config['port']}: {' '.join(parts[11:])}")
                         else:
                              logging.warning(f"Ignoring unknown incident type '{itype}' for server {config['port']}")
                except ValueError as e:
                     logging.warning(f"Skipping malformed server line (value error: {e}): {ln}")
                     continue

                servers.append(config)
            elif ln.lower().startswith("client"):

                 pass
            elif ln.lower().startswith("autograder"):
                 pass
            else:
                 logging.warning(f"Skipping unknown line type: {ln}")

    return servers

async def metrics_task(port: int):
    """
    Background coroutine that prints RPS for every api on `port`
    every STATS_PERIOD seconds.
    """
    prev = defaultdict(int)
    while True:
        await asyncio.sleep(STATS_PERIOD)
        with stats_lock:
            current = req_counts[port].copy()

        lines = []
        for api in ("api1", "api2"):
            rps = (current[api] - prev[api]) / STATS_PERIOD
            lines.append(f"{api}:{rps:.2f} rps")
            prev[api] = current[api]

        logging.debug(f"[METRICS] port {port} -> " + ", ".join(lines))

def run_single_server(config, seed, secret, log_level, process_start_time):
    """Runs a single server instance with potential incident simulation."""

    random.seed(seed)
    app = Flask(__name__)
    setup_log(log_level)
    log = logging.getLogger(__name__)

    try:
        fernet = Fernet(secret)
    except Exception as e:
        log.error(f"Invalid secret key: {e}")
        return

    joint_limit = config["limit1"] + config["limit2"]
    if joint_limit <= 0:
        log.warning(f"Server {config['port']} has a non-positive joint limit ({joint_limit}). Setting to 1.")
        joint_limit = 1
    joint_sem = threading.BoundedSemaphore(joint_limit)
    log.info(f"Server {config['port']} using JOINT concurrency limit: {joint_limit}")

    def check_incident():
        """Checks if an incident is currently active. Returns incident type or None."""
        if not config.get("incident_type"):
            return None

        current_process_time = time.time() - process_start_time
        start = config["incident_start"]
        end = start + config["incident_duration"]

        if start <= current_process_time < end:
            return config["incident_type"]
        return None

    def make_handler(name: str, delays: tuple[float, float], sem: threading.BoundedSemaphore, is_active: bool):
        endpoint_name = f"{name}_{config['port']}"

        if not is_active:
            log.info(f"API /{name} is configured as INACTIVE for server {config['port']}.")
            return lambda: (f"{name} API is inactive on this server", 503)

        def _handler():
            client_key = request.args.get('client_key', '')
            log.debug(f"Received request for /{name} on port {config['port']} (client_key: {client_key[:8]}...)")

            active_incident = check_incident()
            if active_incident:
                log.debug(f"Incident '{active_incident}' active for {name} request on port {config['port']}.")
                if active_incident == "shutdown":
                    return ("Service Unavailable due to simulated shutdown", 503)
                elif active_incident == "slowdown":
                    extra_delay = config.get("incident_extra_delay", 1.0)
                    log.debug(f"Applying slowdown delay: {extra_delay:.4f}s")
                    time.sleep(extra_delay)

            log.debug(f"Attempting to acquire semaphore for {name} on port {config['port']}")
            acquired = sem.acquire(blocking=True, timeout=2.0)
            if not acquired:
                log.warning(f"Semaphore timeout for {name} on port {config['port']}. Server busy.")
                return ("Server busy, semaphore timeout", 503)

            log.debug(f"Semaphore acquired for {name} on port {config['port']}")
            try:

                simulated_delay = random.uniform(delays[0] * config["factor"], delays[1] * config["factor"])
                log.debug(f"Processing {name} ({client_key[:8]}...) on port {config['port']}. Simulated delay: {simulated_delay:.4f}s")
                time.sleep(simulated_delay)

                server_key = fernet.encrypt(client_key.encode())

                log.debug(f"Finished processing {name} on port {config['port']}. Releasing semaphore.")
                return jsonify({
                    "server_response": {
                        'api': name,
                        'port': config["port"],
                        'encrypted_key': server_key.decode('utf-8'),
                        'message': f'Response from {name} on port {config["port"]}'
                    }
                })
            except Exception as e:
                 log.error(f"Error during handler execution for {name} on port {config['port']}: {e}", exc_info=True)
                 return ("Internal server error", 500)
            finally:

                try:
                    sem.release()
                    with stats_lock:
                        req_counts[config["port"]][name] += 1
                    log.debug(f"Semaphore released for {name} on port {config['port']}")
                except ValueError:
                     log.error(f"Error: Released semaphore for {name} on port {config['port']} too many times!")

        app.add_url_rule(f"/{name}", endpoint=endpoint_name, view_func=_handler, methods=["GET"])
        return _handler

    api1_handler = make_handler("api1", config["api1_delays"], joint_sem, config["api1_active"])
    api2_handler = make_handler("api2", config["api2_delays"], joint_sem, config["api2_active"])

    @app.route('/')
    def root_handler():
         return jsonify({"status": "ok", "port": config["port"], "apis": {"api1": config["api1_active"], "api2": config["api2_active"]}})

    server_config = Config()
    server_config.reuse_port = True
    server_config.reuse_address = True
    server_config.bind = [f'127.0.0.1:{config["port"]}']
    server_config.alpn_protocols = ["h2", "http/1.1"]
    server_config.backlog = QUEUE_LIMIT

    log.info(f"Starting server on port {config['port']}. Concurrency: total={joint_limit}. DelayFactor={config['factor']}. APIs Active: api1={config['api1_active']}, api2={config['api2_active']}.")
    if config.get("incident_type"):
        log.info(f"-> Incident configured: {config['incident_type']} @ {config['incident_start']}s for {config['incident_duration']}s")

    async def main():

        try:
            asyncio.create_task(metrics_task(config['port']))
            await serve(app, server_config)
        except asyncio.CancelledError:
             log.info(f"Server on port {config['port']} serve task cancelled.")
        except Exception as e:
             log.error(f"Server serve task failed on port {config['port']}: {e}", exc_info=True)

    try:
         loop = asyncio.new_event_loop()
         asyncio.set_event_loop(loop)
         loop.run_until_complete(main())
         loop.close()
    except KeyboardInterrupt:
         log.info(f"Server process on port {config['port']} interrupted.")
    except Exception as e:
         log.error(f"Server process failed on port {config['port']}: {e}", exc_info=True)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Launch multiple servers with incident simulation.")
    parser.add_argument("--test_case_file", type=str, default="./test-cases/test_case_0.txt",
                        help="File defining server configs: Server: port factor min1 max1 min2 max2 l1 l2 active1 active2 [inc_type start_t duration_t [extra_delay]]")
    parser.add_argument('--secret_key_file', type=str, default='./metadata/secret.key',
                        help='Path to the server secret key file.')
    parser.add_argument("--log_level", type=str, choices=["critical", "error", "info", "debug"], default="info",
                        help="Set log level.")
    args = parser.parse_args()

    setup_log(args.log_level)
    log = logging.getLogger(__name__)

    if not os.path.isfile(args.secret_key_file):
        log.info(f"Secret key file {args.secret_key_file} not found.")
        log.info("Generating a new secret key...")
        try:
            os.makedirs(os.path.dirname(args.secret_key_file), exist_ok=True)
            key = Fernet.generate_key()
            with open(args.secret_key_file, 'wb') as f:
                f.write(key)
            os.chmod(args.secret_key_file, 0o400)
            log.info(f"Generated new secret key: {args.secret_key_file}")
            secret_key = key
        except Exception as e:
            log.critical(f"Failed to generate secret key: {e}")
            exit(1)
    else:
        try:
            with open(args.secret_key_file, 'rb') as f:
                secret_key = f.read()

            Fernet(secret_key)
            log.info(f"Loaded secret key from {args.secret_key_file}")
        except Exception as e:
             log.critical(f"Failed to load or validate secret key from {args.secret_key_file}: {e}")
             exit(1)

    if not os.path.isfile(args.test_case_file):
        log.critical(f"Server configuration file {args.test_case_file} not found.")
        exit(1)
    server_configs = parse_server_config(args.test_case_file)

    if not server_configs:
        log.critical(f"No valid server configurations found in {args.test_case_file}.")
        exit(1)

    log.info(f"Launching {len(server_configs)} server processes...")
    processes = []
    process_start_time = time.time()
    try:
        for i, config in enumerate(server_configs):

            process_seed = RANDOM_SEED + i
            p = Process(target=run_single_server, args=(config, process_seed, secret_key, args.log_level, process_start_time))
            processes.append(p)
            p.start()
            log.info(f"Started process for server on port {config['port']}")

        for p in processes:
            p.join()
    except KeyboardInterrupt:
        log.info("Main process received KeyboardInterrupt. Terminating server processes...")
        for p in processes:
            if p.is_alive():
                p.terminate()
        for p in processes:
             p.join()
        log.info("All server processes terminated.")
    except Exception as e:
        log.error(f"An error occurred in the main process: {e}", exc_info=True)
        for p in processes:
            if p.is_alive():
                p.terminate()
        for p in processes:
             p.join()
    finally:
        log.info("Server launcher process exiting.")
