import argparse
import concurrent.futures
import logging
import math
import multiprocessing
import os
import random
import sys
import tempfile
import threading
import time
import uuid

import httpx
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
from cryptography.fernet import Fernet
from matplotlib import font_manager as fm

RANDOM_SEED = 42
logging.getLogger('httpx').setLevel(logging.WARNING)

class StickyApiChooser:
    """
    Chooses between two APIs ('api1', 'api2') with a given overall ratio,
    but introduces stickiness, making it more likely to repeat the last choice.
    """
    def __init__(self, api1_ratio, stickiness):
        """
        Initializes the chooser.

        Args:
            api1_ratio (float): The desired long-term probability of choosing 'api1' (between 0 and 1).
            stickiness (float): The probability of attempting to stick with the
                                       last chosen API (between 0 and 1).
                                       0 means no stickiness (original behavior).
                                       Near 1 means high stickiness (long runs).
        """
        if not 0.0 <= api1_ratio <= 1.0:
            raise ValueError("api1_ratio must be between 0 and 1")
        if not 0.0 <= stickiness <= 1.0:
            raise ValueError("stickiness must be between 0 and 1")

        self.api1_ratio = api1_ratio
        self.stickiness = stickiness
        self.last_chosen_api = None

    def choose_api(self):
        should_stick = (self.last_chosen_api is not None and
                        random.random() < self.stickiness)

        if should_stick:
            chosen_api = self.last_chosen_api
        else:
            if random.random() < self.api1_ratio:
                chosen_api = "api1"
            else:
                chosen_api = "api2"

        self.last_chosen_api = chosen_api
        return chosen_api

def setup_log(level: str):
    lv = {
            "critical": logging.CRITICAL,
            "error": logging.ERROR,
            "info": logging.INFO,
            "debug": logging.DEBUG
    }.get(level, logging.INFO)

    logging.basicConfig(level = lv, format=f"%(asctime)s [Client PID:{os.getpid()}] %(levelname)s: %(message)s")

def parse_client_config(path):

    clients=[]
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if ln.lower().startswith("client"):
                parts = ln.split(None, 5)[1:]
                clients.append(parts)

    return clients

def validate_client_line(parts: list[str], idx: int, parser: argparse.ArgumentParser) -> tuple[int,int,float,float,str]:

    if len(parts) < 5:
        parser.error(f"Line {idx}: expected 5 parts, got {len(parts)}")

    try:
        rps = int(parts[0])
    except ValueError:
        parser.error(f"Line {idx}: rps must be integer, got '{parts[0]}'")
    if rps <= 0:
        parser.error(f"Line {idx}: rps must be > 0, got {rps}")

    try:
        duration = int(parts[1])
    except ValueError:
        parser.error(f"Line {idx}: duration must be integer, got '{parts[1]}'")
    if duration <= 0:
        parser.error(f"Line {idx}: duration must be > 0, got {duration}")

    try:
        api1_ratio = float(parts[2])
    except ValueError:
        parser.error(f"Line {idx}: api1_ratio must be float, got '{parts[2]}'")
    if not 0.0 <= api1_ratio <= 1.0:
        parser.error(f"Line {idx}: api1_ratio must be between 0 and 1, got {api1_ratio}")

    try:
        stickiness = float(parts[3])
    except ValueError:
        parser.error(f"Line {idx}: stickiness must be float, got '{parts[3]}'")
    if not 0.0 <= stickiness <= 1.0:
        parser.error(f"Line {idx}: stickiness must be between 0 and 1, got {stickiness}")

    dist = parts[4].lower()
    if dist not in {'uniform', 'exponential', 'pareto'}:
        parser.error(f"Line {idx}: distribution must be one of {sorted({'uniform', 'exponential', 'pareto'})}, got '{parts[4]}'")

    return rps, duration, api1_ratio, stickiness, dist

def get_sleep_interval(distribution: str, mean_interval: float) -> float:
    if distribution == "exponential":
        return -mean_interval * math.log(1.0 - random.random())
    elif distribution == "pareto":
        beta = 1.5
        alpha = mean_interval * (beta - 1) / beta
        u = random.random()
        return alpha / (u ** (1.0 / beta))
    else:
        return mean_interval

def send_request(
    client, chosen_api, proxy_url, worker_id, secret_key,
    client_latencies, client_latencies_lock, total_requests_counter, success_counter,
    i, client_key
):
    fernet = Fernet(secret_key)
    full_url = f"{proxy_url}{chosen_api}"

    with total_requests_counter.get_lock():
        total_requests_counter.value += 1

    t1 = time.time()
    try:
        logging.debug(f"Sending from [Client {worker_id}] Req {i+1}\nClient Key={client_key}")
        resp = client.get(full_url, params={"client_key": client_key})
        logging.debug(f"Received at [Client {worker_id}] Req {i+1}\nClient Key={client_key}")
        t2 = time.time()
        latency = t2 - t1

        if resp.is_success:
            data = resp.json()
            encrypted_key = data.get("server_response", {}).get("encrypted_key", "")
            try:
                decrypted = fernet.decrypt(encrypted_key.encode()).decode()
                logging.debug(f"Received at [Client {worker_id}] Req {i+1}\nKey={encrypted_key}: {encrypted_key}\nDecrypted Key={decrypted}")
                if decrypted == client_key:
                    logging.debug(f"Success!")
                    with success_counter.get_lock():
                        success_counter.value += 1
                    with client_latencies_lock:
                        client_latencies.append(latency)
                else:
                    logging.error(f"[Worker {worker_id}] Key mismatch. {decrypted=} != {client_key=}")
            except Exception as e:
                logging.error(f"[Worker {worker_id}] Decryption error: {e}")
        else:
            logging.error(f"[Client {worker_id}] HTTP response error: {resp.status_code}")
    except Exception as e:
        logging.error(f"[Worker {worker_id}] HTTP connection error: {e}")

def run_workload(
        rps, duration_secs, api_chooser, api_lock, dist, proxy_url,
        client_latencies, client_latencies_lock,
        total_requests_counter, success_counter_worker,
        secret_key, worker_id, max_workers
    ):

    random.seed(42)
    mean_interval = 1.0 / rps if rps > 0 else float('inf')
    end_time = time.time() + duration_secs
    i = 0

    timeout = httpx.Timeout(2.0, connect=1.0, read=2.0)
    with httpx.Client(http2=True, timeout=timeout) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            while time.time() < end_time:
                with api_lock:
                    chosen_api = api_chooser.choose_api()
                client_key = f"cli{worker_id}-{uuid.uuid4()}"
                future = executor.submit(
                    send_request,
                    client, chosen_api, proxy_url,
                    worker_id, secret_key, client_latencies, client_latencies_lock,
                    total_requests_counter, success_counter_worker,
                    i, client_key
                )
                futures.append(future)
                sleep_time = get_sleep_interval(dist, mean_interval)
                time.sleep(sleep_time)
                i += 1

def client_worker(rps, duration_secs, api1_ratio, stickiness, dist, proxy_url,
                  global_latencies, global_latencies_lock,
                  total_requests_counter, success_counter, secret_key,
                  worker_id, max_workers, log):

    random.seed(RANDOM_SEED + worker_id)
    setup_log(log)
    logging.info(f"[Worker {worker_id}] Starting: rps={rps}, secs={duration_secs}, api1_ratio={api1_ratio}, stickiness={stickiness}, arrival_distribution={dist}, max_workers={max_workers}")

    client_latencies = []
    client_latencies_lock = threading.Lock()
    api_chooser = StickyApiChooser(api1_ratio=api1_ratio, stickiness=stickiness)
    api_lock = threading.Lock()

    success_counter_worker = multiprocessing.Value('i', 0)
    run_workload(
        rps, duration_secs, api_chooser, api_lock, dist, proxy_url,
        client_latencies, client_latencies_lock,
        total_requests_counter, success_counter_worker,
        secret_key, worker_id, max_workers
    )
    success_counter[worker_id] = success_counter_worker.value
    logging.info(f"[Worker {worker_id}] Done.")

    with global_latencies_lock:
        global_latencies.append(client_latencies)

def plot_cdf(latencies, total_reqs, total_successes, total_time):
    n = len(latencies)
    if n == 0:
        logging.error("No successful requests recorded!")
        logging.error(f"Dropped all {total_reqs} requests. Throughput = 0.0 req/s")
        return

    lat_sorted = sorted(latencies)
    cdf_y = [(i + 1) / n for i in range(n)]
    throughput = total_successes / total_time

    min_l = lat_sorted[0]
    mean_l = sum(latencies) / n
    p90_l = lat_sorted[int(0.90 * n) - 1] if n >= 10 else lat_sorted[-1]
    p95_l = lat_sorted[int(0.95 * n) - 1] if n >= 20 else lat_sorted[-1]
    p99_l = lat_sorted[int(0.99 * n) - 1] if n >= 100 else lat_sorted[-1]
    max_l = lat_sorted[-1]

    table_data = [
        ["Total Requests Sent", str(total_reqs)],
        ["Total Requests Succeeded", str(total_successes)],
        ["Total Requests Dropped", str(total_reqs - total_successes)],
        ["Throughput", f"{throughput:.2f} req/s"],
        ["Minimum Latency", f"{min_l:.4f}s"],
        ["Mean Latency", f"{mean_l:.4f}s"],
        ["90th Percentile Latency", f"{p90_l:.4f}s"],
        ["95th Percentile Latency", f"{p95_l:.4f}s"],
        ["99th Percentile Latency", f"{p99_l:.4f}s"],
        ["Maximum Latency", f"{max_l:.4f}s"],
    ]

    fig = plt.figure(figsize=(12, 10))
    fig.suptitle("Latency/ Throughput Performance Summary", fontsize=18, fontweight='bold')

    gs = gridspec.GridSpec(2, 1, height_ratios=[1.2, 2])

    ax_table = fig.add_subplot(gs[0])
    ax_table.axis("off")
    table = ax_table.table(
        cellText=table_data,
        colLabels=["Metric", "Value"],
        loc="center",
        cellLoc="left",
        colWidths=[0.5, 0.5]
    )
    table.scale(1, 2)
    for (row, _), cell in table.get_celld().items():
        text = cell.get_text()
        if row == 0:
            text.set_fontproperties(fm.FontProperties(weight='bold', size=20))
        else:
            text.set_fontproperties(fm.FontProperties(size=16))

    # Plot (bottom row)
    ax_plot = fig.add_subplot(gs[1])
    ax_plot.plot(lat_sorted, cdf_y, label="Latency CDF", color="#000000", linewidth=2)
    # Add markers at every 10th percentile
    percentiles = np.arange(10, 100, 10)
    for p in percentiles:
        val = np.percentile(latencies, p)
        ax_plot.plot(val, p / 100, 'r*', markersize = 10)
        ax_plot.text(val + 0.001, p / 100 + 0.001, f'({val:.3f}, {p}%)', fontsize=12, ha='left', va='top')
    ax_plot.tick_params(axis='both', which='major', labelsize=14, size=12)
    ax_plot.set_xlabel("Latency (seconds)", fontsize=14, fontweight='bold')
    ax_plot.set_ylabel("CDF", fontsize=16, fontweight='bold')
    ax_plot.set_title("Latency Distribution", fontsize=14, fontweight='bold')

    plt.tight_layout(rect=[0, 0, 1, 0.94])
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.show()

def main() -> None:

    parser = argparse.ArgumentParser()
    parser.add_argument("--test_case_file", type = str, default = "./test-cases/test_case_0.txt",
                        help = "One line per client: rps duration api1_ratio distribution")
    parser.add_argument("--proxy_url", type = str, default = "http://127.0.0.1:8000/proxy/",
                        help = "Base proxy endpoint, must end with /proxy/")
    parser.add_argument("--secret_key_file", type = str, default = "./metadata/secret.key",
                        help = "Same secret as server so we can decrypt & check correctness.")
    parser.add_argument("--max_workers", type = int,
                        help="Maximum number of worker threads per client process. Defaults to 2 * CPU cores.")
    parser.add_argument("--log_level", type = str, choices = ["critical", "error", "info", "debug"], default = "info",
                        help = "Set your log level [critical, error, info, debug]")
    parser.add_argument("--plot", action = "store_true")

    args = parser.parse_args()
    setup_log(args.log_level)

    if not os.path.isfile(args.secret_key_file):
        raise FileNotFoundError(f"Secret key file {args.secret_key_file} not found.")
    with open(args.secret_key_file, "rb") as f:
        secret_key = f.read()

    client_lines = parse_client_config(args.test_case_file)

    manager = multiprocessing.Manager()
    global_latencies = manager.list()
    global_latencies_lock = manager.Lock()
    success_counter = manager.list([0] * len(client_lines))
    total_requests_counter = multiprocessing.Value('i', 0)

    processes = []

    default_max_workers = 16

    max_workers = args.max_workers if args.max_workers is not None else default_max_workers
    logging.info(f"Using max_workers per client process: {max_workers}")

    start_time = time.time()

    try:

        for worker_id, line in enumerate(client_lines):
            rps, duration_secs, api1_ratio, stickiness, dist = validate_client_line(line, worker_id + 1, parser)
            p = multiprocessing.Process(
                target=client_worker,
                args=(int(rps), int(duration_secs), float(api1_ratio), float(stickiness), dist,
                    args.proxy_url, global_latencies, global_latencies_lock,
                    total_requests_counter, success_counter,
                    secret_key, worker_id, max_workers,
                    args.log_level)
            )
            p.start()
            processes.append(p)

        for p in processes:
            p.join()

    except KeyboardInterrupt:
        logging.info("Interrupted by user—shutting down.")
        for p in processes:
            p.terminate()

    finally:

        end_time = time.time()
        total_time = end_time - start_time

        total_successes = sum(success_counter)
        latencies = np.concatenate(global_latencies).tolist() or [999999999]
        total_requests = total_requests_counter.value

        if total_successes == 0 or not latencies:
            throughput = 0.0
            mean_latency = float('inf')
            p95_latency = float('inf')
            final_score = 0.0
            logging.warning("No successful requests; setting metrics to defaults.")
        else:
            throughput = total_successes / total_time if total_time > 0 else 0.0
            mean_latency = sum(latencies) / len(latencies)
            p95_latency = np.percentile(latencies, 95)
            final_score = throughput / (mean_latency * 0.9 + p95_latency * 0.1)

        logging.info(f"Total test time: {total_time:.2f}s. Sent={total_requests}, Success={total_successes}, Throughtput={throughput:.2f}")
        logging.info(f"Mean Latency={mean_latency:.2f}, P99 Latency={np.percentile(latencies, 99):.2f}, P95 Latency={p95_latency:.2f}, P90 Latency={np.percentile(latencies, 90):.2f}")
        logging.info(f"Final Score={final_score:.2f}")

        if args.plot:
            plot_cdf(latencies, total_requests, total_successes, total_time)
        else:
            logging.info("Plotting disabled in headless mode.")

if __name__ == "__main__":
    main()
