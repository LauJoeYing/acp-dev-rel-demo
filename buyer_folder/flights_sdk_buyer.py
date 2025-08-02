import threading
from datetime import datetime, timedelta

from virtuals_acp.client import VirtualsACP
from virtuals_acp.job import ACPJob
from virtuals_acp.models import ACPJobPhase
from dotenv import load_dotenv
from collections import deque

from env import TravelDemoEnvSettings

load_dotenv(override=True)

def traveler(use_thread_lock: bool = True):
    env = TravelDemoEnvSettings()

    if env.WHITELISTED_WALLET_PRIVATE_KEY is None:
        raise ValueError("WHITELISTED_WALLET_PRIVATE_KEY is not set")
    if env.TRAVELER_AGENT_WALLET_ADDRESS is None:
        raise ValueError("TRAVELER_AGENT_WALLET_ADDRESS is not set")
    if env.TRAVELER_ENTITY_ID is None:
        raise ValueError("TRAVELER_ENTITY_ID is not set")

    job_queue = deque()
    initiate_job_lock = threading.Lock()
    job_queue_lock = threading.Lock()
    job_event = threading.Event()

    def safe_append_job(job):
        if use_thread_lock:
            print(f"[safe_append_job] Acquiring lock to append job {job.id}")
            with job_queue_lock:
                print(f"[safe_append_job] Lock acquired, appending job {job.id} to queue")
                job_queue.append(job)
        else:
            job_queue.append(job)

    def safe_pop_job():
        if use_thread_lock:
            print(f"[safe_pop_job] Acquiring lock to pop job")
            with job_queue_lock:
                if job_queue:
                    job = job_queue.popleft()
                    print(f"[safe_pop_job] Lock acquired, popped job {job.id}")
                    return job
                else:
                    print("[safe_pop_job] Queue is empty after acquiring lock")
        else:
            if job_queue:
                job = job_queue.popleft()
                print(f"[safe_pop_job] Popped job {job.id} without lock")
                return job
            else:
                print("[safe_pop_job] Queue is empty (no lock)")
        return None

    def job_worker():
        while True:
            job_event.wait()
            while True:
                job = safe_pop_job()
                if not job:
                    break
                try:
                    process_job(job)
                except Exception as e:
                    print(f"\u274c Error processing job: {e}")
            if use_thread_lock:
                with job_queue_lock:
                    if not job_queue:
                        job_event.clear()
            else:
                if not job_queue:
                    job_event.clear()

    def on_new_task(job: ACPJob):
        print(f"[on_new_task] Received job {job.id} (phase: {job.phase})")
        safe_append_job(job)
        job_event.set()

    def on_evaluate(job: ACPJob):
        print("Evaluation function called", job.memos)
        for memo in job.memos:
            if memo.next_phase == ACPJobPhase.COMPLETED:
                job.evaluate(True)
                break

    def process_job(job: ACPJob):
        if job.phase == ACPJobPhase.NEGOTIATION:
            for memo in job.memos:
                if memo.next_phase == ACPJobPhase.TRANSACTION:
                    print("Paying job", job.id)
                    job.pay(job.price)
                    break
        elif job.phase == ACPJobPhase.COMPLETED:
            print("Job completed", job)
        elif job.phase == ACPJobPhase.REJECTED:
            print("Job rejected", job)

    threading.Thread(target=job_worker, daemon=True).start()

    acp = VirtualsACP(
        wallet_private_key=env.WHITELISTED_WALLET_PRIVATE_KEY,
        agent_wallet_address=env.TRAVELER_AGENT_WALLET_ADDRESS,
        on_new_task=on_new_task,
        on_evaluate=on_evaluate,
        entity_id=env.TRAVELER_ENTITY_ID
    )

    relevant_agents = acp.browse_agents(
        keyword="flights finder",
        cluster="demo-travel",
        graduated=False
    )
    print(f"Relevant agents: {relevant_agents}")

    # Pick one of the agents based on your criteria (in this example we just pick the first one)
    chosen_agent = relevant_agents[0]

    # Pick one of the service offerings based on your criteria (in this example we just pick the first one)
    chosen_job_offering = chosen_agent.offerings[0]

    with initiate_job_lock:
        chosen_job_offering.initiate_job(
            service_requirement={
                "origin (IATA code)": "Madrid",
                "destination (IATA code)": "New York",
                "departureDate (YYYY-MM-DD)": "2025/07/26",
                "returnDate (YYYY-MM-DD)": "2025/07/29"
            },
            expired_at=datetime.now() + timedelta(minutes=8)
        )

    print("Listening for next steps...")
    threading.Event().wait()

if __name__ == "__main__":
    traveler(use_thread_lock=False)
