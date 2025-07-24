import json
import os
import threading
import time

from typing import Tuple, Dict, Any
from acp_plugin_gamesdk.acp_plugin import AcpPlugin, AcpPluginOptions
from acp_plugin_gamesdk.interface import AcpState, to_serializable_dict
from acp_plugin_gamesdk.env import PluginEnvSettings
from virtuals_acp.client import VirtualsACP
from virtuals_acp import ACPJob, ACPJobPhase
from game_sdk.game.custom_types import Function, FunctionResultStatus
from game_sdk.game.agent import Agent
from collections import deque
from rich import print, box
from rich.panel import Panel
from dotenv import load_dotenv

load_dotenv(override=True)

STATE_FILE = "job_state.json"

def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}

def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def ask_question(query: str) -> str:
    return input(query)

def update_job_state(job_type: str, acp_state: Dict) -> None:
    state = load_state()
    job_id = next(iter(state), None)

    if not job_id:
        return

    current_job = state[job_id]

    if job_type == "Alpha":
        job = current_job.get(job_type, {})
        target_acp_id = job.get("acpJobId")

        completed_job = next(
            (
                job for job in acp_state["jobs"]["completed"]
                if job["jobId"] == target_acp_id
            ),
            None,
        )
        if not completed_job:
            return

        acquired_job = next(
            (
                item for item in acp_state["inventory"]["acquired"]
                if item["jobId"] == completed_job["jobId"]
            ),
            None,
        )
        if not acquired_job:
            return

        state[job_id][job_type]["status"] = "COMPLETED"
        deliverable = json.loads(acquired_job.get("value"))
        state[job_id][job_type]["acquired"] = deliverable.get("value").get("data")[0].get("tokens").get("ethereum")

    elif job_type == "Analysis":
        analysis_jobs = current_job.get(job_type, {})
        all_completed = True
        any_pending = False

        for analysis_type, job_info in analysis_jobs["jobs"].items():
            if job_info["status"] == "PENDING":
                target_acp_id = job_info.get("acpJobId")
                completed_job = next(
                    (
                        job for job in acp_state["jobs"]["completed"]
                        if job["jobId"] == target_acp_id
                    ),
                    None,
                )
                if completed_job:
                    acquired_job = next(
                        (
                            item for item in acp_state["inventory"]["acquired"]
                            if item["jobId"] == completed_job["jobId"]
                        ),
                        None,
                    )
                    if acquired_job:
                        job_info["status"] = "COMPLETED"
                        job_info["result"] = json.loads(acquired_job.get("value"))
                    else:
                        all_completed = False
                else:
                    all_completed = False
                    any_pending = True

            save_state(state)

        if all_completed:
            state[job_id][job_type]["status"] = "COMPLETED"
        elif any_pending:
            state[job_id][job_type]["status"] = "PENDING"

    save_state(state)

def get_acp_state(acp_plugin: AcpPlugin, _=None) -> dict:
    acp_state = acp_plugin.get_acp_state()
    print(f"ACP Jobs: {acp_state}")
    return acp_state

def get_travel_proposal_function(acp_plugin: AcpPlugin) -> Function:
    def get_travel_proposal_executable() -> Tuple[FunctionResultStatus, str, dict]:
        state = load_state()
        job_id = next(iter(state), None)

        if not job_id:
            return FunctionResultStatus.FAILED, "Should get travel requirement first", {}

        analysis_jobs = state[job_id].get("Analysis", {})
        if isinstance(analysis_jobs, dict) and analysis_jobs.get("status") == "COMPLETED":
            return FunctionResultStatus.FAILED, "Analysis jobs are already completed", {}

        # Initialize analysis jobs structure if not exists
        if not isinstance(analysis_jobs, dict) or "status" not in analysis_jobs:
            state[job_id]["Proposal"] = {
                "status": "INITIALIZING",
                "jobs": {
                    "flight_finder": {"status": "NOT_STARTED"},
                    "accomodation_n_acitivity_finder": {"status": "NOT_STARTED"},
                }
            }
            analysis_jobs = state[job_id]["Proposal"]
            save_state(state)

        flight_finder_initiator = Agent(
            api_key=os.getenv("GAME_API_KEY"),
            name="flight_agent",
            agent_goal="Initiate a job with an agent that provides flight finding service.",
            agent_description="""
            1. Search for an flight finder agent using searchAgents.
            2. NO EVALUATOR IS NEEDED (requireEvaluator=False).
            """,
            workers=[
                acp_plugin.get_worker({
                    "functions": [acp_plugin.search_agents_functions, acp_plugin.initiate_job]
                })
            ],
            get_agent_state_fn=lambda *args, **kwargs: get_acp_state(acp_plugin)
        )

        accomodation_n_acitivity_finder_initator = Agent(
            api_key=os.getenv("GAME_API_KEY"),
            name="activity_agent",
            agent_goal="Initiate a job with an agent that provides accomodation and acitivity finding service.",
            agent_description="""
            1. Search for a accomodation and acitivity finder agent using searchAgents.
            2. NO EVALUATOR IS NEEDED (requireEvaluator=False).
            """,
            workers=[
                acp_plugin.get_worker({
                    "functions": [acp_plugin.search_agents_functions, acp_plugin.initiate_job]
                })
            ],
            get_agent_state_fn=lambda *args, **kwargs: get_acp_state(acp_plugin)
        )

        travel_proposal_agents = [
            (flight_finder_initiator, "flight_finding", f"Find an agent that provides flight finding service for token with the following serviceRequirement: {state[job_id]['Alpha']['acquired']}"),
            (accomodation_n_acitivity_finder_initator, "sn", f"Find an token audit agent to do onchain due diligence for token on Base chain with the following serviceRequirement: {state[job_id]['Alpha']['acquired']}"),
        ]

        current_acp_state = acp_plugin.get_acp_state()
        current_jobs = current_acp_state["jobs"]["active"].get("asABuyer", [])
        print(f"ACP Jobs: {current_acp_state}")

        jobs_initiated = False
        for agent, job_type, query in travel_proposal_agents:
            if analysis_jobs["jobs"][job_type]["status"] in ["NOT_STARTED", "FAILED"]:
                agent.compile()
                agent.get_worker("acp_worker").run(query)
                time.sleep(50)

                # Check if the job was actually created
                updated_acp_state = acp_plugin.get_acp_state()
                updated_jobs = updated_acp_state["jobs"]["active"].get("asABuyer", [])
                new_jobs = [job for job in updated_jobs if job["jobId"] not in [j["jobId"] for j in current_jobs]]

                if new_jobs:
                    # Update the current jobs list for next iteration
                    current_jobs = updated_jobs
                    # Update state for the newly initiated job
                    analysis_jobs["jobs"][job_type] = {
                        "status": "PENDING",
                        "acpJobId": new_jobs[0]["jobId"]
                    }
                    jobs_initiated = True
                else:
                    analysis_jobs["jobs"][job_type]["status"] = "FAILED"

        if not jobs_initiated:
            return FunctionResultStatus.FAILED, "No new analysis jobs were successfully initiated", {}

        # Check if all jobs have been initiated
        all_initiated = all(
            analysis_jobs["jobs"][job_type]["status"] in ["PENDING", "COMPLETED"]
            for job_type in ["flight_finder", "accomodation_n_acitivity_finder"]
        )

        if all_initiated:
            state[job_id]["Analysis"]["status"] = "PENDING"

        save_state(state)

        print(Panel(f"Analysis jobs state: {analysis_jobs}", title="Analysis Jobs", box=box.ROUNDED, title_align="left"))

        return FunctionResultStatus.DONE, "ANALYSIS JOBS INITIATED", {}

    return Function(
        fn_name="get_travel_requirement",
        fn_description="Initiate jobs to get travel requirement.",
        args=[],
        executable=get_travel_proposal_executable
    )

def seller(use_thread_lock: bool = True):
    env = PluginEnvSettings()

    if env.WHITELISTED_WALLET_PRIVATE_KEY is None:
        return

    if env.SELLER_ENTITY_ID is None:
        return

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
                agent_wallet_address=env.SELLER_AGENT_WALLET_ADDRESS,
                on_new_task=on_new_task,
                entity_id=env.SELLER_ENTITY_ID
            ),
        )
    )

    def get_agent_state(_: None, _e: None) -> dict:
        state = acp_plugin.get_acp_state()
        state_dict = to_serializable_dict(state)
        return state_dict

    acp_worker = acp_plugin.get_worker(
        {
            "functions": [
                acp_plugin.respond_job,
                acp_plugin.deliver_job,
            ]
        }
    )

    agent = Agent(
        api_key=env.GAME_API_KEY,
        name="Memx",
        agent_goal="To provide meme generation as a service. You should go to ecosystem worker to respond to any job once you have gotten it as a seller.",
        agent_description=f"""
        You are Memx, a meme generator. Meme generation is your life. You always give buyer the best meme.

        {acp_plugin.agent_description}
        """,
        workers=[acp_worker],
        get_agent_state_fn=get_agent_state
    )

    agent.compile()

    print("🟢"*40)
    init_state = AcpState.model_validate(agent.agent_state)
    print(Panel(f"{init_state}", title="Agent State", box=box.ROUNDED, title_align="left"))
    print("🔴"*40)

     # Start background thread
    threading.Thread(target=job_worker, daemon=True).start()
    print("\nListening...\n")
    threading.Event().wait()


if __name__ == "__main__":
    seller(use_thread_lock=True)
