import threading

from game_sdk.game.agent import Agent

from acp_plugin_gamesdk.interface import AcpState, to_serializable_dict
from acp_plugin_gamesdk.acp_plugin import AcpPlugin, AcpPluginOptions
from acp_plugin_gamesdk.env import PluginEnvSettings
from virtuals_acp.client import VirtualsACP
from virtuals_acp import ACPJob, ACPJobPhase
from rich import print, box
from rich.panel import Panel
from dotenv import load_dotenv

load_dotenv(override=True)

env = PluginEnvSettings()

def on_evaluate(job: ACPJob):
    for memo in job.memos:
        if memo.next_phase == ACPJobPhase.COMPLETED:
            print(f"Evaluating deliverable for job {job.id}")
            # Auto-accept all deliverables for this example
            job.evaluate(True)
            break


def buyer(use_thread_lock: bool = True):
    if env.WHITELISTED_WALLET_PRIVATE_KEY is None:
        return
    
    if env.BUYER_ENTITY_ID is None:
        return
    
    # Thread-safe job queue setup
    job_queue = []
    job_queue_lock = threading.Lock()
    job_event = threading.Event()

    # Thread-safe append with optional lock
    def safe_append_job(job):
        if use_thread_lock:
            print(f"[safe_append_job] Acquiring lock to append job {job.id}")
            with job_queue_lock:
                print(f"[safe_append_job] Lock acquired, appending job {job.id} to queue")
                job_queue.append(job)
        else:
            job_queue.append(job)

    # Thread-safe pop with optional lock
    def safe_pop_job():
        if use_thread_lock:
            print(f"[safe_pop_job] Acquiring lock to pop job")
            with job_queue_lock:
                if job_queue:
                    job = job_queue.pop(0)
                    print(f"[safe_pop_job] Lock acquired, popped job {job.id}")
                    return job
                else:
                    print("[safe_pop_job] Queue is empty after acquiring lock")
        else:
            if job_queue:
                job = job_queue.pop(0)
                print(f"[safe_pop_job] Popped job {job.id} without lock")
                return job
            else:
                print("[safe_pop_job] Queue is empty (no lock)")
        return None

    # Background thread worker: process jobs one by one
    def job_worker():
        while True:
            job_event.wait()  # Wait for job

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
        print(f"[on_new_task] Received job {job.id} (phase: {job.phase})")
        safe_append_job(job)
        job_event.set()

    def process_job(job: ACPJob):
        out = ""
        print(job.phase, "job.phase")
        if job.phase == ACPJobPhase.NEGOTIATION:
            for memo in job.memos:
                print(memo.next_phase, "memo.next_phase")
                if memo.next_phase == ACPJobPhase.TRANSACTION:
                    out += f"Buyer agent is reacting to job:\n{job}\n\n"
                    buyer_agent.get_worker("acp_worker").run(
                        f"Respond to the following transaction: {job}",
                    )
                    out += "Buyer agent has responded to the job\n"

        print(Panel(out, title="🔁 Reaction", box=box.ROUNDED, title_align="left", border_style="red"))
    
    acp_plugin = AcpPlugin(
        options=AcpPluginOptions(
            api_key=env.GAME_API_KEY,
            acp_client=VirtualsACP(
                wallet_private_key=env.WHITELISTED_WALLET_PRIVATE_KEY,
                agent_wallet_address=env.BUYER_AGENT_WALLET_ADDRESS,
                on_evaluate=on_evaluate,
                on_new_task=on_new_task,
                entity_id=env.BUYER_ENTITY_ID
            ),
            cluster="<your_agent_cluster>", #example cluster
            graduated=True,
        )
    )

    def get_agent_state(_: None, _e: None) -> dict:
        state = acp_plugin.get_acp_state()
        state_dict = to_serializable_dict(state)
        return state_dict

    acp_worker = acp_plugin.get_worker(
        {
            "functions": [
                acp_plugin.search_agents_functions,
                acp_plugin.initiate_job
            ]
        }
    )

    agent = Agent(
        api_key=env.GAME_API_KEY,
        name="Virtuals",
        agent_goal="Finding the best meme to do tweet posting",
        agent_description=f"""
        Agent that gain market traction by posting meme. Your interest are in cats and AI. 
        You can head to acp to look for agents to help you generating meme.
        Do not look for a relevant validator to validate the deliverable.

        {acp_plugin.agent_description}
        """,
        workers=[acp_worker],
        get_agent_state_fn=get_agent_state
    )

    # Buyer agent is meant to handle payments
    buyer_worker = acp_plugin.get_worker(
        {
            "functions": [
                acp_plugin.pay_job
            ]
        }
    )

    buyer_agent = Agent(
        api_key=env.GAME_API_KEY,
        name="Buyer",
        agent_goal="Perform and complete transaction with seller",
        agent_description=f"""
        Agent that gain market traction by posting meme. Your interest are in cats and AI. 
        You can head to acp to look for agents to help you generating meme.
        Do not look for a relevant validator to validate the deliverable.

        {acp_plugin.agent_description}
        """,
        workers=[buyer_worker],
        get_agent_state_fn=get_agent_state
    )

    buyer_agent.compile()
    agent.compile()

    # Start background job thread
    threading.Thread(target=job_worker, daemon=True).start()

    while True:
        print("🟢"*40)
        init_state = AcpState.model_validate(agent.agent_state)
        print(Panel(f"{init_state}", title="Agent State", box=box.ROUNDED, title_align="left"))
        
        print("[agent.step] Attempting to acquire lock for agent.step()")
        with job_queue_lock:
            print("[agent.step] Lock acquired, executing agent.step()")
            agent.step()
        print("[agent.step] Released lock after agent.step()")

        end_state = AcpState.model_validate(agent.agent_state)
        print(Panel(f"{end_state}", title="End Agent State", box=box.ROUNDED, title_align="left"))
        print("🔴"*40)
        input("\nPress any key to continue...\n")

if __name__ == "__main__":
    buyer(use_thread_lock=True)
