import json
import os
import threading
import time
from typing import Dict, Any, Tuple
from collections import deque
from dotenv import load_dotenv
from rich import print, box
from rich.panel import Panel

from acp_plugin_gamesdk.acp_plugin import AcpPlugin, AcpPluginOptions
from acp_plugin_gamesdk.interface import AcpState, to_serializable_dict
from virtuals_acp.client import VirtualsACP
from virtuals_acp import ACPJob, ACPJobPhase
from game_sdk.game.custom_types import Function, FunctionResultStatus
from game_sdk.game.agent import Agent

from env import TravelDemoEnvSettings

load_dotenv(override=True)

STATE_FILE = "job_state.json"


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def ask_question(query: str) -> str:
    return input(query)


def virtuals_travel_agency(use_thread_lock: bool = True):
    env = TravelDemoEnvSettings()

    # ---------------------- ACP & STATE HELPERS ----------------------
    def get_acp_state(acp_plugin: AcpPlugin, _=None) -> dict:
        acp_state = acp_plugin.get_acp_state()
        print(f"ACP Jobs: {acp_state}")
        return acp_state

    # ---------------------- FUNCTION TO INITIATE PROPOSAL ----------------------
    def get_travel_proposal_function() -> Function:
        def get_travel_proposal_executable() -> Tuple[FunctionResultStatus, str, dict]:
            state = load_state()
            job_id = next(iter(state), None)
            if not job_id:
                return FunctionResultStatus.FAILED, "No travel job found. Initialize travel requirements first.", {}

            # Initialize proposal structure
            if "proposal" not in state[job_id]:
                state[job_id]["proposal"] = {
                    "status": "INITIALIZING",
                    "jobs": {
                        "flight_finding": {"status": "NOT_STARTED"},
                        "activity_finding": {"status": "NOT_STARTED"},
                    },
                }
                save_state(state)

            travel_jobs = state[job_id]["proposal"]["jobs"]

            def initiate_job(agent: Agent, job_key: str, query: str):
                try:
                    agent.compile()
                    agent.get_worker("acp_worker").run(query)
                    time.sleep(10)
                    updated_jobs = acp_plugin.get_acp_state()["jobs"]["active"].get("asABuyer", [])
                    if updated_jobs:
                        for job in updated_jobs:
                            if job_key == "flight_finding" and job.provider_address == env.PLANNER_AGENT_WALLET_ADDRESS:
                                travel_jobs[job_key] = {"status": "PENDING", "acpJobId": job.id}
                            elif job_key == "activity_finding" and job.provider_address == env.FLIGHT_FINDER_AGENT_WALLET_ADDRESS:
                                travel_jobs[job_key] = {"status": "PENDING", "acpJobId": job.id}

                    else:
                        travel_jobs[job_key]["status"] = "FAILED"
                except Exception as e:
                    travel_jobs[job_key]["status"] = "FAILED"
                    print(f"❌ Failed initiating {job_key} job: {e}")
                finally:
                    save_state(state)

            # Flight & Activity Agents
            flight_agent_initiator = Agent(
                api_key=env.GAME_API_KEY,
                name="flight_agent",
                agent_goal="Find flight options.",
                agent_description="Search for flight finder agent using searchAgents. No evaluator needed.",
                workers=[acp_plugin.get_worker({"functions": [acp_plugin.search_agents_functions, acp_plugin.initiate_job]})],
                get_agent_state_fn=lambda *a, **k: get_acp_state(acp_plugin),
            )
            activity_agent_initiator = Agent(
                api_key=env.GAME_API_KEY,
                name="activity_agent",
                agent_goal="Find travel activities.",
                agent_description="Search for activity finder agent using searchAgents. No evaluator needed.",
                workers=[acp_plugin.get_worker({"functions": [acp_plugin.search_agents_functions, acp_plugin.initiate_job]})],
                get_agent_state_fn=lambda *a, **k: get_acp_state(acp_plugin),
            )

            # Run both jobs concurrently
            threads = [
                threading.Thread(target=initiate_job, args=(flight_agent_initiator, "flight_finding", f"Find flights based on: {state[job_id]['desc']}")),
                threading.Thread(target=initiate_job, args=(activity_agent_initiator, "activity_finding", f"Find activities based on: {state[job_id]['desc']}")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            state[job_id]["proposal"]["status"] = "PENDING"
            save_state(state)
            print(Panel(f"Proposal jobs state: {state[job_id]['proposal']}", title="Proposal Jobs", box=box.ROUNDED, title_align="left"))
            return FunctionResultStatus.DONE, "Proposal jobs initiated concurrently.", {}

        return Function(
            fn_name="get_travel_requirement",
            fn_description="Initiate jobs for travel proposal (flights + activities).",
            args=[],
            executable=get_travel_proposal_executable,
        )

    # ---------------------- JOB QUEUE SETUP ----------------------
    job_queue = deque()
    job_queue_lock = threading.Lock()
    job_event = threading.Event()

    def safe_append_job(job):
        if use_thread_lock:
            with job_queue_lock:
                job_queue.append(job)
        else:
            job_queue.append(job)

    def safe_pop_job():
        if use_thread_lock:
            with job_queue_lock:
                return job_queue.popleft() if job_queue else None
        return job_queue.popleft() if job_queue else None

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
                    print(f"❌ Error processing job: {e}")
            if use_thread_lock:
                with job_queue_lock:
                    if not job_queue:
                        job_event.clear()
            elif not job_queue:
                job_event.clear()

    def on_new_task(job: ACPJob):
        # Populate state with this new job if not already present
        state = load_state()
        job_id = str(job.id)
        if job_id not in state and job.provider_address == acp_plugin.acp_client.agent_address:
            state[job_id] = {
                "phase": job.phase.value,
                "desc": job.service_requirement,
                "client": job.client_address,
                "provider": job.provider_address,
                "status": "NEW"
            }
            save_state(state)
            print(f"[STATE] New job {job_id} added to state")

        # Append to job queue for processing
        safe_append_job(job)
        job_event.set()

    def process_job(job: ACPJob):
        out = f"Reacting to job:\n{job}\n\n"
        prompt = ""
        if job.phase == ACPJobPhase.REQUEST:
            for memo in job.memos:
                if memo.next_phase == ACPJobPhase.NEGOTIATION:
                    prompt = f"Respond to transaction:\n{job}\nDecide whether to accept the job."
        elif job.phase == ACPJobPhase.NEGOTIATION:
            for memo in job.memos:
                if memo.next_phase == ACPJobPhase.TRANSACTION:
                    print("Paying job", job.id)
                    job.pay(job.price)
                    break
        elif job.phase == ACPJobPhase.TRANSACTION:
            for memo in job.memos:
                if memo.next_phase == ACPJobPhase.EVALUATION:
                    prompt = f"Produce and deliver the requested deliverable:\n{job}"
        else:
            out += "No action required for this phase\n\n"

        if prompt:
            agent.get_worker("acp_worker").run(prompt)
            out += f"Running task:\n{prompt}\n\n✅ Seller responded.\n"
        print(Panel(out, title="🔁 Reaction", box=box.ROUNDED, title_align="left", border_style="red"))

    # ---------------------- ACP PLUGIN & AGENT ----------------------
    acp_plugin = AcpPlugin(
        options=AcpPluginOptions(
            api_key=env.GAME_API_KEY,
            acp_client=VirtualsACP(
                wallet_private_key=env.WHITELISTED_WALLET_PRIVATE_KEY,
                agent_wallet_address=env.AGENCY_AGENT_WALLET_ADDRESS,
                on_new_task=on_new_task,
                entity_id=env.AGENCY_ENTITY_ID
            ),
            cluster="demo-travel",
            graduated=False
        )
    )

    def get_agent_state(_: None, _e: None) -> dict:
        return to_serializable_dict(acp_plugin.get_acp_state())

    acp_worker = acp_plugin.get_worker(
        {
            "functions": [
                acp_plugin.respond_job,
                acp_plugin.deliver_job,
                acp_plugin.pay_job,
                get_travel_proposal_function()
            ]
        }
    )
    agent = Agent(
        api_key=env.GAME_API_KEY,
        name="Virtuals Travel Agency",
        agent_goal="Autonomously query Flight Finder and Activity Planner agents, then compile results into a cohesive travel plan.",
        agent_description=f"This agent orchestrates travel planning (flights + activities). Only wait for payment after responding "
                          f"to job, if no payment yet has been conducted, wait.\n{acp_plugin.agent_description}",
        workers=[acp_worker],
        get_agent_state_fn=get_agent_state,
    )
    agent.compile()

    print("🟢" * 40)
    init_state = AcpState.model_validate(agent.agent_state)
    print(Panel(f"{init_state}", title="Agent State", box=box.ROUNDED, title_align="left"))
    print("🔴" * 40)

    threading.Thread(target=job_worker, daemon=True).start()
    print("\nListening...\n")
    threading.Event().wait()


if __name__ == "__main__":
    virtuals_travel_agency(use_thread_lock=True)
