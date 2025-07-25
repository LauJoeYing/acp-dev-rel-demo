import json
import threading
from collections import deque
from typing import Tuple

from acp_plugin_gamesdk.acp_plugin import AcpPlugin, AcpPluginOptions
from acp_plugin_gamesdk.interface import AcpState, IInventory, to_serializable_dict
from dotenv import load_dotenv
from game_sdk.game.agent import Agent
from game_sdk.game.custom_types import Argument, Function, FunctionResultStatus
from rich import print, box
from rich.panel import Panel
from serpapi import GoogleSearch
from virtuals_acp import ACPJob, ACPJobPhase
from virtuals_acp.client import VirtualsACP

from env import TravelDemoEnvSettings

load_dotenv(override=True)

env = TravelDemoEnvSettings()


def activity_planner(use_thread_lock: bool = True):
    # Thread-safe job queue setup
    job_queue = deque()
    job_queue_lock = threading.Lock()
    job_event = threading.Event()

    # Thread-safe append wrapper
    def safe_append_job(job):
        if use_thread_lock:
            print("[append] Attempting to acquire job_queue_lock")
            with job_queue_lock:
                print("[append] Lock acquired. Appending job to queue:", job.id)
                job_queue.append(job)
                print(f"[append] Queue size is now {len(job_queue)}")
        else:
            job_queue.append(job)
            print(f"[append] Appended job (no lock). Queue size is now {len(job_queue)}")

    # Thread-safe pop wrapper
    def safe_pop_job():
        if use_thread_lock:
            print("[pop] Attempting to acquire job_queue_lock")
            with job_queue_lock:
                print("[pop] Lock acquired.")
                if job_queue:
                    job = job_queue.popleft()
                    print(f"[pop] Job popped: {job.id}")
                    return job
                else:
                    print("[pop] Queue is empty.")
        else:
            if job_queue:
                job = job_queue.popleft()
                print(f"[pop] Job popped (no lock): {job.id}")
                return job
            else:
                print("[pop] Queue is empty (no lock).")
        return None

    # Background thread worker: process jobs one by one
    def job_worker():
        while True:
            job_event.wait()

            # Process all available jobs
            while True:
                job = safe_pop_job()
                if not job:
                    break
                try:
                    process_job(job)
                except Exception as e:
                    print(f"❌ Error processing job: {e}")
                    # Continue processing other jobs even if one fails

            # Clear event only after ensuring no jobs remain
            if use_thread_lock:
                with job_queue_lock:
                    if not job_queue:
                        job_event.clear()
            else:
                if not job_queue:
                    job_event.clear()

    # Event-triggered job task receiver
    def on_new_task(job: ACPJob):
        print(f"[on_new_task] New job received: {job.id}")
        safe_append_job(job)
        job_event.set()
        print("[on_new_task] job_event set.")

    def process_job(job: ACPJob):
        out = ""
        out += f"Reacting to job:\n{job}\n\n"
        prompt = ""

        if job.phase == ACPJobPhase.REQUEST:
            for memo in job.memos:
                if memo.next_phase == ACPJobPhase.NEGOTIATION:
                    prompt = f"""
                    Respond to the following transaction:
                    {job}
        
                    decide whether you should accept the job or not.
                    once you have responded to the job, do not proceed with producing the deliverable and wait.
                    """
        elif job.phase == ACPJobPhase.TRANSACTION:
            for memo in job.memos:
                if memo.next_phase == ACPJobPhase.EVALUATION:
                    prompt = f"""
                    Respond to the following transaction:
                    {job}
        
                    you should produce the deliverable and deliver it to the buyer.
        
                    If no deliverable is provided, you should produce the deliverable and deliver it to the buyer.
                    """
        else:
            out += "No need to react to the phase change\n\n"

        if prompt:
            agent.get_worker("acp_worker").run(prompt)
            out += f"Running task:\n{prompt}\n\n"
            out += "✅ Seller has responded to job.\n"

        print(Panel(out, title="🔁 Reaction", box=box.ROUNDED, title_align="left", border_style="red"))

    acp_plugin = AcpPlugin(
        options=AcpPluginOptions(
            api_key=env.GAME_API_KEY,
            acp_client=VirtualsACP(
                wallet_private_key=env.WHITELISTED_WALLET_PRIVATE_KEY,
                agent_wallet_address=env.PLANNER_AGENT_WALLET_ADDRESS,
                on_new_task=on_new_task,
                entity_id=env.PLANNER_ENTITY_ID
            )
        )
    )

    print(f"Agent: {acp_plugin.acp_client.agent_address} connected.")

    def get_agent_state(_: None, _e: None) -> dict:
        state = acp_plugin.get_acp_state()
        state_dict = to_serializable_dict(state)
        return state_dict

    def find_activities_executable(job_id: int, search_query: str) -> Tuple[FunctionResultStatus, str, dict]:
        if not job_id or job_id == 'None':
            return FunctionResultStatus.FAILED, f"job_id is invalid. Should only respond to active as a seller job.", {}

        job = acp_plugin.acp_client.get_job_by_onchain_id(job_id)

        if not job:
            return FunctionResultStatus.FAILED, f"Job {job_id} is invalid. Should only respond to active as a seller job.", {}

        activities_data = GoogleSearch({
            "q": search_query,
            "engine": "google_events",
            "api_key": env.SERPAPI_API_KEY,
        }).get_dict().get("events_results")

        if not activities_data:
            return FunctionResultStatus.FAILED, "No activities found.", {}

        activities = {
            "type": "object",
            "value": activities_data[:5],
        }

        job.deliver(json.dumps(activities))

        return FunctionResultStatus.DONE, f"Activities found: {activities_data}", {}

    find_activities = Function(
        fn_name="find_activities",
        fn_description="A function to find activities",
        args=[
            Argument(
                name="job_id",
                type="integer",
                description="Job that your are responding to."
            ),
            Argument(
                name="search_query",
                type="str",
                description="Search query for relevant activities. Should always be formatted as 'Events in {location}, {country}, {month of the date}'",
            )
        ],
        executable=find_activities_executable
    )

    acp_worker = acp_plugin.get_worker(
        {
            "functions": [
                acp_plugin.respond_job,
                find_activities
            ]
        }
    )

    agent = Agent(
        api_key=env.GAME_API_KEY,
        name="Activities Planner",
        agent_goal=f"""
        To find plan activities for the destination, considering user interests, and logistical constraints.
        """,
        agent_description=f"""
        This agent integrates accommodation search and itinerary planning. It finds lodging options that balance price, 
        location, and amenities, then schedules activities by factoring in weather forecasts and user interest categories 
        (e.g., culture, adventure, relaxation). It dynamically adjusts plans to optimize for comfort and safety.

        {acp_plugin.agent_description}
        """,
        workers=[acp_worker],
        get_agent_state_fn=get_agent_state
    )

    agent.compile()

    print("🟢" * 40)
    init_state = AcpState.model_validate(agent.agent_state)
    print(Panel(f"{init_state}", title="Agent State", box=box.ROUNDED, title_align="left"))
    print("🔴" * 40)

    # Start background thread
    threading.Thread(target=job_worker, daemon=True).start()
    print("\nListening...\n")
    threading.Event().wait()


if __name__ == "__main__":
    activity_planner(use_thread_lock=False)
