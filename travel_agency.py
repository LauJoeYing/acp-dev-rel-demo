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
from game_sdk.game.custom_types import Function, FunctionResultStatus, Argument
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

    # ---------------------- FUNCTION TO INITIATE PROPOSAL ----------------------
    def get_travel_proposal_function() -> Function:
        def get_travel_proposal_executable(job_id: str) -> Tuple[FunctionResultStatus, str, dict]:
            state = load_state()
            if not job_id:
                return FunctionResultStatus.FAILED, "No travel job found. Initialize travel requirements first.", {}

            # Ensure proposal structure exists
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

            # --- Check if main job is already processing ---
            if state[job_id]["proposal"]["status"] == "PENDING":
                # Look for any failed sub-jobs to retry
                failed_jobs = [k for k, v in travel_jobs.items() if v["status"] == "FAILED"]
                if not failed_jobs:
                    return FunctionResultStatus.FAILED, f"Job {job_id} is already being processed.", {}

                print(f"Retrying failed jobs for {job_id}: {failed_jobs}")
            else:
                print(f"Starting new proposal jobs for {job_id}")
                state[job_id]["proposal"]["status"] = "PENDING"
                save_state(state)
                failed_jobs = ["flight_finding", "activity_finding"]

            def initiate_job(agent: Agent, job_key: str, query: str):
                successful = False
                while not successful:
                    try:
                        print(f"Initiating sub-job: {job_key}")
                        agent.compile()
                        agent.get_worker("acp_worker").run(query)
                        time.sleep(5)
                        updated_jobs = acp_plugin.get_acp_state()["jobs"]["active"].get("asABuyer", [])
                        for job in updated_jobs:
                            if (
                                    ((job_key == "activity_finding" and job["providerAddress"] == env.PLANNER_AGENT_WALLET_ADDRESS)
                                    or (job_key == "flight_finding" and job["providerAddress"] == env.FLIGHT_FINDER_AGENT_WALLET_ADDRESS))
                                and job["memo"]
                            ):
                                travel_jobs[job_key] = {"status": "PENDING", "acpJobId": job["jobId"]}
                                successful = True
                                break
                            else:
                                travel_jobs[job_key]["status"] = "FAILED"
                    except Exception as e:
                        travel_jobs[job_key]["status"] = "FAILED"
                        print(f"❌ Failed initiating {job_key} job: {e}")
                    finally:
                        save_state(state)

            # Agents
            flight_agent_initiator = Agent(
                api_key=env.GAME_API_KEY,
                name="flight_agent",
                agent_goal="Find flight options.",
                agent_description="Search for flight finder agent using searchAgents. No evaluator needed.",
                workers=[acp_plugin.get_worker({"functions": [acp_plugin.search_agents_functions, acp_plugin.initiate_job]})],
                get_agent_state_fn=get_agent_state,
            )
            activity_agent_initiator = Agent(
                api_key=env.GAME_API_KEY,
                name="activity_agent",
                agent_goal="Find travel activities.",
                agent_description="Search for activity finder agent using searchAgents. No evaluator needed.",
                workers=[acp_plugin.get_worker({"functions": [acp_plugin.search_agents_functions, acp_plugin.initiate_job]})],
                get_agent_state_fn=get_agent_state,
            )

            # Run only the required jobs concurrently
            threads = []
            if "flight_finding" in failed_jobs:
                threads.append(threading.Thread(
                    target=initiate_job,
                    args=(flight_agent_initiator, "flight_finding", f"Find flights based on: {state[job_id]['desc']}"),
                ))
            if "activity_finding" in failed_jobs:
                threads.append(threading.Thread(
                    target=initiate_job,
                    args=(activity_agent_initiator, "activity_finding", f"Find activities based on: {state[job_id]['desc']}"),
                ))

            for t in threads:
                t.start()
            for t in threads:
                t.join()

            save_state(state)
            print(Panel(f"Proposal jobs state: {state[job_id]['proposal']}", title="Proposal Jobs", box=box.ROUNDED, title_align="left"))
            return FunctionResultStatus.DONE, "Proposal jobs (re)initiated as needed.", {}

        return Function(
            fn_name="get_travel_requirement",
            fn_description="Initiate jobs for travel proposal (flights + activities).",
            args=[
                Argument(
                    name="job_id",
                    description="The job ID to initiate the travel proposal for.",
                    type="string"
                )
            ],
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

    def on_evaluate(job: ACPJob):
        job.evaluate(True)

        state = load_state()
        deliver_triggered = False

        for main_job_id, jobs in state.items():
            if "proposal" not in jobs or "jobs" not in jobs["proposal"]:
                continue

            for job_key, job_info in jobs["proposal"]["jobs"].items():
                print(f"Checking job {job_key} for main job {main_job_id}: {job_info}")
                if job_info.get("acpJobId") == job.id:
                    job_info["status"] = "EVALUATED"
                    job_info["deliverable"] = job.deliverable
                    save_state(state)
                    break


            # --- Check if all sub-jobs done & have deliverables ---
            all_evaluated = all(
                j["status"] == "EVALUATED" and "deliverable" in j
                for j in jobs["proposal"]["jobs"].values()
            )

            if all_evaluated and not jobs["proposal"].get("delivered"):
                print(f"All sub-jobs done for main job {main_job_id}, delivering result...")

                # Compile deliverable (example: combine JSON)
                deliverable = {
                    "type": "object",
                    "value": {
                        "flights": jobs["proposal"]["jobs"]["flight_finding"]["deliverable"],
                        "activities": jobs["proposal"]["jobs"]["activity_finding"]["deliverable"]
                    }
                }

                try:
                    # Fetch main job & deliver
                    main_job = acp_plugin.acp_client.get_job_by_onchain_id(int(main_job_id))
                    main_job.deliver(json.dumps(deliverable))
                    jobs["proposal"]["status"] = "DELIVERED"
                    save_state(state)
                    print(f"✅ Delivered compiled travel proposal for {main_job_id}")
                    deliver_triggered = True
                except Exception as e:
                    print(f"❌ Failed to deliver main job {main_job_id}: {e}")

        if not deliver_triggered:
            agent.get_worker("acp_worker").run(
                "Deliverable not triggered yet. Waiting for other sub-jobs to complete. Make payments if needed."
            )
            print("Deliverable not triggered (waiting for other sub-jobs).")

    def on_new_task(job: ACPJob):
        # Populate state with this new job if not already present
        state = load_state()
        job_id = str(job.id)
        if job_id not in state and job.provider_address == acp_plugin.acp_client.agent_address:
            state[job_id] = {
                "desc": job.service_requirement
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
                on_evaluate=on_evaluate,
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
                acp_plugin.pay_job,
                get_travel_proposal_function()
            ]
        }
    )
    agent = Agent(
        api_key=env.GAME_API_KEY,
        name="Virtuals Travel Agency",
        agent_goal="Autonomously query Flight Finder and Activity Planner agents, then compile results into a cohesive travel plan.",
        agent_description=f"This agent orchestrates travel planning (flights + activities). WAIT FOR PAYMENT AFTER PROCESSING "
                          f"if no payment yet has been conducted yet DO NOT provide travel proposal.\n{acp_plugin.agent_description}",
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
