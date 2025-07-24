from pydantic_settings import BaseSettings
from pydantic import field_validator

class TravelDemoEnvSettings(BaseSettings):
    WHITELISTED_WALLET_PRIVATE_KEY: str
    TRAVELER_AGENT_WALLET_ADDRESS: str
    AGENCY_AGENT_WALLET_ADDRESS: str
    PLANNER_AGENT_WALLET_ADDRESS: str
    FLIGHT_FINDER_AGENT_WALLET_ADDRESS: str
    TRAVELER_ENTITY_ID: int
    AGENCY_ENTITY_ID: int
    PLANNER_ENTITY_ID: int
    FLIGHT_FINDER_ENTITY_ID: int
    GAME_API_KEY: str

    @field_validator("WHITELISTED_WALLET_PRIVATE_KEY")
    def strip_0x_prefix(cls, v: str):
        if v and v.startswith("0x"):
            raise ValueError("WHITELISTED_WALLET_PRIVATE_KEY must not start with '0x'. Please remove it.")
        return v

    @field_validator(
        "TRAVELER_AGENT_WALLET_ADDRESS",
        "AGENCY_AGENT_WALLET_ADDRESS",
        "PLANNER_AGENT_WALLET_ADDRESS",
        "FLIGHT_FINDER_AGENT_WALLET_ADDRESS"
    )
    def validate_wallet_address(cls, v: str):
        if v is None:
            raise ValueError("Wallet address must not be None.")
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError("Wallet address must start with '0x' and be 42 characters long.")
        return v

    @field_validator("GAME_API_KEY")
    def check_apt_prefix(cls, v: str):
        if v and not v.startswith("apt-"):
            raise ValueError("GAME key must start with 'apt-'")
        return v
